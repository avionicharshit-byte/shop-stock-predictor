"""Every setting of the hosted app, read from the environment in one place."""
import os
from dataclasses import dataclass
from pathlib import Path

from core.predictor import HORIZON_DAYS

ROOT = Path(__file__).resolve().parent.parent
LANGUAGES = ["Hindi", "English", "Hinglish"]  # the first is the default
FILE_TYPES = (".pdf", ".xlsx", ".xls", ".csv")


def _flag(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _number(value: str | None, default):
    return type(default)(value) if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    tabpfn_backend: str = "api"  # api: Prior Labs' hosted TabPFN, local: TabPFN v2 on this machine
    tabpfn_token: str | None = None
    gemma_backend: str = "off"  # google, ollama or off (plain sentences)
    gemma_api_key: str | None = None
    gemma_model: str = "gemma-4-26b-a4b-it"
    run_honesty: bool = True
    max_items: int | None = 25  # None means no cap. a 25-item check costs 250,000 prior labs credits
    max_upload_mb: float = 5.0
    horizon_days: int = HORIZON_DAYS
    wording_seconds: float = 30  # gemma's whole share of one check, lines not worded by then stay plain
    forecast_workers: int = 8  # prior labs calls at once per check, forecast and honesty test together
    checks_per_hour: int = 6  # per visitor
    running_per_ip: int = 2
    running_total: int = 4  # the free host has 512 MB, so only a few checks run at once
    job_minutes: int = 30  # finished checks are dropped after this
    recorded_sample: bool = True  # the sample answers from data/sample_result.json, no model calls
    credits_per_call: int = 5000  # one prior labs fit and predict on model v2 at these table sizes
    credit_reserve: int = 500_000  # live checks stop when the month's credits would drop below this
    daily_credits: int = 2_000_000  # what this server may spend per utc day
    port: int = 10000

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb * 1024 * 1024)

    @classmethod
    def from_env(cls, env=None) -> "Settings":
        env = os.environ if env is None else env
        tabpfn_backend = (env.get("TABPFN_BACKEND") or "api").strip().lower()
        gemma_key = env.get("GEMMA_API_KEY") or None
        gemma_backend = (env.get("GEMMA_BACKEND") or ("google" if gemma_key else "off")).strip().lower()
        # the cap keeps a hosted check short. on the shop's own machine nothing is capped unless asked
        max_items = env.get("MAX_ITEMS")
        if max_items in (None, ""):
            max_items = None if tabpfn_backend == "local" else cls.max_items
        else:
            max_items = int(max_items) or None  # 0 means no cap
        return cls(
            tabpfn_backend=tabpfn_backend,
            tabpfn_token=env.get("TABPFN_TOKEN") or None,
            gemma_backend=gemma_backend,
            gemma_api_key=gemma_key,
            gemma_model=env.get("GEMMA_MODEL") or cls.gemma_model,
            run_honesty=_flag(env.get("RUN_HONESTY"), True),
            max_items=max_items,
            max_upload_mb=_number(env.get("MAX_UPLOAD_MB"), cls.max_upload_mb),
            forecast_workers=_number(env.get("FORECAST_WORKERS"), cls.forecast_workers),
            wording_seconds=_number(env.get("WORDING_SECONDS"), cls.wording_seconds),
            checks_per_hour=_number(env.get("CHECKS_PER_HOUR"), cls.checks_per_hour),
            running_per_ip=_number(env.get("RUNNING_PER_IP"), cls.running_per_ip),
            running_total=_number(env.get("RUNNING_TOTAL"), cls.running_total),
            job_minutes=_number(env.get("JOB_MINUTES"), cls.job_minutes),
            recorded_sample=_flag(env.get("RECORDED_SAMPLE"), True),
            credits_per_call=_number(env.get("CREDITS_PER_CALL"), cls.credits_per_call),
            credit_reserve=_number(env.get("CREDIT_RESERVE"), cls.credit_reserve),
            daily_credits=_number(env.get("DAILY_CREDITS"), cls.daily_credits),
            port=_number(env.get("PORT"), cls.port),
        )
