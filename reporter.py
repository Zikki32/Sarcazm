import json
import logging
import os
from pathlib import Path

from models import VacancyReport

logger = logging.getLogger(__name__)


def deduplicate_reports(reports: list[VacancyReport]) -> list[VacancyReport]:
    """Гарантирует один результат на ID вакансии в итоговом отчёте."""
    by_id: dict[str, VacancyReport] = {}
    for item in reports:
        by_id[item.vacancy_id] = item
    return list(by_id.values())


def _write_json_atomic(output_path: Path, payload: list[dict]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(output_path.name + ".tmp")

    try:
        with tmp_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_path, output_path)
    except Exception:
        # Временный файл не должен оставаться мусором рядом с рабочим отчётом
        tmp_path.unlink(missing_ok=True)
        raise


def save_report(output_path: Path, reports: list[VacancyReport]) -> int:
    """Атомарно сохраняет отчёт в JSON. Возвращает число записей."""
    unique_reports = deduplicate_reports(reports)
    _write_json_atomic(output_path, [item.model_dump() for item in unique_reports])
    logger.info("Отчёт сохранён: %s (вакансий: %s)", output_path, len(unique_reports))
    return len(unique_reports)


def save_good_vacancies(output_path: Path, items: list[dict]) -> int:
    """Атомарно сохраняет список вакансий, готовых к публикации."""
    _write_json_atomic(output_path, items)
    logger.info("Файл готовых вакансий сохранён: %s (вакансий: %s)", output_path, len(items))
    return len(items)
