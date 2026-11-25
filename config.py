from dataclasses import dataclass
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from market import (
    KALININGRAD_AVG_SALARY,
    KALININGRAD_MEDIAN_SALARY,
    KALININGRAD_REGION,
)

load_dotenv()

DEFAULT_API_URL = "http://opendata.trudvsem.ru/api/v1/vacancies/region/39"

logger = logging.getLogger(__name__)


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Некорректное значение %s=%r, использую %s", name, raw, default)
        return default
    return max(minimum, value)


def _env_float(name: str, default: float, minimum: float = 1.0) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Некорректное значение %s=%r, использую %s", name, raw, default)
        return default
    return max(minimum, value)


@dataclass(frozen=True)
class AppConfig:
    run_mode: str
    api_base_url: str
    max_vacancies: int
    max_pages: int
    api_page_size: int
    local_json_path: Path
    report_output_path: Path
    schedule_interval_minutes: int
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout_seconds: float
    max_text_chars: int
    log_file: Path
    good_vacancies_path: Path
    good_max_issues: int
    market_region: str
    market_avg_salary: int
    market_median_salary: int


def load_config() -> AppConfig:
    run_mode = _env_str("RUN_MODE", "check").lower()
    if run_mode not in {"check", "live", "schedule"}:
        logger.warning("Неизвестный RUN_MODE=%r, использую 'check'", run_mode)
        run_mode = "check"

    max_vacancies = _env_int("MAX_VACANCIES", 40)
    return AppConfig(
        run_mode=run_mode,
        api_base_url=_env_str("API_BASE_URL", DEFAULT_API_URL),
        max_vacancies=max_vacancies,
        max_pages=_env_int("MAX_PAGES", 3),
        # Размер страницы меньше лимита, чтобы гарантированно пройти 2+ страницы
        api_page_size=min(_env_int("API_PAGE_SIZE", 20), 100, max_vacancies),
        local_json_path=Path(_env_str("LOCAL_JSON_PATH", "data/kantkod_sample_vacancies.json")),
        report_output_path=Path(_env_str("REPORT_OUTPUT_PATH", "output/report.json")),
        schedule_interval_minutes=_env_int("SCHEDULE_INTERVAL_MINUTES", 60),
        llm_base_url=_env_str("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/"),
        llm_api_key=_env_str("LLM_API_KEY", "ollama"),
        llm_model=_env_str("LLM_MODEL", "qwen2.5:7b"),
        llm_timeout_seconds=_env_float("LLM_TIMEOUT_SECONDS", 60),
        max_text_chars=_env_int("MAX_TEXT_CHARS", 1500, minimum=200),
        log_file=Path(_env_str("LOG_FILE", "logs/run.log")),
        # Доп. сценарий: вакансии с минимальным числом замечаний
        good_vacancies_path=Path(
            _env_str("GOOD_VACANCIES_OUTPUT_PATH", "output/good_vacancies.json")
        ),
        good_max_issues=_env_int("GOOD_MAX_ISSUES", 1, minimum=0),
        # Рыночный ориентир для компонента «оплата к средней по региону»
        market_region=_env_str("MARKET_REGION", KALININGRAD_REGION),
        market_avg_salary=_env_int("MARKET_AVG_SALARY", KALININGRAD_AVG_SALARY, minimum=1000),
        market_median_salary=_env_int(
            "MARKET_MEDIAN_SALARY", KALININGRAD_MEDIAN_SALARY, minimum=1000
        ),
    )
