import json
import logging
import time
from typing import Any

import httpx
from pydantic import ValidationError

from models import IssueNote, VacancyAnalysisResult, VacancyRecord
from rules import build_vacancy_payload, collect_rule_issues, merge_issues

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — ассистент редактора вакансий. Оцени, насколько понятно и однозначно для соискателя описаны условия работы по 7 темам:
- role_title: название должности (поле job-name)
- responsibilities: обязанности (поле duty)
- requirements: требования (поля requirement.education, requirement.experience, requirement.qualification, requirements)
- compensation: оплата (поля salary_min, salary_max, salary_raw, currency)
- schedule: график (поле schedule, requirement.qualification)
- location: место работы (поля locations, duty, requirement.qualification)
- employment_type: тип занятости (поле employment)

Как читать карточку:
1. Карточка уже нормализована: HTML удалён, пробелы схлопнуты, слишком длинные тексты обрезаны с пометкой «[…текст обрезан]».
2. experience — целое число лет стажа. Значение 0 означает «опыт не требуется», null — «не заполнено». Не считай проблемой отсутствие единицы измерения у experience.
3. salary_min и salary_max — числа в рублях. null или отсутствие значения означает «не указано». salary_raw — исходная строка из API. currency уже приведена к короткому виду.
4. locations — список адресов из структурированного поля addresses.
5. Пустые строки и значения-заглушки («Не указано», «Нет данных», «-») означают, что сведения не заданы.

Что искать:
- missing: конкретных сведений нет ни в структурированных полях, ни в текстах.
- ambiguity: формулировка допускает разные трактовки (например, «гибкий график» без дней, часов и правил; «выполнение различных поручений»; «зарплата по договорённости»).
- contradiction: два значения в карточке несовместимы (например, schedule = «Полный рабочий день», а в тексте «вахта 15/15»; duty = «полностью дистанционно», а в requirements — «ежедневное присутствие в офисе»).

Жёсткие правила:
1. Если конкретные сведения уже есть в структурированном поле, отсутствие их повтора в тексте — НЕ проблема.
2. Не выдумывай факты, не добавляй сведения, которых нет в карточке, и не ссылайся на ТК РФ или другие нормы.
3. Для missing: укажи topic и source_field, quote оставь пустой строкой "", в problem объясни, каких именно сведений не хватает.
4. missing используй только когда сведений нет вовсе. Если поле заполнено, но неполно (например, указан только регион вместо адреса или только город без улицы), используй ambiguity.
5. Для contradiction: в quote приведи ОБА конфликтующих значения дословно из карточки.
6. Для ambiguity: в quote приведи дословную неоднозначную формулировку из карточки.
7. quote должен быть дословным фрагментом карточки. Не переформатируй числа, валюту и не добавляй пояснений в quote.
8. Одно наблюдение — одно замечание. Не дублируй одну и ту же проблему под разными темами. Не больше двух замечаний на одну тему.
9. Не считай недостатком отсутствие объяснения, от чего зависит позиция внутри зарплатной вилки: это нормальная практика, а не пропуск.
10. recommendation — конкретное действие для работодателя: что именно дописать или согласовать.
11. Если замечаний нет, верни {"issues": []}.

Отвечай СТРОГО валидным JSON-объектом по схеме:
{
  "issues": [
    {
      "topic": "role_title | responsibilities | requirements | compensation | schedule | location | employment_type",
      "source_field": "имя поля из карточки вакансии",
      "quote": "дословная цитата или пустая строка для missing",
      "issue_type": "missing | ambiguity | contradiction",
      "problem": "суть проблемы",
      "recommendation": "конкретная рекомендация для работодателя"
    }
  ]
}
"""


def _extract_json_object(text: str) -> dict[str, Any]:
    """Извлекает JSON-объект из ответа модели (в т.ч. из ```json ... ```)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end >= start:
        cleaned = cleaned[start : end + 1]

    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("Ответ модели не является JSON-объектом")
    return parsed


