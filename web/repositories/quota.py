"""Prior Labs credits: what the account has used this month, and what this server spent today."""
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone

log = logging.getLogger(__name__)
CACHE_SECONDS = 60
# "Currently, you have used 2000000 of the allowed limit of 20000000 credits.
#  The limit will reset at 2026-11-01 00:00:00 UTC."
USED = re.compile(r"used\s+([\d,_.]+)\s+of", re.IGNORECASE)
LIMIT = re.compile(r"limit\s+of\s+([\d,_.]+|unlimited)", re.IGNORECASE)
RESETS = re.compile(r"reset\s+(?:at|on)\s+(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?)", re.IGNORECASE)
# what a quota or rate refusal looks like in tabpfn-client's error messages
QUOTA_WORDS = re.compile(r"(?:http|status|code)\W{0,3}(?:429|402)\b|quota|credit|usage limit|rate.?limit|"
                         r"limit (?:exceeded|reached)|exceeded .{0,30}limit|payment required|too many requests",
                         re.IGNORECASE)


@dataclass(frozen=True)
class Usage:
    used: int
    limit: int | None  # None means unlimited
    resets: datetime | None  # utc


def _whole(text: str) -> int:
    return int(float(re.sub(r"[,_]", "", text)))


def parse_usage(text) -> Usage | None:
    """The numbers in tabpfn-client's usage sentence, or None when it no longer reads that way."""
    if not isinstance(text, str):
        return None
    used, limit = USED.search(text), LIMIT.search(text)
    if not (used and limit):
        return None
    try:
        whole_limit = None if limit.group(1).lower() == "unlimited" else _whole(limit.group(1))
        resets = RESETS.search(text)
        when = None
        if resets:
            when = datetime.fromisoformat(resets.group(1).replace(" ", "T")).replace(tzinfo=timezone.utc)
        return Usage(_whole(used.group(1)), whole_limit, when)
    except ValueError:
        return None


def looks_like_quota(error: BaseException | None) -> bool:
    """True when an error, or one it came from, reads like a quota, limit or payment refusal."""
    seen = 0
    while error is not None and seen < 5:
        if QUOTA_WORDS.search(f"{type(error).__name__} {error}"):
            return True
        error, seen = error.__cause__ or error.__context__, seen + 1
    return False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CreditMeter:
    """The account's usage, read at most once a minute, and this server's spend per utc day, in memory."""

    def __init__(self, read=None, now=utc_now, clock=time.monotonic, cache_seconds: float = CACHE_SECONDS):
        """read returns tabpfn-client's usage sentence. None means usage cannot be read here."""
        self.read, self.now, self.clock, self.cache_seconds = read, now, clock, cache_seconds
        self._lock = threading.Lock()
        self._usage: Usage | None = None
        self._read_at: float | None = None
        self._since_read = 0  # credits accepted after the last reading, not in it yet
        self._day: date | None = None
        self._spent = 0  # credits of the checks accepted today
        self._first_used: int | None = None  # the account's usage at the first reading today

    def _new_day(self) -> None:
        today = self.now().date()
        if today != self._day:
            self._day, self._spent = today, 0
            self._first_used = self._usage.used if self._usage else None

    def _refresh(self) -> None:
        if self.read is None:
            return
        if self._read_at is not None and self.clock() - self._read_at < self.cache_seconds:
            return
        try:
            usage = parse_usage(self.read())
            if usage is None:
                log.warning("prior labs usage could not be parsed, live checks are allowed")
        except Exception as error:  # unknown usage never blocks a check
            log.warning("prior labs usage could not be read: %s", type(error).__name__)
            usage = None
        self._usage, self._read_at, self._since_read = usage, self.clock(), 0
        if usage is not None and self._first_used is None:
            self._first_used = usage.used

    def forget(self) -> None:
        """Reads the usage again next time, after the provider refused a call."""
        with self._lock:
            self._read_at = None

    def _current(self) -> tuple[Usage | None, int, int]:
        self._new_day()
        self._refresh()
        spent = self._spent
        if self._usage is not None and self._first_used is not None:
            # memory is lost when the free host restarts, the account's own figure is not
            spent = max(spent, self._usage.used - self._first_used)
        return self._usage, self._since_read, spent

    def snapshot(self) -> tuple[Usage | None, int, int]:
        """(the account's usage or None, credits accepted since that reading, credits this server spent today)."""
        with self._lock:
            return self._current()

    def reserve(self, decide) -> int:
        """Runs decide(usage, since_read, spent_today) and records the cost it returns, under one lock.

        decide raises to refuse the check.
        """
        with self._lock:
            cost = decide(*self._current())
            self._spent += cost
            self._since_read += cost
            return cost

    def release(self, cost: int) -> None:
        """Gives back credits reserved for a check that was refused right after."""
        with self._lock:
            self._spent = max(0, self._spent - cost)
            self._since_read = max(0, self._since_read - cost)
