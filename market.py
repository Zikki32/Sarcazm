"""Рыночные ориентиры и оценка графика/оплаты для скоринга вакансий.

Средняя зарплата в Калининградской области (ориентиры на октябрь 2026):

- Калининградстат: средняя начисленная заработная плата за январь–август
  2026 года — 83 202,9 ₽ (округлённо 83 200 ₽).
- ГородРабот.ру, октябрь 2026: медиана предлагаемых зарплат — 70 000 ₽,
  модальная зарплата — 50 000 ₽.
- РИА Новости / РБК, первое полугодие 2026: средняя чистая зарплата
  в Калининграде — 86,3 тыс. ₽. Используется как перекрёстная проверка.

Для скоринга берётся средняя начисленная зарплата по региону (83 200 ₽).
Значение можно переопределить через MARKET_AVG_SALARY и MARKET_MEDIAN_SALARY,
если данные устарели или жюри хочет подставить свежий ориентир.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

KALININGRAD_REGION = "Калининградская область"
KALININGRAD_AVG_SALARY = 83_200
KALININGRAD_MEDIAN_SALARY = 70_000
KALININGRAD_MODAL_SALARY = 50_000

MARKET_SOURCE = (
    "Калининградстат, январь–август 2026 (83 202,9 ₽); "
    "ГородРабот.ру, октябрь 2026 (медиана 70 000 ₽, мода 50 000 ₽); "
    "РИА Новости / РБК, I полугодие 2026 (86,3 тыс. ₽ чистыми)"
)
MARKET_UPDATED = "2026-10"


@dataclass(frozen=True)
class MarketBenchmark:
    """Ориентиры рынка труда, с которыми сравнивается вакансия."""

    region: str = KALININGRAD_REGION
    average_salary: int = KALININGRAD_AVG_SALARY
    median_salary: int = KALININGRAD_MEDIAN_SALARY
    modal_salary: int = KALININGRAD_MODAL_SALARY
    source: str = MARKET_SOURCE
    updated: str = MARKET_UPDATED


DEFAULT_MARKET = MarketBenchmark()


# ---------------------------------------------------------------------------
# График работы
# ---------------------------------------------------------------------------

_DAYS_RE = re.compile(
    r"\b(?:"
    r"пн|вт|ср|чт|пт|сб|вс|"
    r"понедельник\w*|вторник\w*|сред\w*|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*|"
    r"будн\w*|рабочие\s+дни|"
    r"пятиднев\w*|шестиднев\w*|"
    r"день\s+через\s+день|сутки|вахт\w*|"
    r"\d{1,2}\s*/\s*\d{1,2}"
    r")\b",
    re.IGNORECASE,
)
_TIME_RE = re.compile(
    r"\b\d{1,2}[:.]\d{2}\b"
    r"|\bс\s*\d{1,2}(?:[:.]\d{2})?\s*(?:до|[-–—])\s*\d{1,2}(?:[:.]\d{2})?\b"
    r"|\b\d{1,2}\s*[-–—]\s*\d{1,2}\s*(?:час|ч)\b"
    r"|\b\d{1,2}\s*(?:час|ч)\b",
    re.IGNORECASE,
)

_REMOTE_RE = re.compile(r"удал[её]н|дистанц|надомн|на\s+дому", re.IGNORECASE)
_WATCH_RE = re.compile(r"вахт", re.IGNORECASE)
_SHIFT_RE = re.compile(
    r"смен|"
    r"\b(?:2\s*/\s*2|3\s*/\s*3|4\s*/\s*4|1\s*/\s*3)\b|"
    r"день\s+через\s+день|ночн|сутки|"
    r"\b12\s*час",
    re.IGNORECASE,
)
_FLEX_RE = re.compile(r"гибк", re.IGNORECASE)
_PART_RE = re.compile(r"неполн|частичн|подработ|\b0[.,]5\s*ставк", re.IGNORECASE)
_STANDARD_RE = re.compile(
    r"полн\w*\s+рабоч\w*\s+день|"
    r"пятиднев\w*|шестиднев\w*|"
    r"\b5\s*/\s*2\b|\b6\s*/\s*1\b|"
    r"пн\s*[-–—]\s*пт|"
    r"понедельник\w*\s*[-–—]\s*пятниц\w*|"
    r"будн\w*|рабочие\s+дни|"
    r"фиксированн\w*",
    re.IGNORECASE,
)

_TYPE_LABELS = {
    "standard": "Стандартная пятидневка",
    "flexible": "Гибкий график",
    "shift": "Сменный график",
    "watch": "Вахтовый график",
    "remote": "Удалённая работа",
    "part_time": "Частичная занятость",
    "unknown": "Тип не распознан",
    "missing": "Не указан",
}

_COMFORT = {
    "standard": 100,
    "remote": 95,
    "flexible": 90,
    "part_time": 85,
    "shift": 70,
    "watch": 50,
    "unknown": 60,
    "missing": 0,
}


def _hours_in_range(text: str) -> int | None:
    """Длительность смены из строки вида «08:00–20:00»."""
    match = re.search(r"\b(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})\b", text)
    if not match:
        return None
    start = int(match.group(1)) * 60 + int(match.group(2))
    end = int(match.group(3)) * 60 + int(match.group(4))
    if end <= start:
        end += 24 * 60
    return (end - start) // 60


def analyze_schedule(schedule: str) -> dict[str, Any]:
    """Разбирает поле schedule и возвращает объяснимую оценку графика 0–100.

    Оценка = 0,6 × ясность + 0,4 × комфортность.
    Ясность показывает, хватает ли соискателю конкретики (дни, часы, схема),
    комфортность — базовая оценка типа графика с поправками за ночные смены,
    суточные дежурства и 12-часовые смены.
    """
    text = (schedule or "").strip().lower().replace("ё", "е")
    if not text:
        return {
            "type": "missing",
            "type_label": _TYPE_LABELS["missing"],
            "clarity": 0,
            "comfort": 0,
            "score": 0,
            "days_specified": False,
            "hours_specified": False,
            "notes": ["График не указан — соискатель не знает дни и часы работы."],
        }

    days_specified = bool(_DAYS_RE.search(text))
    hours_specified = bool(_TIME_RE.search(text))

    if _REMOTE_RE.search(text):
        schedule_type = "remote"
    elif _WATCH_RE.search(text):
        schedule_type = "watch"
    elif _SHIFT_RE.search(text):
        schedule_type = "shift"
    elif _FLEX_RE.search(text):
        schedule_type = "flexible"
    elif _PART_RE.search(text):
        schedule_type = "part_time"
    elif _STANDARD_RE.search(text):
        schedule_type = "standard"
    else:
        schedule_type = "unknown"

    clarity = 45
    if schedule_type != "unknown":
        clarity += 5
    if days_specified:
        clarity += 25
    if hours_specified:
        clarity += 25
    if not days_specified and not hours_specified:
        clarity = 50 if schedule_type != "unknown" else 40
    clarity = max(0, min(100, clarity))

    comfort = _COMFORT[schedule_type]
    if "ночн" in text:
        comfort -= 15
    if "сутки" in text or re.search(r"\b24\s*час", text):
        comfort -= 15
    else:
        hours = _hours_in_range(text)
        if hours is not None and hours >= 12:
            comfort -= 10
    if "выходн" in text and ("только" in text or "сб" in text or "вс" in text):
        comfort -= 10
    comfort = max(0, min(100, comfort))

    score = max(0, min(100, int(round(0.6 * clarity + 0.4 * comfort))))

    notes: list[str] = []
    if days_specified and hours_specified:
        notes.append("Указаны дни работы и часы начала/окончания.")
    elif days_specified:
        notes.append("Указаны дни работы, но нет времени начала и окончания.")
    elif hours_specified:
        notes.append("Указано время работы, но нет дней недели или схемы смен.")
    else:
        notes.append("График описан общо: нет ни дней, ни часов работы.")

    if schedule_type == "standard":
        notes.append("Стандартный фиксированный график — понятная пятидневка.")
    elif schedule_type == "flexible":
        notes.append("Гибкий график: уточните обязательные часы и порядок учёта переработки.")
    elif schedule_type == "shift":
        notes.append("Сменный график: уточните продолжительность смены и доплаты за ночь/переработку.")
    elif schedule_type == "watch":
        notes.append("Вахта: уточните длительность вахты, проезд и условия проживания.")
    elif schedule_type == "remote":
        notes.append("Удалённая работа: уточните часовой пояс и доступность в рабочие часы.")
    elif schedule_type == "part_time":
        notes.append("Частичная занятость: уточните количество часов в неделю и размер ставки.")
    else:
        notes.append("Тип графика не распознан — проверьте формулировку в поле schedule.")

    if "ночн" in text:
        notes.append("Есть ночные смены — уточните размер доплаты.")
    hours = _hours_in_range(text)
    if hours is not None and hours >= 12 and "сутки" not in text:
        notes.append(f"Смена длится около {hours} часов — проверьте режим переработки.")

    return {
        "type": schedule_type,
        "type_label": _TYPE_LABELS[schedule_type],
        "clarity": clarity,
        "comfort": comfort,
        "score": score,
        "days_specified": days_specified,
        "hours_specified": hours_specified,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Оплата относительно средней по региону
# ---------------------------------------------------------------------------

_SALARY_POINTS = (
    (0.0, 0),
    (0.4, 10),
    (0.6, 30),
    (0.8, 55),
    (1.0, 75),
    (1.2, 88),
    (1.5, 100),
)

_SALARY_DIGITS_RE = re.compile(r"\d[\d\s]{2,}")


def _interpolate(value: float, points: tuple[tuple[float, int], ...]) -> int:
    """Линейная интерполяция баллов между опорными точками."""
    if value <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if value <= x1:
            share = (value - x0) / (x1 - x0)
            return int(round(y0 + share * (y1 - y0)))
    return points[-1][1]


def _salary_numbers(raw: str) -> list[int]:
    numbers: list[int] = []
    for token in _SALARY_DIGITS_RE.findall(raw or ""):
        digits = re.sub(r"\s+", "", token)
        try:
            numbers.append(int(digits))
        except ValueError:
            continue
    return numbers


def representative_salary(payload: dict[str, Any]) -> int | None:
    """Середина вилки; если указана одна граница — она; иначе разбор salary_raw."""
    low = payload.get("salary_min")
    high = payload.get("salary_max")
    if low and high:
        return int(round((low + high) / 2))
    if low:
        return int(low)
    if high:
        return int(high)

    numbers = _salary_numbers(str(payload.get("salary_raw") or ""))
    if not numbers:
        return None
    if len(numbers) >= 2:
        return int(round((numbers[0] + numbers[1]) / 2))
    return numbers[0]


def _format_money(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def analyze_salary(
    payload: dict[str, Any],
    market: MarketBenchmark | None = None,
) -> dict[str, Any]:
    """Сравнивает зарплату вакансии со средней по Калининградской области."""
    market = market or DEFAULT_MARKET
    value = representative_salary(payload)
    benchmark = market.average_salary

    if value is None:
        return {
            "value": None,
            "benchmark": benchmark,
            "median": market.median_salary,
            "ratio": None,
            "score": 0,
            "label": "не указана",
            "notes": [
                "Оплата не указана — сравнить её со средней по региону нельзя.",
                f"Ориентир: средняя начисленная зарплата в регионе — {_format_money(benchmark)} ₽.",
            ],
        }

    ratio = value / benchmark if benchmark else 0.0
    score = _interpolate(ratio, _SALARY_POINTS)

    if ratio < 0.6:
        label = "существенно ниже средней"
    elif ratio < 0.85:
        label = "ниже средней"
    elif ratio <= 1.15:
        label = "на уровне средней"
    elif ratio <= 1.5:
        label = "выше средней"
    else:
        label = "существенно выше средней"

    notes = [
        f"Оплата {_format_money(value)} ₽ — {round(ratio * 100)}% от средней по региону "
        f"({_format_money(benchmark)} ₽).",
        f"Медиана предлагаемых зарплат в Калининграде — {_format_money(market.median_salary)} ₽.",
    ]

    if not (payload.get("salary_min") and payload.get("salary_max")):
        notes.append(
            "Указана только одна граница зарплаты — для сравнения взята она."
        )
    currency = str(payload.get("currency") or "").upper()
    if currency and currency != "RUB":
        notes.append(
            f"Валюта {currency} отличается от рубля — сравнение приблизительное."
        )

    return {
        "value": value,
        "benchmark": benchmark,
        "median": market.median_salary,
        "ratio": round(ratio, 3),
        "score": score,
        "label": label,
        "notes": notes,
    }
