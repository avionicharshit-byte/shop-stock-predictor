"""The stock check, as app.py does it: read the two reports, predict, decide what to order, word it."""
import json
import logging
import threading
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field

import pandas as pd

from core.intake import SALES_COLUMNS, STOCK_COLUMNS, DataProblem
from core.predictor import check_enough_days, forecast, split_rare_items
from core.reorder import items_without_stock, reorder_plan
from web.config import LANGUAGES, Settings
from web.models import (Capped, ColumnQuestion, JobStatus, NoteLine, NoteResult, PlanLine, RecordedSample,
                        StockCheckResult)
from web.repositories.forecasting import ForecastingGateway, ModelNotConfigured
from web.repositories.jobs import Job, JobStore, LimitReached
from web.repositories.quota import looks_like_quota
from web.repositories.samples import SampleRepository
from web.repositories.uploads import FileRepository, FileTooBig
from web.services.honesty import HonestyService
from web.services.problems import Problem
from web.services.quota import QuotaService
from web.services.wording import WordingService

log = logging.getLogger(__name__)
# the same questions app.py asks when a column is not recognised
ASK = {"date": "the date", "item": "the item name", "qty_sold": "how many were sold", "stock_left": "how many are left"}
NEEDED = {"sales": SALES_COLUMNS, "stock": STOCK_COLUMNS}


@dataclass
class Upload:
    name: str
    data: bytes


@dataclass
class ReadFiles:
    sales: pd.DataFrame
    stock: pd.DataFrame
    shop_name: str | None
    notes: list[str] = field(default_factory=list)


def _column_choices(columns: str | None) -> dict[str, dict[str, str]]:
    if not columns:
        return {"sales": {}, "stock": {}}
    try:
        chosen = json.loads(columns)
        assert isinstance(chosen, dict)
        return {kind: {str(name): str(column) for name, column in (chosen.get(kind) or {}).items()}
                for kind in NEEDED}
    except (ValueError, AssertionError, AttributeError):
        raise Problem(422, 'columns must be JSON like {"sales": {"date": "Bill Date"}, "stock": {}}.') from None


MIN_CALLS_FOR_ETA = 3  # calls timed before the page is given a time left
CAP_RECENT_DAYS = 28


class _Progress:
    """One bar over stages that overlap, moved by every finished model call. The stage shown is the earliest unfinished.

    eta_seconds is the calls left times the measured average call, divided by the calls running at once.
    """

    def __init__(self, jobs: JobStore, job: Job, testing: bool, items: int = 0, workers: int = 1):
        self.jobs, self.job, self.clock = jobs, job, jobs.clock
        self.items, self.workers = items, max(1, workers)
        self.done = {"predicting": 0.0, **({"testing": 0.0} if testing else {})}
        self.seconds: list[float] = []  # how long each finished call took
        self.worded = False
        self.lock = threading.Lock()
        # the first calls take seconds, so the page says they started
        jobs.update(job, stage="predicting", items=items)

    def counts(self) -> dict:
        """predicted, tested, eta_seconds and estimate_seconds for the status. Call with the lock held."""
        left = sum(self.items * (1 - done) for done in self.done.values())
        counts = {"items": self.items, "predicted": round(self.done["predicting"] * self.items),
                  "tested": round(self.done.get("testing", 0.0) * self.items), "eta_seconds": None,
                  "estimate_seconds": None}
        if len(self.seconds) >= MIN_CALLS_FOR_ETA:
            average = sum(self.seconds) / len(self.seconds)
            calls = self.items * len(self.done)
            counts["estimate_seconds"] = round(calls * average / min(self.workers, calls)) if calls else 0
            counts["eta_seconds"] = round(left * average / min(self.workers, left)) if left >= 0.5 else 0
        return counts

    def _show(self) -> None:
        waiting = [stage for stage, done in self.done.items() if done < 1]
        if waiting:
            share = sum(self.done.values()) / len(self.done)
            self.jobs.update(self.job, stage=waiting[0], progress=round(0.02 + 0.83 * share, 3), **self.counts())
        elif self.worded:
            self.jobs.update(self.job, stage="wording", progress=0.87, **self.counts())
        else:
            self.jobs.update(self.job, progress=0.85, **self.counts())  # the last call, before wording starts

    def part(self, stage: str):
        def report(done: float) -> None:
            with self.lock:
                self.done[stage] = max(self.done[stage], done)
                self._show()
        return report

    def timed(self, seconds: float) -> None:
        with self.lock:
            self.seconds.append(seconds)

    def wording(self) -> None:
        with self.lock:
            self.worded = True
            self._show()


