"""Проверки скоринга: график, оплата к средней и итоговая формула."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market import DEFAULT_MARKET, analyze_salary, analyze_schedule
from scoring import score_vacancy


def full_payload(**overrides):
    payload = {
        "id": "TEST-1",
        "job-name": "Специалист",
        "company_name": "Организация",
        "region": "Калининградская область",
        "employment": "Полная занятость",
        "schedule": "Полный рабочий день, понедельник–пятница, 09:00–18:00",
        "salary_min": 45000,
        "salary_max": 55000,
        "salary_raw": "",
        "currency": "RUB",
        "duty": "Ведение документов и подготовка отчётов.",
        "requirements": "Высшее образование, уверенный пользователь ПК.",
        "requirement": {
            "education": "Высшее",
            "experience": 1,
            "qualification": "Знание офисных программ.",
        },
        "locations": ["г. Калининград, ул. Учебная, д. 1"],
    }
    payload.update(overrides)
    return payload


class ScheduleTests(unittest.TestCase):
    def test_detailed_standard_schedule_is_max(self):
        info = analyze_schedule("Полный рабочий день, понедельник–пятница, 09:00–18:00")
        self.assertEqual(info["type"], "standard")
        self.assertEqual(info["clarity"], 100)
        self.assertEqual(info["score"], 100)

    def test_weekday_range_without_full_day_label(self):
        info = analyze_schedule("Понедельник–пятница, 10:00–19:00")
        self.assertEqual(info["type"], "standard")
        self.assertEqual(info["score"], 100)

    def test_vague_flexible_schedule_is_lower(self):
        info = analyze_schedule("Гибкий график")
        self.assertEqual(info["type"], "flexible")
        self.assertLess(info["score"], 80)

    def test_shift_schedule_with_hours(self):
        info = analyze_schedule("Сменный график: 2/2, 08:00–20:00")
        self.assertEqual(info["type"], "shift")
        self.assertGreaterEqual(info["score"], 70)

    def test_missing_schedule_is_zero(self):
        info = analyze_schedule("")
        self.assertEqual(info["type"], "missing")
        self.assertEqual(info["score"], 0)


class SalaryTests(unittest.TestCase):
    def test_midpoint_is_compared_with_average(self):
        info = analyze_salary(
            {"salary_min": 45000, "salary_max": 55000, "currency": "RUB"},
            DEFAULT_MARKET,
        )
        self.assertEqual(info["value"], 50000)
        self.assertEqual(info["score"], 30)
        self.assertEqual(info["label"], "ниже средней")

    def test_salary_above_average(self):
        info = analyze_salary(
            {"salary_min": 120000, "salary_max": 120000, "currency": "RUB"},
            DEFAULT_MARKET,
        )
        self.assertGreaterEqual(info["score"], 88)
        self.assertIn(info["label"], {"выше средней", "существенно выше средней"})

    def test_missing_salary_has_zero_component(self):
        info = analyze_salary({"salary_min": None, "salary_max": None}, DEFAULT_MARKET)
        self.assertIsNone(info["value"])
        self.assertEqual(info["score"], 0)

    def test_salary_raw_is_used_as_fallback(self):
        info = analyze_salary(
            {"salary_min": None, "salary_max": None, "salary_raw": "от 40 000 до 60 000"},
            DEFAULT_MARKET,
        )
        self.assertEqual(info["value"], 50000)


class CombinedScoreTests(unittest.TestCase):
    def test_components_have_expected_weights(self):
        result = score_vacancy(full_payload(), [])
        components = {item["key"]: item for item in result["score_components"]}
        self.assertEqual(set(components), {"content", "schedule", "salary"})
        self.assertAlmostEqual(sum(item["weight"] for item in components.values()), 1.0)
        self.assertEqual(components["content"]["score"], 100)
        self.assertEqual(components["schedule"]["score"], 100)
        self.assertEqual(components["salary"]["score"], 30)

    def test_final_score_is_weighted_sum(self):
        result = score_vacancy(full_payload(), [])
        weighted = sum(item["weighted"] for item in result["score_components"])
        self.assertEqual(result["score"], int(weighted + 0.5))
        self.assertEqual(result["grade"], "B")

    def test_missing_salary_and_schedule_lower_score(self):
        result = score_vacancy(
            full_payload(salary_min=None, salary_max=None, schedule=""),
            [],
        )
        components = {item["key"]: item for item in result["score_components"]}
        self.assertEqual(components["salary"]["score"], 0)
        self.assertEqual(components["schedule"]["score"], 0)
        self.assertLess(result["score"], 70)


if __name__ == "__main__":
    unittest.main()
