from typing import Literal

from pydantic import BaseModel, Field, field_validator

TopicType = Literal[
    "role_title",       # название должности
    "responsibilities", # обязанности
    "requirements",     # требования
    "compensation",     # оплата
    "schedule",         # график
    "location",         # место работы
    "employment_type",  # тип занятости
]

IssueType = Literal[
    "missing",       # пропуск сведений
    "ambiguity",     # неоднозначная формулировка
    "contradiction", # противоречие между данными/полями
]

DetectedBy = Literal["rule", "ai"]


class VacancyRecord(BaseModel):
    vacancy_id: str
    url: str | None = None
    raw_vacancy: dict


class IssueNote(BaseModel):
    topic: TopicType
    source_field: str = "unknown"
    quote: str = ""
    issue_type: IssueType
    problem: str
    recommendation: str
    # Каким слоем найдено замечание: детерминированным правилом или моделью.
    detected_by: DetectedBy = "ai"

    @field_validator("source_field", "quote", "problem", "recommendation", mode="before")
    @classmethod
    def _clean_strings(cls, value):
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("problem", "recommendation")
    @classmethod
    def _require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("поле не может быть пустым")
        return value


class VacancyCard(BaseModel):
    """Компактная карточка вакансии для отчёта и интерфейса."""

    job_name: str = ""
    company: str = ""
    region: str = ""
    salary_min: int | None = None
    salary_max: int | None = None
    salary_raw: str = ""
    currency: str = ""
    schedule: str = ""
    employment: str = ""
    locations: list[str] = Field(default_factory=list)


class ScorePenalty(BaseModel):
    points: int = Field(ge=0)
    reason: str


class ScoreComponent(BaseModel):
    """Один компонент итоговой оценки: понятность, график или оплата."""

    key: str
    label: str
    score: int = Field(ge=0, le=100)
    weight: float = Field(ge=0, le=1)
    weighted: float = Field(ge=0, le=100)
    note: str = ""


class ScheduleAnalysis(BaseModel):
    """Разбор графика: тип, ясность, комфортность и пояснения."""

    type: str
    type_label: str
    clarity: int = Field(ge=0, le=100)
    comfort: int = Field(ge=0, le=100)
    score: int = Field(ge=0, le=100)
    days_specified: bool = False
    hours_specified: bool = False
    notes: list[str] = Field(default_factory=list)


class SalaryAnalysis(BaseModel):
    """Сравнение зарплаты со средней по региону."""

    value: int | None = None
    benchmark: int
    median: int
    ratio: float | None = None
    score: int = Field(ge=0, le=100)
    label: str = ""
    notes: list[str] = Field(default_factory=list)


class VacancyReport(BaseModel):
    vacancy_id: str
    url: str | None = None
    vacancy: VacancyCard | None = None
    issues: list[IssueNote] = Field(default_factory=list)
    # Заполняется, только если анализ ИИ для вакансии не удался и остались
    # результаты детерминированных правил.
    analysis_error: str | None = None
    # Оценка готовности вакансии к публикации (см. scoring.py)
    score: int | None = Field(default=None, ge=0, le=100)
    grade: str | None = None
    penalties: list[ScorePenalty] = Field(default_factory=list)
    # Из чего сложилась итоговая оценка: понятность (50%), график (25%),
    # оплата относительно средней по региону (25%).
    score_components: list[ScoreComponent] = Field(default_factory=list)
    schedule_analysis: ScheduleAnalysis | None = None
    salary_analysis: SalaryAnalysis | None = None


class VacancyAnalysisResult(BaseModel):
    issues: list[IssueNote] = Field(default_factory=list)
