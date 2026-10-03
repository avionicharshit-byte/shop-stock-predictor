"""What the API answers."""
from datetime import date
from typing import Literal

from pydantic import BaseModel


class ColumnQuestion(BaseModel):
    """A column intake could not recognise. The client picks one of the options and sends the check again."""
    file: Literal["sales", "stock"]
    name: str
    ask: str
    options: list[str]


class PlanLine(BaseModel):
    item: str
    stock_left: int
    likely_to_sell: int
    busy_week: int
    runs_out_on: date
    runs_out_weekday: str
    already_out: bool
    order_packs: int
    order_units: int
    line: str  # worded by the AI when it passed the fact check, plain otherwise
    line_plain: str
    by_ai: bool


class GuessError(BaseModel):
    daily_error: float
    weekly_error: float


class HonestyRow(BaseModel):
    item: str
    really_sold: int
    tabpfn: int
    plain: int
    same_weekday: int


class HonestyResult(BaseModel):
    """The last week hidden, guessed, and compared. Errors are pieces per item, lower is better."""
    tabpfn: GuessError
    plain: GuessError
    same_weekday: GuessError
    busy_week_covered: float
    per_item: list[HonestyRow]


class Capped(BaseModel):
    kept: int
    dropped: int


class StockCheckResult(BaseModel):
    shop_name: str | None
    language: str
    days_of_sales: int
    first_day: date
    last_day: date
    items_sold: int  # items in the sales file
    stock_items: int  # items in the stock file
    items_checked: int  # items predicted that also have a stock count
    notes: list[str]
    plan: list[PlanLine]
    worded_by_ai: int
    enough_stock: list[str]
    not_checked: list[str]
    rare_items: list[str]
    capped: Capped | None
    honesty: HonestyResult | None
    honesty_problem: str | None


class JobStatus(BaseModel):
    state: Literal["queued", "running", "done", "failed"]
    stage: Literal["reading", "predicting", "testing", "wording"]
    progress: float
    result: StockCheckResult | None
    problem: str | None


class NoteLine(BaseModel):
    """One plan line worded in the asked language, with the same wording fields as PlanLine."""
    item: str
    line: str
    line_plain: str
    by_ai: bool


class NoteResult(BaseModel):
    lines: list[str]
    worded_by_ai: int
    plan: list[NoteLine]  # per line, in plan order, so the client can mark which ones Gemma worded


class Limits(BaseModel):
    max_items: int | None
    max_upload_mb: float
    horizon_days: int


class AppConfig(BaseModel):
    tabpfn: Literal["api", "local", "off"]
    gemma: Literal["google", "ollama", "off"]
    languages: list[str]
    limits: Limits
