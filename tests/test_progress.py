import pandas as pd
from conftest import FakeTabPFN, make_client, wait_for

from web.config import Settings
from web.repositories.jobs import JobStore
from web.repositories.wording import WordingGateway
from web.services.stock_check import _Progress, _TimedProvider, closest_to_running_out
from web.services.wording import WordingService


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class SlowProvider:
    """Each call takes `seconds` on the fake clock."""
    workers = 2

    def __init__(self, clock, seconds):
        self.clock, self.seconds = clock, seconds

    def guess(self, history, future_dates):
        self.clock.now += self.seconds
        return "guess"

    def finish(self):
        pass


def test_progress_moves_per_call_and_eta_follows_the_measured_average():
    clock = FakeClock()
    jobs = JobStore(60, clock=clock)
    job = jobs.create("me", "Hindi", 10, 10, 10)
    progress = _Progress(jobs, job, testing=True, items=4, workers=2)
    provider = _TimedProvider(SlowProvider(clock, 2.0), progress)
    predicting, testing = progress.part("predicting"), progress.part("testing")
    shown = []
    for number in range(1, 5):
        assert provider.guess(None, None) == "guess"
        predicting(number / 4)
        shown.append((job.progress, job.predicted, job.eta_seconds))
    # no time left is guessed from fewer than three calls
    assert shown[0][2] is None and shown[1][2] is None
    # three calls of 2 s each, 5 calls left, 2 at once: 5 * 2 / 2
    assert shown[2] == (round(0.02 + 0.83 * 0.75 / 2, 3), 3, 5)
    assert job.estimate_seconds == 8  # 8 calls * 2 s / 2 at once
    # 4 left, all test calls
    assert (job.stage, job.predicted, job.tested, job.eta_seconds) == ("testing", 4, 0, 4)
    for number in range(1, 5):
        provider.guess(None, None)
        testing(number / 4)
    assert job.tested == 4 and job.eta_seconds == 0 and job.progress == 0.85
    assert [value for value, _, _ in shown] == sorted(value for value, _, _ in shown)


def test_eta_uses_only_the_calls_left_when_fewer_than_the_pool():
    clock = FakeClock()
    jobs = JobStore(60, clock=clock)
    job = jobs.create("me", "Hindi", 10, 10, 10)
    progress = _Progress(jobs, job, testing=False, items=10, workers=8)
    provider = _TimedProvider(SlowProvider(clock, 3.0), progress)
    report = progress.part("predicting")
    for number in range(1, 10):
        provider.guess(None, None)
        report(number / 10)
    assert job.eta_seconds == 3  # one call left runs alone, not shared by 8


def test_status_carries_counts_and_eta():
    seen = []
    client = make_client()
    jobs = client.app.state.jobs
    update = jobs.update

    def watch(job, **changes):
        update(job, **changes)
        seen.append((job.predicted, job.tested, job.eta_seconds))
    jobs.update = watch
    job_id = client.post("/api/checks", data={"use_sample": "true"}).json()["job_id"]
    status = wait_for(client, job_id)
    assert status["state"] == "done" and status["items"] == 10
    assert status["predicted"] == 10 and status["tested"] == 10 and status["eta_seconds"] == 0
    predicted = [count for count, _, _ in seen]
    assert predicted == sorted(predicted) and set(range(1, 11)) <= set(predicted)


def test_wording_stops_at_its_deadline():
    clock, asked = FakeClock(), []

    def slow_gemma(prompt):
        asked.append(prompt)
        clock.now += 20
        return prompt.rsplit("Alert: ", 1)[1].splitlines()[0]
    gateway = WordingGateway("google", None, "m", ask=slow_gemma, clock=clock)
    gateway_workers_one = type("One", (WordingGateway,), {"workers": property(lambda self: 1)})
    gateway.__class__ = gateway_workers_one
    plan = pd.DataFrame({"item": ["a", "b", "c"], "stock_left": [0, 1, 2], "likely_to_sell": [5, 5, 5],
                         "busy_week": [7, 7, 7], "runs_out_on": pd.to_datetime(["2026-10-04"] * 3),
                         "order_packs": [1, 1, 1], "order_units": [6, 6, 6], "pack_size": [6, 6, 6]})
    lines, _ = WordingService(gateway).worded(plan, "English", seconds=30)
    # the first two asks fit in 30 s, the third would start with 0 s left and stays plain
    assert len(asked) == 2 and [ai is not None for _, ai in lines] == [True, True, False]


def _sales(rows):
    days = pd.date_range("2026-08-01", periods=30)
    return pd.DataFrame([{"date": day, "item": item, "qty_sold": qty} for item, qty in rows for day in days])


def test_cap_picks_low_days_of_stock_over_big_sellers():
    sales = _sales([("big", 50), ("big2", 40), ("small", 2), ("small2", 1)])
    stock = pd.DataFrame({"item": ["big", "big2", "small", "small2"], "stock_left": [5000, 4000, 1, 3]})
    assert closest_to_running_out(sales, stock, 2) == ["small", "small2"]


def test_cap_never_puts_zero_pace_ahead_of_a_real_risk():
    sales = _sales([("busy", 5), ("still", 0)])
    stock = pd.DataFrame({"item": ["busy", "still"], "stock_left": [1000, 0]})
    assert closest_to_running_out(sales, stock, 1) == ["busy"]


def test_cap_skips_items_without_stock():
    sales = _sales([("listed", 1), ("unlisted", 9)])
    stock = pd.DataFrame({"item": ["listed"], "stock_left": [100]})
    assert closest_to_running_out(sales, stock, 2) == ["listed"]


def test_cap_ties_go_to_the_bigger_seller():
    sales = _sales([("a", 1), ("b", 2)])
    stock = pd.DataFrame({"item": ["a", "b"], "stock_left": [10, 20]})
    assert closest_to_running_out(sales, stock, 1) == ["b"]


def test_cap_uses_the_faster_of_recent_and_whole_pace():
    days = pd.date_range("2026-07-01", periods=60)
    # "late" sold nothing for a month, then 10 a day: its recent pace counts, not the lower whole-file one
    sales = pd.DataFrame([{"date": day, "item": "late", "qty_sold": 10 if i >= 30 else 0} for i, day in enumerate(days)]
                         + [{"date": day, "item": "even", "qty_sold": 6} for day in days])
    stock = pd.DataFrame({"item": ["late", "even"], "stock_left": [60, 50]})
    assert closest_to_running_out(sales, stock, 1) == ["late"]  # 6 days left against 8.3


def test_cap_notice_text():
    client = make_client(max_items=3)
    result = wait_for(client, client.post("/api/checks", data={"use_sample": "true"}).json()["job_id"])["result"]
    assert result["capped"]["text"] == ("This online check looked at the 3 items closest to running out, picked by a "
                                        "plain average. 7 other items were left out.")


def test_no_cap_leaves_every_item():
    assert Settings.from_env({"MAX_ITEMS": "0"}).max_items is None
    client = make_client(max_items=None)
    result = wait_for(client, client.post("/api/checks", data={"use_sample": "true"}).json()["job_id"])["result"]
    assert result["capped"] is None and result["items_checked"] == 10


def test_timed_provider_passes_everything_else_through():
    provider = _TimedProvider(FakeTabPFN(), None)
    assert provider.workers == 4 and provider.finish() is None
