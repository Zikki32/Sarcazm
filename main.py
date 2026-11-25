import argparse
import logging
import sys
import time
from pathlib import Path

import schedule

from analyzer import VacancyAIAnalyzer
from config import AppConfig, load_config
from fetcher import FetchError, fetch_from_api, load_from_local_json
from market import MarketBenchmark
from models import VacancyReport
from reporter import save_good_vacancies, save_report
from rules import build_vacancy_card, build_vacancy_payload, collect_rule_issues
from scoring import build_good_vacancy, is_good_vacancy, score_vacancy

EXIT_OK = 0
EXIT_FETCH_ERROR = 1
EXIT_ANALYSIS_ERROR = 2

logger = logging.getLogger("transparent_vacancy")


def setup_logging(cfg: AppConfig, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(stream=sys.stdout)]
    try:
        cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(cfg.log_file, encoding="utf-8")
        handlers.append(file_handler)
    except OSError as exc:
        print(f"[WARN] Не удалось открыть лог-файл {cfg.log_file}: {exc}", file=sys.stderr)

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )
    # httpx пишет каждый запрос на уровне INFO — в отчёте о прогоне это шум
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def run_pipeline(
    cfg: AppConfig,
    mode_override: str | None = None,
    input_path: Path | None = None,
    output_path: Path | None = None,
    limit: int | None = None,
    good_output: Path | None = None,
    good_max_issues: int | None = None,
) -> int:
    mode = (mode_override or cfg.run_mode).strip().lower()
    if mode == "schedule":
        # Запуск по расписанию — это тот же живой режим
        mode = "live"

    output_path = output_path or cfg.report_output_path
    good_output = good_output or cfg.good_vacancies_path
    good_max_issues = cfg.good_max_issues if good_max_issues is None else good_max_issues
    good_max_issues = max(0, good_max_issues)
    limit = limit or cfg.max_vacancies
    if limit <= 0:
        logger.error("Лимит вакансий должен быть больше нуля")
        return EXIT_FETCH_ERROR

    logger.info("Запуск в режиме: %s (лимит: %s)", mode, limit)

    try:
        if mode == "check":
            local_path = input_path or cfg.local_json_path
            records = load_from_local_json(local_path, max_vacancies=limit)
        elif mode == "live":
            records = fetch_from_api(
                base_url=cfg.api_base_url,
                max_vacancies=limit,
                page_size=min(cfg.api_page_size, limit),
                max_pages=cfg.max_pages,
            )
        else:
            logger.error("Неизвестный режим запуска: %s. Используйте 'check', 'live' или 'schedule'.", mode)
            return EXIT_FETCH_ERROR
    except FetchError as exc:
        logger.error("Не удалось получить данные: %s", exc)
        if output_path.exists():
            logger.error(
                "Последний успешный отчёт сохранён без изменений: %s", output_path
            )
        return EXIT_FETCH_ERROR

    if not records:
        logger.error("Источник вернул 0 вакансий. Отчёт %s не изменён.", output_path)
        return EXIT_FETCH_ERROR

    logger.info("Загружено вакансий для анализа: %s", len(records))

    analyzer = VacancyAIAnalyzer(
        base_url=cfg.llm_base_url,
        api_key=cfg.llm_api_key,
        model=cfg.llm_model,
        timeout_seconds=cfg.llm_timeout_seconds,
        max_text_chars=cfg.max_text_chars,
    )
    market = MarketBenchmark(
        region=cfg.market_region,
        average_salary=cfg.market_avg_salary,
        median_salary=cfg.market_median_salary,
    )

    reports: list[VacancyReport] = []
    payloads: dict[str, dict] = {}
    scores: dict[str, dict] = {}
    ai_failures = 0
    started = time.monotonic()

    for position, record in enumerate(records, start=1):
        vacancy_started = time.monotonic()
        analysis_error: str | None = None
        payload = build_vacancy_payload(record, max_text_chars=cfg.max_text_chars)
        try:
            issues = analyzer.analyze_vacancy(record, payload=payload)
        except Exception as exc:  # LLM недоступна или вернула мусор
            ai_failures += 1
            analysis_error = str(exc)
            logger.warning(
                "[%s/%s] %s: ИИ недоступен, остаются только правила (%s)",
                position,
                len(records),
                record.vacancy_id,
                exc,
            )
            issues = collect_rule_issues(payload)

        scoring = score_vacancy(payload, issues, market=market)
        payloads[record.vacancy_id] = payload
        scores[record.vacancy_id] = scoring

        reports.append(
            VacancyReport(
                vacancy_id=record.vacancy_id,
                url=record.url,
                vacancy=build_vacancy_card(payload),
                issues=issues,
                analysis_error=analysis_error,
                score=scoring["score"],
                grade=scoring["grade"],
                penalties=scoring["penalties"],
                score_components=scoring["score_components"],
                schedule_analysis=scoring["schedule_analysis"],
                salary_analysis=scoring["salary_analysis"],
            )
        )
        logger.info(
            "[%s/%s] %s: замечаний %s, оценка %s (%s), %.1f с",
            position,
            len(records),
            record.vacancy_id,
            len(issues),
            scoring["score"],
            scoring["grade"],
            time.monotonic() - vacancy_started,
        )

    if ai_failures == len(records):
        logger.error(
            "ИИ не смогла обработать ни одну вакансию (%s из %s). "
            "Отчёт %s не изменён, чтобы не потерять прошлый результат.",
            ai_failures,
            len(records),
            output_path,
        )
        return EXIT_ANALYSIS_ERROR

    try:
        saved = save_report(output_path, reports)
    except OSError as exc:
        logger.error("Не удалось сохранить отчёт %s: %s", output_path, exc)
        return EXIT_FETCH_ERROR

    rule_notes = sum(1 for report in reports for issue in report.issues if issue.detected_by == "rule")
    ai_notes = sum(1 for report in reports for issue in report.issues if issue.detected_by == "ai")
    average_score = sum(item["score"] for item in scores.values()) / max(1, len(scores))
    logger.info(
        "Готово за %.1f с: вакансий %s, замечаний %s (правила: %s, ИИ: %s), "
        "средняя оценка %.1f, сбоев ИИ: %s",
        time.monotonic() - started,
        saved,
        rule_notes + ai_notes,
        rule_notes,
        ai_notes,
        average_score,
        ai_failures,
    )

    # Доп. сценарий: отбираем вакансии, готовые к публикации
    good_items = [
        build_good_vacancy(
            payloads[report.vacancy_id],
            report,
            scores[report.vacancy_id],
        )
        for report in reports
        if is_good_vacancy(report, good_max_issues)
    ]
    good_items.sort(key=lambda item: (-item["score"], item["vacancy_id"]))

    try:
        save_good_vacancies(good_output, good_items)
    except OSError as exc:
        logger.error("Не удалось сохранить файл готовых вакансий %s: %s", good_output, exc)
        return EXIT_FETCH_ERROR

    logger.info(
        "Готовых к публикации (не более %s замечаний): %s из %s",
        good_max_issues,
        len(good_items),
        len(reports),
    )
    if good_items:
        logger.info(
            "Лучшая вакансия: %s — %s баллов (%s), %s",
            good_items[0]["vacancy_id"],
            good_items[0]["score"],
            good_items[0]["grade"],
            good_items[0]["job_name"],
        )
    return EXIT_OK


