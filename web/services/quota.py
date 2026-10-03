"""The credit budget for live checks: refuse one that would spend past today's share or the month's reserve."""
from datetime import datetime, time, timedelta, timezone

from web.config import Settings
from web.models import Quota
from web.repositories.quota import CreditMeter, Usage
from web.services.problems import Problem

SAMPLE_ITEMS = 10  # the sample has 10 items, a visitor's count of checks left is in checks of that size
TODAY_USED_UP = ("Today's free quota for live checks is used up. It resets at midnight UTC. "
                 "The recorded sample run still works.")


def month_used_up(resets: datetime | None) -> str:
    when = f"on {resets.day} {resets:%B}" if resets else "at the start of next month"
    return f"This month's free quota for live checks is used up. It resets {when}. The recorded sample run still works."


class QuotaUsedUp(Problem):
    def __init__(self, message: str):
        super().__init__(429, message, quota=True)


class QuotaService:
    def __init__(self, settings: Settings, meter: CreditMeter, counts: bool):
        """counts is False when checks spend no credits, like TabPFN on this machine."""
        self.settings, self.meter, self.counts = settings, meter, counts

    def cost(self, items: int) -> int:
        """Credits a check of this many items spends: a forecast call per item, and an honesty test call per item."""
        return items * (2 if self.settings.run_honesty else 1) * self.settings.credits_per_call

    def _month_left(self, usage: Usage | None, since_read: int) -> int | None:
        if usage is None or usage.limit is None:
            return None
        return usage.limit - usage.used - since_read - self.settings.credit_reserve

    def _refusal(self, usage: Usage | None, since_read: int, spent: int, cost: int) -> str | None:
        month_left = self._month_left(usage, since_read)
        if month_left is not None and month_left < cost:
            return month_used_up(usage.resets)
        if spent + cost > self.settings.daily_credits:
            return TODAY_USED_UP
        return None

    def reserve(self, items: int) -> int:
        """Records a live check's cost, or raises QuotaUsedUp. Returns the cost, to release if the check is refused."""
        if not self.counts:
            return 0

        def decide(usage, since_read, spent):
            cost = self.cost(items)
            refusal = self._refusal(usage, since_read, spent, cost)
            if refusal:
                raise QuotaUsedUp(refusal)
            return cost
        return self.meter.reserve(decide)

    def release(self, cost: int) -> None:
        if cost:
            self.meter.release(cost)

    def provider_refused(self) -> str:
        """The message for a check the provider itself refused for quota or rate reasons."""
        self.meter.forget()
        usage, since_read, _ = self.meter.snapshot()
        month_left = self._month_left(usage, since_read)
        return month_used_up(usage.resets) if month_left is not None and month_left <= 0 else TODAY_USED_UP

    def outlook(self) -> Quota | None:
        """How many sample-sized live checks are left today, for the page. None when checks cost nothing."""
        if not self.counts:
            return None
        usage, since_read, spent = self.meter.snapshot()
        sample = self.cost(SAMPLE_ITEMS)
        today_left = max(0, self.settings.daily_credits - spent)
        month_left = self._month_left(usage, since_read)
        now = self.meter.now()
        midnight = datetime.combine(now.date() + timedelta(days=1), time(), tzinfo=timezone.utc)
        if usage is None and today_left >= sample:
            return Quota(live_checks_left_today=None, resets=midnight.isoformat())  # the month cannot be read
        credits, resets = today_left, midnight
        if month_left is not None and month_left < today_left:
            credits, resets = max(0, month_left), usage.resets or midnight
        return Quota(live_checks_left_today=credits // sample, resets=resets.isoformat())
