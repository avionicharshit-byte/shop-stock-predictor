from collections.abc import Callable

import numpy as np
import pandas as pd
import torch
from tabpfn import TabPFNRegressor
from tabpfn.constants import ModelVersion
from tabpfn.errors import TabPFNOutOfMemoryError

from intake import DataProblem

HORIZON_DAYS = 7
MIN_DAYS_TO_PREDICT = 7
MIN_DAYS_SOLD = 3
BUSY_WEEK = 0.8  # the "busy week" level is one that sales stay under about 8 weeks in 10
CALENDAR = ["day_number", "day_of_week", "weekday_sin", "weekday_cos", "day_of_month"]


def split_rare_items(sales: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """An item sold on only a day or two has no pattern to learn, so it is set aside, not guessed."""
    days_sold = sales[sales["qty_sold"] > 0].groupby("item")["date"].nunique()
    regular = days_sold[days_sold >= MIN_DAYS_SOLD].index
    return sales[sales["item"].isin(regular)], sorted(set(sales["item"]) - set(regular))


def check_enough_days(sales: pd.DataFrame) -> None:
    days_covered = sales["date"].nunique()
    if days_covered < MIN_DAYS_TO_PREDICT:
        raise DataProblem(f"Need at least {MIN_DAYS_TO_PREDICT} days of sales to predict, this file covers {days_covered}.")


def _calendar(dates: pd.Series, first_day: pd.Timestamp) -> pd.DataFrame:
    # Prior Labs' own recipe for forecasting with TabPFN: no hand-made averages and no "yesterday's sales",
    # only where the day sits on the calendar. the weekday goes in as a circle so Sunday sits next to Monday
    weekday = dates.dt.dayofweek
    return pd.DataFrame({
        "day_number": (dates - first_day).dt.days, "day_of_week": weekday,
        "weekday_sin": np.sin(2 * np.pi * weekday / 7), "weekday_cos": np.cos(2 * np.pi * weekday / 7),
        "day_of_month": dates.dt.day})


def _guess_one_item(model: TabPFNRegressor, history: pd.DataFrame, future_dates: pd.Series) -> tuple:
    """(likely qty, busy-day qty) for each future day. TabPFN reads this item's past days and answers in one go."""
    first_day = history["date"].min()
    model.fit(_calendar(history["date"], first_day), history["qty_sold"])
    answer = model.predict(_calendar(future_dates, first_day), output_type="main", quantiles=[BUSY_WEEK])
    return np.clip(answer["mean"], 0, None), np.clip(answer["quantiles"][0], 0, None)


def forecast(history: pd.DataFrame, days: int = HORIZON_DAYS,
             on_progress: Callable[[float], None] | None = None) -> pd.DataFrame:
    """One row per item per future day: predicted_qty (most likely) and busy_qty (a busy day)."""
    check_enough_days(history)
    future_dates = pd.Series(pd.date_range(history["date"].max() + pd.Timedelta(days=1), periods=days))
    # v2 is the openly licensed TabPFN, newer versions need an account and a licence click
    model = TabPFNRegressor.create_default_for_version(ModelVersion.V2, ignore_pretraining_limits=True)
    guesses = []
    items = list(history.groupby("item"))
    for number, (item, item_history) in enumerate(items, 1):
        # each item gets its own small table. mixing all items in one table let big sellers
        # distort the small ones, and it lost to a plain average on a public dataset
        try:
            likely, busy = _guess_one_item(model, item_history, future_dates)
        except (TabPFNOutOfMemoryError, torch.OutOfMemoryError):
            # Gemma shares the same graphics card. when it is full, the slower main processor does the job
            torch.cuda.empty_cache()
            model = TabPFNRegressor.create_default_for_version(ModelVersion.V2, ignore_pretraining_limits=True, device="cpu")
            likely, busy = _guess_one_item(model, item_history, future_dates)
        guesses.append(pd.DataFrame({"date": future_dates, "item": item, "predicted_qty": likely, "busy_qty": busy}))
        if on_progress:
            on_progress(number / len(items))
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return pd.concat(guesses, ignore_index=True)


def weekly_need(predicted: pd.DataFrame) -> pd.DataFrame:
    """Per item: what will likely sell over the predicted days, and what a busy week would sell."""
    # adding up seven busy days should overshoot a busy week in theory. measured on hidden weeks it does not:
    # it covered 7 to 8 items in 10 on one month of a real shop and 66 percent on three months, short of the
    # 8 in 10 aimed for. a shrunk version covered only 6, so this stays. slow sellers are the gap
    return predicted.groupby("item").agg(likely=("predicted_qty", "sum"), busy_week=("busy_qty", "sum")).reset_index()


def honesty_test(sales: pd.DataFrame, days: int = HORIZON_DAYS,
                 on_progress: Callable[[float], None] | None = None) -> dict:
    # hide the last `days`, predict them, and score TabPFN against two guesses that need no AI
    days_of_sales = sales["date"].nunique()
    if days_of_sales < days + MIN_DAYS_TO_PREDICT:
        raise DataProblem(f"The honesty test needs {days + MIN_DAYS_TO_PREDICT} days of sales "
                          f"(a week to hide and a week to learn from), this file covers {days_of_sales}.")
    cutoff = sales["date"].max() - pd.Timedelta(days=days)
    history, hidden = sales[sales["date"] <= cutoff], sales[sales["date"] > cutoff]
    predicted = forecast(history, days, on_progress)
    result = hidden.merge(predicted, on=["date", "item"])
    result["plain"] = result["item"].map(history.groupby("item")["qty_sold"].mean())
    same_weekday = history.assign(weekday=history["date"].dt.dayofweek).groupby(["item", "weekday"])["qty_sold"].mean()
    result = result.assign(weekday=result["date"].dt.dayofweek).join(same_weekday.rename("same_weekday"), on=["item", "weekday"])
    result["same_weekday"] = result["same_weekday"].fillna(result["plain"])
    per_item = result.groupby("item").agg(really_sold=("qty_sold", "sum"), tabpfn=("predicted_qty", "sum"),
                                          plain=("plain", "sum"), same_weekday=("same_weekday", "sum"))
    busy_week = weekly_need(predicted).set_index("item")["busy_week"]
    scores = {"busy_week_covered": float((busy_week.reindex(per_item.index) >= per_item["really_sold"]).mean())}
    for guess, column in (("tabpfn", "predicted_qty"), ("plain", "plain"), ("same_weekday", "same_weekday")):
        scores[f"{guess}_daily_error"] = float((result["qty_sold"] - result[column]).abs().mean())
        scores[f"{guess}_weekly_error"] = float((per_item["really_sold"] - per_item[guess]).abs().mean())
    return {**scores, "per_item": per_item.round(0).astype(int).sort_values("really_sold", ascending=False).reset_index()}