class VacancyAIAnalyzer:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 60.0,
        max_text_chars: int = 1500,
        retries: int = 3,
        backoff_seconds: float = 1.5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_text_chars = max_text_chars
        self.retries = max(1, retries)
        self.backoff_seconds = backoff_seconds

    # ------------------------------------------------------------------
    # Транспорт
    # ------------------------------------------------------------------
    def _post(self, client: httpx.Client, payload: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
            except httpx.TransportError as exc:
                last_error = exc
                logger.warning("LLM: сетевая ошибка (попытка %s/%s): %s", attempt + 1, self.retries, exc)
            else:
                if response.status_code in {429, 500, 502, 503, 504} and attempt < self.retries - 1:
                    last_error = httpx.HTTPStatusError(
                        f"HTTP {response.status_code}", request=response.request, response=response
                    )
                    logger.warning(
                        "LLM: ответ %s (попытка %s/%s), повтор",
                        response.status_code,
                        attempt + 1,
                        self.retries,
                    )
                else:
                    return response

            if attempt < self.retries - 1:
                time.sleep(self.backoff_seconds * (2**attempt))

        raise httpx.TransportError(f"LLM недоступен после {self.retries} попыток: {last_error}")

    def _call_llm(self, messages: list[dict[str, str]]) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.1,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }

        # connect — отдельно и короче: при «мёртвой» сети ждём 10 с, а не весь таймаут
        timeout = httpx.Timeout(self.timeout_seconds, connect=min(10.0, self.timeout_seconds))
        with httpx.Client(timeout=timeout) as client:
            response = self._post(client, payload, headers)
            # Не все OpenAI-совместимые провайдеры поддерживают response_format
            if response.status_code in {400, 422}:
                logger.info("LLM: провайдер не принял response_format, повтор без него")
                payload.pop("response_format", None)
                response = self._post(client, payload, headers)
            response.raise_for_status()
            data = response.json()

        choices = data.get("choices")
        if not choices or not isinstance(choices, list):
            raise ValueError("Пустой список choices в ответе LLM")

        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Пустой content в ответе LLM")
        return content

    # ------------------------------------------------------------------
    # Анализ
    # ------------------------------------------------------------------
    def analyze_ai(self, payload: dict[str, Any]) -> list[IssueNote]:
        """Только шаг LLM: карточка уже нормализована правилами."""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Проанализируй вакансию и верни JSON строго по схеме.\n"
                    "Карточка вакансии:\n"
                    + json.dumps(payload, ensure_ascii=False, indent=2)
                ),
            },
        ]

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                raw_text = self._call_llm(messages)
                parsed_dict = _extract_json_object(raw_text)
                validated = VacancyAnalysisResult.model_validate(parsed_dict)
                return validated.issues
            except (httpx.HTTPError, ValueError, ValidationError, json.JSONDecodeError) as exc:
                last_error = exc
                logger.warning(
                    "LLM: некорректный ответ для вакансии %s (попытка %s/2): %s",
                    payload.get("id"),
                    attempt + 1,
                    exc,
                )
                if attempt == 0:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Предыдущий ответ не прошёл проверку формата или JSON. "
                                'Верни ТОЛЬКО валидный JSON-объект вида {"issues": [...]} без пояснений.'
                            ),
                        }
                    )

        raise RuntimeError(
            f"Не удалось получить корректный разбор от ИИ для вакансии {payload.get('id')}: {last_error}"
        ) from last_error

    def analyze_vacancy(
        self, record: VacancyRecord, payload: dict[str, Any] | None = None
    ) -> list[IssueNote]:
        """Гибридный шаг: детерминированные правила + LLM."""
        if payload is None:
            payload = build_vacancy_payload(record, max_text_chars=self.max_text_chars)
        rule_issues = collect_rule_issues(payload)
        ai_issues = self.analyze_ai(payload)
        return merge_issues(rule_issues, ai_issues, payload=payload)


def analyze_with_rules(record: VacancyRecord, max_text_chars: int = 1500) -> list[IssueNote]:
    """Резервный путь, если LLM недоступна: только детерминированные правила."""
    payload = build_vacancy_payload(record, max_text_chars=max_text_chars)
    return collect_rule_issues(payload)
