"""The stock check, as app.py does it: read the two reports, predict, decide what to order, word it."""
import json
import logging
import threading
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field

import pandas as pd

from intake import SALES_COLUMNS, STOCK_COLUMNS, DataProblem
from predictor import check_enough_days, forecast, split_rare_items
from reorder import items_without_stock, reorder_plan
from web.config import LANGUAGES, Settings
from web.models import Capped, ColumnQuestion, JobStatus, NoteLine, NoteResult, PlanLine, StockCheckResult
from web.repositories.forecasting import ForecastingGateway, ModelNotConfigured
from web.repositories.jobs import Job, JobStore, LimitReached
from web.repositories.samples import SampleRepository
from web.repositories.uploads import FileRepository, FileTooBig
from web.services.honesty import HonestyService
from web.services.problems import Problem
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


class _Progress:
    """One bar over stages that overlap. Model calls fill it to 0.85, the stage shown is the earliest unfinished one."""

    def __init__(self, jobs: JobStore, job: Job, testing: bool):
        self.jobs, self.job = jobs, job
        self.done = {"predicting": 0.0, **({"testing": 0.0} if testing else {})}
        self.worded = False
        self.lock = threading.Lock()

    def _show(self) -> None:
        waiting = [stage for stage, done in self.done.items() if done < 1]
        if waiting:
            share = sum(self.done.values()) / len(self.done)
            self.jobs.update(self.job, stage=waiting[0], progress=round(0.02 + 0.83 * share, 3))
        elif self.worded:
            self.jobs.update(self.job, stage="wording", progress=0.87)

    def part(self, stage: str):
        def report(done: float) -> None:
            with self.lock:
                self.done[stage] = max(self.done[stage], done)
                self._show()
        return report

    def wording(self) -> None:
        with self.lock:
            self.worded = True
            self._show()


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
                 forecasting: ForecastingGateway, wording: WordingService, honesty: HonestyService):
        self.settings, self.files, self.samples, self.jobs = settings, files, samples, jobs
        self.forecasting, self.wording, self.honesty = forecasting, wording, honesty

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

    def start(self, owner: str, sales: Upload | None, stock: Upload | None, use_sample: bool,
              columns: str | None, language: str | None) -> str:
        language = self._language(language)
        try:
            provider = self.forecasting.provider()
        except ModelNotConfigured as problem:
            raise Problem(503, str(problem)) from None
        try:
            self.jobs.check(owner, *self._limits())
            read = self.read(sales, stock, use_sample, columns)
            job = self.jobs.create(owner, language, *self._limits())
        except LimitReached as problem:
            raise Problem(429, str(problem)) from None
        log.info("check %s started: %d days, %d items", job.id, read.sales["date"].nunique(), read.sales["item"].nunique())
        threading.Thread(target=self.run, args=(job, read, provider), daemon=True).start()
        return job.id

    # running it, in its own thread

    def _cap(self, regular: pd.DataFrame) -> tuple[pd.DataFrame, Capped | None]:
        """Keeps the top sellers when there are more items than a hosted check allows."""
        limit, items = self.settings.max_items, regular["item"].nunique()
        if limit is None or items <= limit:
            return regular, None
        totals = regular.groupby("item")["qty_sold"].sum().sort_values(ascending=False, kind="stable")
        return regular[regular["item"].isin(totals.index[:limit])], Capped(kept=limit, dropped=items - limit)

    def _predict_test_word(self, job: Job, regular: pd.DataFrame, stock: pd.DataFrame, provider):
        """The forecast and the honesty test share one pool of model calls. Wording starts once the plan exists."""
        progress = _Progress(self.jobs, job, self.settings.run_honesty)

        def test(pool=None):
            tested = progress.part("testing")
            honesty = self.honesty.run(regular, provider, tested, pool)
            if self.settings.run_honesty:
                tested(1.0)  # a test that could not run is finished too
            return honesty

        workers = getattr(provider, "workers", 1)
        if workers <= 1:
            # a model on this machine runs one call at a time, so the stages go one after another
            predicted = forecast(regular, self.settings.horizon_days, progress.part("predicting"), provider)
            plan = reorder_plan(predicted, stock)
            honesty = test()
            progress.wording()
            return predicted, plan, honesty, self.wording.lines(plan, job.language)
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
            plan = reorder_plan(predicted, stock)
            progress.wording()
            lines = self.wording.lines(plan, job.language)  # gemma words while the honesty test finishes
            return predicted, plan, testing.result(), lines

    def run(self, job: Job, read: ReadFiles, provider) -> None:
        try:
            self.jobs.update(job, state="running", stage="reading", progress=0.02)
            regular, rare_items = split_rare_items(read.sales)
            regular, capped = self._cap(regular)
            predicted, plan, (honesty, honesty_problem), lines = self._predict_test_word(job, regular, read.stock,
                                                                                         provider)
            checked = set(predicted["item"]) & set(read.stock["item"])
            result = StockCheckResult(
                shop_name=read.shop_name, language=job.language,
                days_of_sales=read.sales["date"].nunique(),
                first_day=read.sales["date"].min().date(), last_day=read.sales["date"].max().date(),
                items_sold=read.sales["item"].nunique(), stock_items=len(read.stock), items_checked=len(checked),
                notes=read.notes, plan=self._plan_lines(plan, lines), worded_by_ai=sum(ai is not None for _, ai in lines),
                enough_stock=sorted(checked - set(plan["item"])), not_checked=items_without_stock(predicted, read.stock),
                rare_items=rare_items, capped=capped, honesty=honesty, honesty_problem=honesty_problem)
            with job.lock:
                job.notes = {job.language: lines}
            self.jobs.update(job, plan=plan, result=result, state="done", progress=1.0)
            log.info("check %s done: %d plan lines, %d worded by ai", job.id, len(plan), result.worded_by_ai)
        except DataProblem as problem:
            self.jobs.update(job, state="failed", problem=str(problem))
        except Exception as error:
            # only the kind of error is logged, never the shop's data
            log.warning("check %s failed: %s", job.id, type(error).__name__)
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
        return JobStatus(state=job.state, stage=job.stage, progress=job.progress, result=job.result, problem=job.problem)

    def note(self, job_id: str, language: str | None) -> NoteResult:
        """The finished plan worded in another language. Each language is worded once and kept."""
        language = self._language(language)
        job = self._job(job_id)
        if job.state != "done":
            raise Problem(409, "This check has not finished yet.")
        with job.lock:
            if language not in job.notes:
                job.notes[language] = self.wording.lines(job.plan, language)
            lines = job.notes[language]
        return NoteResult(lines=[ai or plain for plain, ai in lines], worded_by_ai=sum(ai is not None for _, ai in lines),
                          plan=[NoteLine(item=item, line=ai or plain, line_plain=plain, by_ai=ai is not None)
                                for item, (plain, ai) in zip(job.plan["item"], lines)])