class _TimedProvider:
    """Times each model call for the time-left estimate. Everything else goes to the real provider."""

    def __init__(self, provider, progress: _Progress):
        self.provider, self.progress = provider, progress

    def guess(self, history, future_dates):
        started = self.progress.clock()
        answer = self.provider.guess(history, future_dates)
        self.progress.timed(self.progress.clock() - started)
        return answer

    def __getattr__(self, name):
        return getattr(self.provider, name)


def closest_to_running_out(regular: pd.DataFrame, stock: pd.DataFrame, limit: int) -> list[str]:
    """The items with the fewest days of stock left at a plain average pace, ties to the bigger seller.

    plain arithmetic only picks which items the model looks at. every forecast shown still comes from TabPFN.
    """
    last = regular["date"].max()
    recent = regular[regular["date"] > last - pd.Timedelta(days=CAP_RECENT_DAYS)]
    pace = pd.concat([recent.groupby("item")["qty_sold"].mean(), regular.groupby("item")["qty_sold"].mean()],
                     axis=1).max(axis=1)
    totals = regular.groupby("item")["qty_sold"].sum()
    left = stock.set_index("item")["stock_left"]
    candidates = sorted(set(pace.index) & set(left.index))  # no stock entry, nothing to plan
    # a zero pace never runs out, so it goes last
    days = {item: left[item] / pace[item] if pace[item] > 0 else float("inf") for item in candidates}
    return sorted(candidates, key=lambda item: (days[item], -totals[item], item))[:limit]


class _QueueFirst:
    """Passes calls to a shared pool and says when the last of `items` calls is queued."""

    def __init__(self, pool: Executor, items: int):
        self.pool, self.left, self.queued = pool, items, threading.Event()

    def submit(self, *args, **kwargs):
        future = self.pool.submit(*args, **kwargs)
        self.left -= 1
        if self.left <= 0:
            self.queued.set()
        return future


