"""The honesty test: hide the last week, guess it, compare with what really sold."""
import logging

import pandas as pd

from intake import DataProblem
from predictor import honesty_test
from web.models import GuessError, HonestyResult, HonestyRow

log = logging.getLogger(__name__)


class HonestyService:
    def __init__(self, enabled: bool, days: int):
        self.enabled, self.days = enabled, days

    def run(self, regular_sales: pd.DataFrame, provider, on_progress=None,
            pool=None) -> tuple[HonestyResult | None, str | None]:
        """(result, None), or (None, why it did not run). pool is shared with the forecast when given."""
        if not self.enabled:
            return None, "The honesty test is switched off on this server."
        try:
            test = honesty_test(regular_sales, self.days, on_progress, provider, pool)
        except DataProblem as problem:
            return None, str(problem)
        except Exception as error:  # a network or service failure should not lose the plan itself
            log.warning("honesty test failed: %s", type(error).__name__)
            return None, "The honesty test could not run this time. The plan above is not affected."
        return HonestyResult(
            busy_week_covered=test["busy_week_covered"],
            per_item=[HonestyRow(**row) for row in test["per_item"].to_dict("records")],
            **{guess: GuessError(daily_error=test[f"{guess}_daily_error"], weekly_error=test[f"{guess}_weekly_error"])
               for guess in ("tabpfn", "plain", "same_weekday")}), None
