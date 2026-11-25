import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from models import VacancyRecord

logger = logging.getLogger(__name__)

MAX_PAGE_LIMIT = 100


class FetchError(Exception):
    """Ошибка при получении данных из API или локального файла."""


def _extract_vacancies_list(payload: Any) -> list[dict[str, Any]]:
    """Достаёт список словарей вакансий из разных возможных форматов ответа."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    candidates: list[Any] = []
    results = payload.get("results")
    if isinstance(results, dict):
        candidates.append(results.get("vacancies"))
    elif isinstance(results, list):
        candidates.append(results)

    data = payload.get("data")
    if isinstance(data, dict):
        candidates.append(data.get("vacancies"))
    elif isinstance(data, list):
        candidates.append(data)

    candidates.append(payload.get("vacancies"))

    for cand in candidates:
        if isinstance(cand, list):
            return [item for item in cand if isinstance(item, dict)]

    if "vacancy" in payload and isinstance(payload["vacancy"], dict):
        return [payload["vacancy"]]

    return []


def _unwrap_vacancy(raw_item: dict[str, Any]) -> dict[str, Any]:
    """В API trudvsem вакансия лежит под ключом 'vacancy'."""
    if "vacancy" in raw_item and isinstance(raw_item["vacancy"], dict):
        return raw_item["vacancy"]
    return raw_item


def _extract_vacancy_id(vac: dict[str, Any], index: int) -> str:
    for key in ("id", "vacancy_id", "uuid", "code"):
        val = vac.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return f"unknown_{index}"


def _extract_vacancy_url(vac: dict[str, Any]) -> str | None:
    for key in ("vac_url", "url", "link", "vacancy_url", "href"):
        val = vac.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _to_records(raw_items: list[dict[str, Any]], limit: int, start_index: int = 0) -> list[VacancyRecord]:
    records: list[VacancyRecord] = []
    for idx, raw_item in enumerate(raw_items, start=start_index):
        if len(records) >= limit:
            break
        vac = _unwrap_vacancy(raw_item)
        records.append(
            VacancyRecord(
                vacancy_id=_extract_vacancy_id(vac, idx),
                url=_extract_vacancy_url(vac),
                raw_vacancy=vac,
            )
        )
    return records


def _deduplicate(records: list[VacancyRecord]) -> list[VacancyRecord]:
    """Один результат на ID вакансии, порядок сохраняется."""
    unique: dict[str, VacancyRecord] = {}
    for record in records:
        unique.setdefault(record.vacancy_id, record)
    return list(unique.values())


def load_from_local_json(json_path: Path, max_vacancies: int) -> list[VacancyRecord]:
    """Проверочный режим: читает локальный JSON без обращения к API."""
    if not json_path.exists():
        raise FetchError(f"Локальный файл не найден: {json_path}")

    try:
        content = json_path.read_text(encoding="utf-8")
        payload = json.loads(content)
    except Exception as exc:
        raise FetchError(f"Не удалось прочитать или распарсить JSON {json_path}: {exc}") from exc

    raw_items = _extract_vacancies_list(payload)
    if not raw_items:
        raise FetchError(f"В файле {json_path} не найдено ни одной вакансии")

    records = _deduplicate(_to_records(raw_items, limit=max_vacancies))
    logger.info("Локальный файл %s: %s вакансий (уникальных %s)", json_path, len(raw_items), len(records))
    return records


def _get_with_retries(
    client: httpx.Client,
    url: str,
    params: dict[str, Any],
    retries: int,
    backoff_seconds: float,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = client.get(url, params=params)
        except httpx.TransportError as exc:  # таймауты, DNS, обрывы соединения
            last_error = exc
            logger.warning("Сетевая ошибка (попытка %s/%s): %s", attempt + 1, retries, exc)
        else:
            if response.status_code in {429, 500, 502, 503, 504} and attempt < retries - 1:
                last_error = httpx.HTTPStatusError(
                    f"HTTP {response.status_code}", request=response.request, response=response
                )
                logger.warning(
                    "API ответил %s (попытка %s/%s), повтор",
                    response.status_code,
                    attempt + 1,
                    retries,
                )
            else:
                response.raise_for_status()
                return response

        if attempt < retries - 1:
            time.sleep(backoff_seconds * (2**attempt))

    raise FetchError(f"API недоступен после {retries} попыток: {last_error}")


def fetch_from_api(
    base_url: str,
    max_vacancies: int,
    page_size: int = 20,
    max_pages: int = 3,
    timeout_seconds: float = 20.0,
    retries: int = 3,
    backoff_seconds: float = 1.0,
) -> list[VacancyRecord]:
    """Загружает вакансии постранично через параметры offset/limit.

    API «Работа в России» игнорирует параметр page и по умолчанию отдаёт
    до 100 записей, поэтому пагинация выполняется только через offset.
    """
    if max_vacancies <= 0:
        raise FetchError("MAX_VACANCIES должен быть больше нуля")

    page_size = max(1, min(page_size, MAX_PAGE_LIMIT, max_vacancies))
    effective_pages = max(1, max_pages)

    headers = {
        "User-Agent": "TransparentVacancyMVP/1.0",
        "Accept": "application/json",
    }

    collected: dict[str, VacancyRecord] = {}
    offset = 0
    total_available: int | None = None
    pages_fetched = 0

    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds)),
            headers=headers,
            follow_redirects=True,
        ) as client:
            while len(collected) < max_vacancies and pages_fetched < effective_pages:
                limit = min(page_size, max_vacancies - len(collected), MAX_PAGE_LIMIT)
                params = {"limit": limit, "offset": offset}
                response = _get_with_retries(client, base_url, params, retries, backoff_seconds)
                payload = response.json()

                if total_available is None and isinstance(payload, dict):
                    meta = payload.get("meta")
                    if isinstance(meta, dict):
                        try:
                            total_available = int(meta.get("total"))
                        except (TypeError, ValueError):
                            total_available = None

                page_items = _extract_vacancies_list(payload)
                pages_fetched += 1
                logger.info(
                    "Страница %s: offset=%s, limit=%s, получено %s вакансий (всего доступно: %s)",
                    pages_fetched,
                    offset,
                    limit,
                    len(page_items),
                    total_available if total_available is not None else "н/д",
                )

                if not page_items:
                    break

                new_items = 0
                for idx, raw_item in enumerate(page_items, start=offset):
                    if len(collected) >= max_vacancies:
                        break
                    vac = _unwrap_vacancy(raw_item)
                    record = VacancyRecord(
                        vacancy_id=_extract_vacancy_id(vac, idx),
                        url=_extract_vacancy_url(vac),
                        raw_vacancy=vac,
                    )
                    if record.vacancy_id not in collected:
                        collected[record.vacancy_id] = record
                        new_items += 1

                offset += len(page_items)

                # API перестал отдавать новые записи — дальше листать бессмысленно
                if new_items == 0:
                    logger.info("Страница без новых вакансий, пагинация остановлена")
                    break

                if total_available is not None and offset >= total_available:
                    break

    except FetchError:
        raise
    except httpx.HTTPStatusError as exc:
        raise FetchError(
            f"API вернул ошибку {exc.response.status_code} при обращении к {base_url}"
        ) from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"Ошибка HTTP при обращении к API ({base_url}): {exc}") from exc
    except ValueError as exc:
        raise FetchError(f"API вернул некорректный JSON: {exc}") from exc
    except Exception as exc:
        raise FetchError(f"Непредвиденная ошибка при загрузке из API: {exc}") from exc

    records = list(collected.values())[:max_vacancies]
    logger.info(
        "Итого загружено %s уникальных вакансий со страниц: %s", len(records), pages_fetched
    )
    return records
