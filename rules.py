"""Детерминированный слой: нормализация полей вакансии и правила для очевидных пропусков.

Правила решают две задачи:
1. Готовят компактную и чистую карточку для LLM (без HTML, без мусорных
   placeholder-значений, с ограничением длины текстов).
2. Сразу находят пропуски по базовым полям, чтобы модель не тратила на них
   токены и не выдумывала формулировки.
"""

import html
import re
from typing import Any

from models import IssueNote, VacancyRecord

# ---------------------------------------------------------------------------
# Нормализация текста
# ---------------------------------------------------------------------------

_BLOCK_TAG_RE = re.compile(r"<\s*(br|/p|/div|/li|/tr|/h[1-6])\s*/?\s*>", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_SPACES_RE = re.compile(r"[ \t\r\f\v]+")
_NEWLINES_RE = re.compile(r"\n\s*\n\s*\n+")
_DIGITS_RE = re.compile(r"\d[\d\s]{2,}")

PLACEHOLDERS = {
    "",
    "-",
    "--",
    "—",
    "–",
    "нет",
    "нет данных",
    "не указан",
    "не указана",
    "не указано",
    "не указаны",
    "не требуется",
    "n/a",
    "na",
    "null",
    "none",
}


def clean_text(value: Any) -> str:
    """Убирает HTML-теги, HTML-сущности и лишние пробелы."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    text = html.unescape(value)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _ANY_TAG_RE.sub(" ", text)
    text = text.replace("\u00a0", " ")
    text = _SPACES_RE.sub(" ", text)
    text = _NEWLINES_RE.sub("\n\n", text)
    return text.strip()


def truncate(text: str, limit: int) -> str:
    """Ограничивает длину текста, чтобы не раздувать промпт."""
    if limit and len(text) > limit:
        return text[:limit].rstrip() + " […текст обрезан]"
    return text


def is_blank(value: Any) -> bool:
    """Пустое ли текстовое значение (с учётом типичных заглушек из API)."""
    if value is None:
        return True
    if isinstance(value, (int, float)):
        return False
    text = clean_text(value).lower().strip(" .,;:!?")
    return text in PLACEHOLDERS


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return None
    return number


def _salary_min_max(vac: dict[str, Any]) -> tuple[int | None, int | None]:
    lo = _to_int(vac.get("salary_min"))
    hi = _to_int(vac.get("salary_max"))
    if lo == 0:
        lo = None
    if hi == 0:
        hi = None
    return lo, hi


def normalize_currency(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    lowered = text.lower()
    if "руб" in lowered or lowered in {"rur", "rub"}:
        return "RUB"
    return text


def _flatten_addresses(raw_vac: dict[str, Any]) -> list[str]:
    locations: list[str] = []
    addresses = raw_vac.get("addresses")
    if isinstance(addresses, dict):
        addr_list = addresses.get("address")
        if isinstance(addr_list, list):
            for item in addr_list:
                if isinstance(item, dict):
                    location = clean_text(item.get("location"))
                    if location:
                        locations.append(location)
    elif isinstance(addresses, list):
        for item in addresses:
            if isinstance(item, dict):
                location = clean_text(item.get("location"))
                if location:
                    locations.append(location)
    # Убираем повторы, сохраняя порядок
    return list(dict.fromkeys(locations))


def build_vacancy_payload(record: VacancyRecord, max_text_chars: int = 1500) -> dict[str, Any]:
    """Формирует нормализованную карточку вакансии для правил и для ИИ."""
    vac = record.raw_vacancy
    requirement = vac.get("requirement") if isinstance(vac.get("requirement"), dict) else {}
    company = vac.get("company") if isinstance(vac.get("company"), dict) else {}
    region = vac.get("region") if isinstance(vac.get("region"), dict) else {}

    salary_min, salary_max = _salary_min_max(vac)

    return {
        "id": record.vacancy_id,
        "url": record.url,
        "job-name": clean_text(vac.get("job-name")),
        "company_name": clean_text(company.get("name")),
        "region": clean_text(region.get("name")),
        "employment": clean_text(vac.get("employment")),
        "schedule": clean_text(vac.get("schedule")),
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_raw": clean_text(vac.get("salary")),
        "currency": normalize_currency(vac.get("currency")),
        "duty": truncate(clean_text(vac.get("duty")), max_text_chars),
        "requirements": truncate(clean_text(vac.get("requirements")), max_text_chars),
        "requirement": {
            "education": clean_text(requirement.get("education")),
            "experience": _to_int(requirement.get("experience")),
            "qualification": truncate(clean_text(requirement.get("qualification")), max_text_chars),
        },
        "locations": _flatten_addresses(vac),
    }


def build_vacancy_card(payload: dict[str, Any]) -> dict[str, Any]:
    """Компактная карточка для отчёта и дашборда (без длинных текстов)."""
    return {
        "job_name": payload.get("job-name") or "",
        "company": payload.get("company_name") or "",
        "region": payload.get("region") or "",
        "salary_min": payload.get("salary_min"),
        "salary_max": payload.get("salary_max"),
        "salary_raw": payload.get("salary_raw") or "",
        "currency": payload.get("currency") or "",
        "schedule": payload.get("schedule") or "",
        "employment": payload.get("employment") or "",
        "locations": payload.get("locations") or [],
    }


# ---------------------------------------------------------------------------
# Правила: очевидные пропуски
# ---------------------------------------------------------------------------

def _issue(
    topic: str,
    source_field: str,
    problem: str,
    recommendation: str,
) -> IssueNote:
    return IssueNote(
        topic=topic,  # type: ignore[arg-type]
        source_field=source_field,
        quote="",
        issue_type="missing",
        problem=problem,
        recommendation=recommendation,
        detected_by="rule",
    )


def _mentions_education(*texts: str) -> bool:
    return any("образован" in (text or "").lower() for text in texts)


def collect_rule_issues(payload: dict[str, Any]) -> list[IssueNote]:
    """Пропуски по базовым полям, которые не требуют участия модели."""
    issues: list[IssueNote] = []

    if not payload.get("job-name"):
        issues.append(
            _issue(
                "role_title",
                "job-name",
                "Не заполнено название должности, поэтому соискатель не понимает, на какую позицию откликается.",
                "Укажите корректное название должности так, как оно будет указано в трудовом договоре.",
            )
        )

    has_salary = payload.get("salary_min") or payload.get("salary_max")
    if not has_salary and not _DIGITS_RE.search(payload.get("salary_raw") or ""):
        issues.append(
            _issue(
                "compensation",
                "salary_min, salary_max, salary",
                "Не указан размер оплаты труда: salary_min и salary_max пусты или равны нулю, в тексте вакансии сумма тоже не упоминается.",
                "Укажите сумму или диапазон (оклад, ставку, оплату за смену) либо явно опишите, от чего зависит доход.",
            )
        )

    if not payload.get("schedule"):
        issues.append(
            _issue(
                "schedule",
                "schedule",
                "Не указан график работы: соискатель не знает продолжительность рабочего дня, дни работы и выходные.",
                "Заполните поле schedule: дни недели, время начала и окончания работы, при сменном режиме — схему смен.",
            )
        )

    if not payload.get("employment"):
        issues.append(
            _issue(
                "employment_type",
                "employment",
                "Не указан тип занятости (полная, частичная, проектная и т. п.).",
                "Заполните поле employment, чтобы соискатель понимал объём и характер занятости.",
            )
        )

    if not payload.get("locations"):
        issues.append(
            _issue(
                "location",
                "addresses",
                "Не указан адрес места работы: в ответе API нет ни одного адреса.",
                "Заполните addresses.location или укажите в описании населённый пункт и адрес (либо формат удалённой работы).",
            )
        )

    if not payload.get("duty"):
        issues.append(
            _issue(
                "responsibilities",
                "duty",
                "Не описаны обязанности: поле duty пустое.",
                "Перечислите 3–7 основных задач, которые будет выполнять сотрудник.",
            )
        )

    requirement = payload.get("requirement") or {}
    qualification = requirement.get("qualification") or ""
    requirements_text = payload.get("requirements") or ""
    education = requirement.get("education") or ""

    if not qualification and not requirements_text:
        issues.append(
            _issue(
                "requirements",
                "requirements, requirement.qualification",
                "Не описаны требования к кандидату: поля requirements и requirement.qualification пусты.",
                "Перечислите обязательные знания, навыки и допущения по опыту работы.",
            )
        )

    if is_blank(education) and not _mentions_education(qualification, requirements_text):
        issues.append(
            _issue(
                "requirements",
                "requirement.education",
                "Требования к образованию не заданы: поле education пустое или содержит заглушку «Не указано», в тексте требования образование не упоминается.",
                "Укажите требуемый уровень и профиль образования либо явно напишите, что образование не имеет значения.",
            )
        )

    return issues


# ---------------------------------------------------------------------------
# Слияние результатов правил и ИИ
# ---------------------------------------------------------------------------

def _field_key(source_field: str) -> str:
    return re.sub(r"\s+", "", (source_field or "").lower())


def _drop_contained_quotes(issues: list[IssueNote]) -> list[IssueNote]:
    """Убирает замечание, если его цитата целиком входит в цитату другого замечания по той же теме.

    Пример: модель отдельно отметила «Гибкий график» как неоднозначность и отдельно
    описала то же место как противоречие с более длинной цитатой.
    """
    result: list[IssueNote] = []
    for issue in issues:
        quote = issue.quote.strip().lower()
        if len(quote) >= 4 and any(
            other is not issue
            and other.topic == issue.topic
            and quote in (other.quote or "").lower()
            for other in issues
        ):
            continue
        result.append(issue)
    return result


_STRUCTURED_TOPIC_FIELDS = {
    "role_title": "job-name",
    "schedule": "schedule",
    "employment_type": "employment",
}


def _drop_false_missing(ai_issues: list[IssueNote], payload: dict[str, Any]) -> list[IssueNote]:
    """Убирает от ИИ замечания «missing» по полям, которые на самом деле заполнены.

    Опечатка модели не должна превращаться в отчёт: если структурированное поле
    содержит значение, отсутствие сведений — это уже ambiguity, а не missing.
    """
    result: list[IssueNote] = []
    for issue in ai_issues:
        if issue.issue_type == "missing":
            if issue.topic in _STRUCTURED_TOPIC_FIELDS:
                if payload.get(_STRUCTURED_TOPIC_FIELDS[issue.topic]):
                    continue
            if issue.topic == "compensation":
                has_salary = payload.get("salary_min") or payload.get("salary_max")
                if has_salary or _DIGITS_RE.search(payload.get("salary_raw") or ""):
                    continue
        result.append(issue)
    return result


def merge_issues(
    rule_issues: list[IssueNote],
    ai_issues: list[IssueNote],
    payload: dict[str, Any] | None = None,
    max_issues: int = 12,
) -> list[IssueNote]:
    """Объединяет замечания, убирая дубли и наводнения по одной теме."""
    if payload is not None:
        ai_issues = _drop_false_missing(ai_issues, payload)

    merged: list[IssueNote] = []
    seen: set[tuple[str, str, str]] = set()
    rule_missing_topics = {
        issue.topic for issue in rule_issues if issue.issue_type == "missing"
    }
    per_topic: dict[str, int] = {}

    for issue in [*rule_issues, *ai_issues]:
        if issue.issue_type == "missing":
            # По ТЗ у пропуска цитата всегда пустая
            issue = issue.model_copy(update={"quote": ""})
            if issue.detected_by == "ai" and issue.topic in rule_missing_topics:
                # Правило уже сообщило о пропуске по этой теме
                continue

        key = (issue.topic, issue.issue_type, _field_key(issue.source_field))
        if key in seen:
            continue

        # Не больше двух замечаний на одну тему: защита от «наводнений» модели
        if per_topic.get(issue.topic, 0) >= 2:
            continue

        seen.add(key)
        per_topic[issue.topic] = per_topic.get(issue.topic, 0) + 1
        merged.append(issue)

        if len(merged) >= max_issues:
            break

    return _drop_contained_quotes(merged)