class StockCheckService:
    def __init__(self, settings: Settings, files: FileRepository, samples: SampleRepository, jobs: JobStore,
                 forecasting: ForecastingGateway, wording: WordingService, honesty: HonestyService, quota: QuotaService):
        self.settings, self.files, self.samples, self.jobs = settings, files, samples, jobs
        self.forecasting, self.wording, self.honesty, self.quota = forecasting, wording, honesty, quota

    # starting a check

    def _language(self, language: str | None) -> str:
        language = language or LANGUAGES[0]
        if language not in LANGUAGES:
            raise Problem(422, f"Language must be one of: {', '.join(LANGUAGES)}.")
        return language

    def _limits(self) -> tuple:
        return self.settings.checks_per_hour, self.settings.running_per_ip, self.settings.running_total

    def read(self, sales: Upload | None, stock: Upload | None, use_sample: bool, columns: str | None) -> ReadFiles:
        """Opens and cleans both reports, or answers 422 with the problem or the columns to ask about."""
        chosen = _column_choices(columns)
        try:
            if use_sample:
                sales_file, stock_file = self.samples.file("sales"), self.samples.file("stock")
            elif not (sales and stock):
                missing = "both reports" if not (sales or stock) else "the stock report too" if sales else "the sales report too"
                raise DataProblem(f"Add {missing} to continue.")
            else:
                sales_file = self.files.accept("sales", sales.name, sales.data)
                stock_file = self.files.accept("stock", stock.name, stock.data)
            opened = {"sales": self.files.open_sales(sales_file), "stock": self.files.open_stock(stock_file)}
            questions = []
            for kind, needed in NEEDED.items():
                columns_found = [str(column) for column in opened[kind].rows.columns]
                for name, column in chosen[kind].items():
                    if name not in needed or column not in columns_found:
                        raise DataProblem(f"There is no column {column!r} to use as {ASK.get(name, name)} "
                                          f"in the {kind} file.")
                questions += [ColumnQuestion(file=kind, name=name, ask=f"In the {kind} file, which column has {ASK[name]}?",
                                             options=columns_found)
                              for name in opened[kind].unknown(needed) if name not in chosen[kind]]
            if questions:
                raise Problem(422, None, needs_columns=[question.model_dump() for question in questions])
            sales_table = self.files.clean_sales(opened["sales"], chosen["sales"])
            stock_table = self.files.clean_stock(opened["stock"], chosen["stock"])
            check_enough_days(sales_table)
        except FileTooBig as problem:
            raise Problem(413, str(problem)) from None
        except DataProblem as problem:
            raise Problem(422, str(problem)) from None
        return ReadFiles(sales_table, stock_table, opened["stock"].shop_name or opened["sales"].shop_name,
                         [*opened["sales"].notes, *opened["stock"].notes])

    def recorded_sample(self) -> RecordedSample | None:
        return self.samples.recorded() if self.settings.recorded_sample else None

    @staticmethod
    def _recorded_in(recorded: RecordedSample, language: str) -> StockCheckResult:
        """The saved sample result with its note in the asked language. Every language was worded when it was saved."""
        result = recorded.result.model_copy(update={"recorded": True, "recorded_on": recorded.recorded_on})
        note = recorded.notes.get(language)
        if note is None or language == result.language:
            return result
        plan = [line.model_copy(update={"line": worded.line, "by_ai": worded.by_ai})
                for line, worded in zip(result.plan, note.plan)]
        return result.model_copy(update={"language": language, "plan": plan, "worded_by_ai": note.worded_by_ai,
                                         "gemma_limited": note.gemma_limited})

    def _items_to_check(self, read: ReadFiles) -> int:
        regular, _ = split_rare_items(read.sales)
        return self._cap(regular, read.stock)[0]["item"].nunique()

    def start(self, owner: str, sales: Upload | None, stock: Upload | None, use_sample: bool,
              columns: str | None, language: str | None) -> Job:
        language = self._language(language)
        recorded = self.recorded_sample() if use_sample else None
        if recorded is not None:
            # the sample never changes, so it answers from the saved run: no model calls, no credits
            return self.jobs.add_recorded(owner, language, self._recorded_in(recorded, language), recorded)
        try:
            provider = self.forecasting.provider()
        except ModelNotConfigured as problem:
            raise Problem(503, str(problem)) from None
        try:
            self.jobs.check(owner, *self._limits())
            reading = self.jobs.clock()
            read = self.read(sales, stock, use_sample, columns)
            reading = self.jobs.clock() - reading
            cost = self.quota.reserve(self._items_to_check(read))
            try:
                job = self.jobs.create(owner, language, *self._limits())
            except LimitReached:
                self.quota.release(cost)
                raise
        except LimitReached as problem:
            raise Problem(429, str(problem)) from None
        log.info("check %s started: %d days, %d items, %d credits, files read in %.1f s", job.id,
                 read.sales["date"].nunique(), read.sales["item"].nunique(), cost, reading)
        threading.Thread(target=self.run, args=(job, read, provider), daemon=True).start()
        return job

    # running it, in its own thread

    def _cap(self, regular: pd.DataFrame, stock: pd.DataFrame) -> tuple[pd.DataFrame, Capped | None]:
        """Keeps the items closest to running out when there are more than a hosted check allows."""
        limit, items = self.settings.max_items, regular["item"].nunique()
        if limit is None or items <= limit:
            return regular, None
        kept = closest_to_running_out(regular, stock, limit)
        dropped = items - len(kept)
        return regular[regular["item"].isin(kept)], Capped(
            kept=len(kept), dropped=dropped,
            text=f"This online check looked at the {len(kept)} items closest to running out, picked by a plain "
                 f"average. {dropped} other {'item was' if dropped == 1 else 'items were'} left out.")

    def _predict_test_word(self, job: Job, regular: pd.DataFrame, stock: pd.DataFrame, provider):
        """The forecast and the honesty test share one pool of model calls. Wording starts once the plan exists."""
        workers = getattr(provider, "workers", 1)
        progress = _Progress(self.jobs, job, self.settings.run_honesty, regular["item"].nunique(), workers)
        provider = _TimedProvider(provider, progress)
        clock, began, took = self.jobs.clock, self.jobs.clock(), {}

        def test(pool=None):
            tested = progress.part("testing")
            honesty = self.honesty.run(regular, provider, tested, pool)
            if self.settings.run_honesty:
                tested(1.0)  # a test that could not run is finished too
            took["honesty"] = clock() - began
            return honesty

        def word(plan):
            started = clock()
            worded = self.wording.worded(plan, job.language, self.settings.wording_seconds)
            took["wording"] = clock() - started
            return worded

        def log_times():
            calls = progress.seconds
            log.info("check %s model calls: %d averaging %.1f s, forecast %.0f s, honesty %.0f s, wording %.0f s",
                     job.id, len(calls), sum(calls) / max(len(calls), 1), took.get("forecast", 0),
                     took.get("honesty", 0), took.get("wording", 0))

        if workers <= 1:
            # a model on this machine runs one call at a time, so the stages go one after another
            predicted = forecast(regular, self.settings.horizon_days, progress.part("predicting"), provider)
            took["forecast"] = clock() - began
            plan = reorder_plan(predicted, stock)
            honesty = test()
            progress.wording()
            worded = word(plan)
            log_times()
            return predicted, plan, honesty, worded
        with ThreadPoolExecutor(workers) as calls, ThreadPoolExecutor(1) as side:
            # the forecast queues its calls first, so the plan and its wording are ready early
            first = _QueueFirst(calls, regular["item"].nunique())

            def test_after_forecast():
                first.queued.wait()
                return test(calls)
            testing = side.submit(test_after_forecast)
            try:
                predicted = forecast(regular, self.settings.horizon_days, progress.part("predicting"), provider, first)
            except BaseException:
                calls.shutdown(cancel_futures=True)
                raise
            finally:
                first.queued.set()
            took["forecast"] = clock() - began
            plan = reorder_plan(predicted, stock)
            progress.wording()
            worded = word(plan)  # gemma words while the honesty test finishes
            honesty = testing.result()
            log_times()
            return predicted, plan, honesty, worded

    def run(self, job: Job, read: ReadFiles, provider) -> None:
        try:
            started = self.jobs.clock()
            self.jobs.update(job, state="running", stage="reading", progress=0.02)
            regular, rare_items = split_rare_items(read.sales)
            regular, capped = self._cap(regular, read.stock)
            predicted, plan, (honesty, honesty_problem), (lines, limited) = self._predict_test_word(
                job, regular, read.stock, provider)
            checked = set(predicted["item"]) & set(read.stock["item"])
            result = StockCheckResult(
                shop_name=read.shop_name, language=job.language,
                days_of_sales=read.sales["date"].nunique(),
                first_day=read.sales["date"].min().date(), last_day=read.sales["date"].max().date(),
                items_sold=read.sales["item"].nunique(), stock_items=len(read.stock), items_checked=len(checked),
                notes=read.notes, plan=self._plan_lines(plan, lines), worded_by_ai=sum(ai is not None for _, ai in lines),
                enough_stock=sorted(checked - set(plan["item"])), not_checked=items_without_stock(predicted, read.stock),
                rare_items=rare_items, capped=capped, honesty=honesty, honesty_problem=honesty_problem,
                gemma_limited=limited)
            with job.lock:
                job.notes = {job.language: (lines, limited)}
            self.jobs.update(job, plan=plan, result=result, state="done", progress=1.0)
            log.info("check %s done in %.0f s: %d plan lines, %d worded by ai", job.id, self.jobs.clock() - started,
                     len(plan), result.worded_by_ai)
        except DataProblem as problem:
            self.jobs.update(job, state="failed", problem=str(problem))
        except Exception as error:
            # only the kind of error is logged, never the shop's data
            log.warning("check %s failed: %s", job.id, type(error).__name__)
            if looks_like_quota(error):
                self.jobs.update(job, state="failed", quota=True, problem=self.quota.provider_refused())
            else:
                self.jobs.update(job, state="failed", problem="The prediction service did not answer. "
                                                             "Please try again in a few minutes.")

    @staticmethod
    def _plan_lines(plan: pd.DataFrame, lines: list[tuple[str, str | None]]) -> list[PlanLine]:
        return [PlanLine(item=row.item, stock_left=int(row.stock_left), likely_to_sell=int(row.likely_to_sell),
                         busy_week=int(row.busy_week), runs_out_on=row.runs_out_on.date(),
                         runs_out_weekday=f"{row.runs_out_on:%A}", already_out=bool(row.stock_left == 0),
                         order_packs=int(row.order_packs), order_units=int(row.order_units),
                         line=ai or plain, line_plain=plain, by_ai=ai is not None)
                for row, (plain, ai) in zip(plan.itertuples(), lines)]

    # reading it back

    def _job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise Problem(404, f"No such check. Checks are kept for {self.settings.job_minutes} minutes, run it again.")
        return job

    def status(self, job_id: str) -> JobStatus:
        job = self._job(job_id)
        return JobStatus(state=job.state, stage=job.stage, progress=job.progress, result=job.result, problem=job.problem,
                         quota=job.quota, items=job.items, predicted=job.predicted, tested=job.tested,
                         eta_seconds=job.eta_seconds, estimate_seconds=job.estimate_seconds)

    def note(self, job_id: str, language: str | None) -> NoteResult:
        """The finished plan worded in another language. Each language is worded once and kept."""
        language = self._language(language)
        job = self._job(job_id)
        if job.state != "done":
            raise Problem(409, "This check has not finished yet.")
        if job.recorded is not None:
            return self._recorded_note(job.recorded, language)
        with job.lock:
            if language not in job.notes:
                job.notes[language] = self.wording.worded(job.plan, language)
            lines, limited = job.notes[language]
        return NoteResult(lines=[ai or plain for plain, ai in lines], worded_by_ai=sum(ai is not None for _, ai in lines),
                          plan=[NoteLine(item=item, line=ai or plain, line_plain=plain, by_ai=ai is not None)
                                for item, (plain, ai) in zip(job.plan["item"], lines)],
                          gemma_limited=limited)

    @staticmethod
    def _recorded_note(recorded: RecordedSample, language: str) -> NoteResult:
        note = recorded.notes.get(language)
        if note is not None:
            return note
        plan = recorded.result.plan  # a language missing from the recording gets the plain sentences
        return NoteResult(lines=[line.line_plain for line in plan], worded_by_ai=0,
                          plan=[NoteLine(item=line.item, line=line.line_plain, line_plain=line.line_plain, by_ai=False)
                                for line in plan])