def run_schedule(
    cfg: AppConfig,
    mode_override: str | None = None,
    output_path: Path | None = None,
    limit: int | None = None,
    good_output: Path | None = None,
    good_max_issues: int | None = None,
) -> int:
    interval = max(1, cfg.schedule_interval_minutes)
    mode = "live" if (mode_override or cfg.run_mode) == "schedule" else (mode_override or cfg.run_mode)
    logger.info("Планировщик: режим %s, интервал %s мин.", mode, interval)

    def job() -> None:
        try:
            run_pipeline(
                cfg,
                mode_override=mode,
                output_path=output_path,
                limit=limit,
                good_output=good_output,
                good_max_issues=good_max_issues,
            )
        except Exception:
            # Планировщик не должен умирать из-за одного неудачного прогона
            logger.exception("Ошибка во время планового прогона")

    job()
    schedule.every(interval).minutes.do(job)

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Планировщик остановлен пользователем")
        return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Прозрачная вакансия — отчёт о понятности условий работы (регион 39)"
    )
    parser.add_argument(
        "--mode",
        choices=["check", "live", "schedule"],
        default=None,
        help="check — локальный JSON, live — API, schedule — работа по расписанию",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Путь к локальному JSON для режима check (по умолчанию из .env)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Путь к файлу отчёта (по умолчанию из .env)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Максимальное число вакансий в этом прогоне",
    )
    parser.add_argument(
        "--good-output",
        type=Path,
        default=None,
        help="Куда сохранять вакансии, готовые к публикации (по умолчанию из .env)",
    )
    parser.add_argument(
        "--good-max-issues",
        type=int,
        default=None,
        help="Максимум замечаний для отбора готовых вакансий (по умолчанию 1)",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Алиас для --mode schedule",
    )
    parser.add_argument("--verbose", action="store_true", help="Подробный лог")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config()
    setup_logging(cfg, verbose=args.verbose)

    mode = args.mode
    if args.schedule:
        mode = "schedule"
    if mode is None and args.input is not None:
        mode = "check"
    mode = mode or cfg.run_mode

    if args.input is not None and mode != "check":
        logger.warning("Флаг --input используется только в режиме check и будет проигнорирован")

    if mode == "schedule":
        return run_schedule(
            cfg,
            mode_override="schedule",
            output_path=args.output,
            limit=args.limit,
            good_output=args.good_output,
            good_max_issues=args.good_max_issues,
        )

    return run_pipeline(
        cfg,
        mode_override=mode,
        input_path=args.input,
        output_path=args.output,
        limit=args.limit,
        good_output=args.good_output,
        good_max_issues=args.good_max_issues,
    )


if __name__ == "__main__":
    sys.exit(main())
