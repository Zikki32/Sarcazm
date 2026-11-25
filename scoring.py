"""Скоринг вакансии: насколько она понятна и привлекательна для соискателя.

Оценка полностью детерминированная и объяснимая. Итоговые 100 баллов
складываются из трёх компонентов:

- понятность описания (50%) — 100 минус штрафы за пропуски и замечания ИИ;
- график работы (25%) — насколько конкретно указаны дни, часы и тип графика;
- оплата относительно средней по региону (25%) — сравнение с рыночным
  ориентиром Калининградской области (market.py).

Один и тот же вход всегда даёт один и тот же результат, поэтому жюри может
проверить расчёт вручную.

Дополнительный сценарий: вакансии с оценкой выше порога и минимальным числом
замечаний попадают в отдельный файл «готовых» вакансий.
"""

import re
from typing import Any

from market import DEFAULT_MARKET, MarketBenchmark, analyze_salary, analyze_schedule
from models import IssueNote, VacancyReport
from rules import is_blank

_DIGITS_RE = re.compile(r"\d[\d\s]{2,}")
_ADDRESS_DETAIL_RE = re.compile(
    r"(\d|ул\.|улиц|пр-т|пр\.|просп|переул|пер\.|шоссе|наб\.|набереж|д\.|дом|корп|оф\.|офис|помещ|стр\.)",
    re.IGNORECASE,
)

GRADE_THRESHOLDS = ((85, "A"), (70, "B"), (50, "C"), (0, "D"))

# Веса компонентов итоговой оценки. Сумма — 1.0.
SCORE_WEIGHTS = {
    "content": 0.5,
    "schedule": 0.25,
    "salary": 0.25,
}

TOPIC_LABELS = {
    "role_title": "Должность",
    "responsibilities": "Обязанности",
    "requirements": "Требования",
    "compensation": "Оплата",
    "schedule": "График",
    "location": "Место работы",
    "employment_type": "Занятость",
}


def grade_for(score: int) -> str:
    for threshold, grade in GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return "D"


def _has_salary(payload: dict[str, Any]) -> bool:
    if payload.get("salary_min") or payload.get("salary_max"):
        return True
    return bool(_DIGITS_RE.search(payload.get("salary_raw") or ""))


def _has_full_address(locations: list[str]) -> bool:
    return any(_ADDRESS_DETAIL_RE.search(location) for location in locations)


def _round_half_up(value: float) -> int:
    """Обычное округление 0,5 вверх (round() в Python округляет к чётному)."""
    return int(value + 0.5) if value >= 0 else int(value - 0.5)


def score_vacancy(
    payload: dict[str, Any],
    issues: list[IssueNote],
    market: MarketBenchmark | None = None,
) -> dict[str, Any]:
    """Возвращает оценку 0–100, категорию, штрафы и разбор компонентов."""
    market = market or DEFAULT_MARKET
    penalties: list[dict[str, Any]] = []

    def penalize(points: int, reason: str) -> None:
        penalties.append({"points": points, "reason": reason})

    if not _has_salary(payload):
        penalize(25, "Не указана оплата труда")
    elif not (payload.get("salary_min") and payload.get("salary_max")):
        penalize(8, "Указана только одна граница зарплаты")

    locations = payload.get("locations") or []
    if not locations:
        penalize(12, "Не указан адрес места работы")
    elif not _has_full_address(locations):
        penalize(6, "Адрес указан неполно (нет улицы, дома или офиса)")

    if not payload.get("schedule"):
        penalize(12, "Не указан график работы")
    if not payload.get("employment"):
        penalize(8, "Не указан тип занятости")
    if not payload.get("duty"):
        penalize(12, "Не описаны обязанности")

    requirement = payload.get("requirement") or {}
    if not (payload.get("requirements") or requirement.get("qualification")):
        penalize(8, "Не описаны требования к кандидату")
    if is_blank(requirement.get("education")):
        penalize(4, "Не указаны требования к образованию")

    for issue in issues:
        # Структурные пропуски уже учтены выше — не штрафуем дважды
        if issue.detected_by == "rule":
            continue
        topic = TOPIC_LABELS.get(issue.topic, issue.topic)
        if issue.issue_type == "contradiction":
            penalize(8, f"Противоречие в теме «{topic}»")
        elif issue.issue_type == "ambiguity":
            penalize(3, f"Неоднозначность в теме «{topic}»")
        else:
            penalize(5, f"Пропуск сведений в теме «{topic}»")

    total_penalty = sum(item["points"] for item in penalties)
    content_score = max(0, min(100, 100 - total_penalty))

    schedule_analysis = analyze_schedule(payload.get("schedule") or "")
    salary_analysis = analyze_salary(payload, market=market)

    components = [
        {
            "key": "content",
            "label": "Понятность описания",
            "score": content_score,
            "weight": SCORE_WEIGHTS["content"],
            "weighted": round(content_score * SCORE_WEIGHTS["content"], 1),
            "note": (
                f"100 − {total_penalty} штрафных баллов"
                if total_penalty
                else "Штрафов за неполноту нет"
            ),
        },
        {
            "key": "schedule",
            "label": "График работы",
            "score": schedule_analysis["score"],
            "weight": SCORE_WEIGHTS["schedule"],
            "weighted": round(schedule_analysis["score"] * SCORE_WEIGHTS["schedule"], 1),
            "note": schedule_analysis["notes"][0] if schedule_analysis["notes"] else "",
        },
        {
            "key": "salary",
            "label": "Оплата к средней по региону",
            "score": salary_analysis["score"],
            "weight": SCORE_WEIGHTS["salary"],
            "weighted": round(salary_analysis["score"] * SCORE_WEIGHTS["salary"], 1),
            "note": salary_analysis["notes"][0] if salary_analysis["notes"] else "",
        },
    ]

    weighted_total = sum(item["weighted"] for item in components)
    score = max(0, min(100, _round_half_up(weighted_total)))

    return {
        "score": score,
        "grade": grade_for(score),
        "penalties": penalties,
        "score_components": components,
        "schedule_analysis": schedule_analysis,
        "salary_analysis": salary_analysis,
    }


def build_good_vacancy(
    payload: dict[str, Any],
    report: VacancyReport,
    scoring: dict[str, Any],
) -> dict[str, Any]:
    """Компактная карточка вакансии для файла «готовых к публикации»."""
    return {
        "vacancy_id": report.vacancy_id,
        "url": report.url,
        "score": scoring["score"],
        "grade": scoring["grade"],
        "job_name": payload.get("job-name") or "",
        "company": payload.get("company_name") or "",
        "region": payload.get("region") or "",
        "salary": {
            "min": payload.get("salary_min"),
            "max": payload.get("salary_max"),
            "currency": payload.get("currency") or "",
            "raw": payload.get("salary_raw") or "",
        },
        "schedule": payload.get("schedule") or "",
        "employment": payload.get("employment") or "",
        "locations": payload.get("locations") or [],
        "issues": [issue.model_dump() for issue in report.issues],
        "penalties": scoring["penalties"],
        "score_components": scoring["score_components"],
        "schedule_analysis": scoring["schedule_analysis"],
        "salary_analysis": scoring["salary_analysis"],
    }


def is_good_vacancy(report: VacancyReport, max_issues: int) -> bool:
    """Вакансия считается готовой, если анализ прошёл и замечаний не больше лимита."""
    return report.analysis_error is None and len(report.issues) <= max_issues
