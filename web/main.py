"""The hosted app. Run with: uvicorn web.main:app --port 10000"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.formparsers import MultiPartParser

from web.config import FILE_TYPES, Settings
from web.controllers import checks, meta, orders, samples
from web.repositories.forecasting import ForecastingGateway
from web.repositories.jobs import JobStore
from web.repositories.samples import SampleRepository
from web.repositories.uploads import FileRepository
from web.repositories.wording import WordingGateway
from web.services.honesty import HonestyService
from web.services.orders import OrderService
from web.services.problems import Problem
from web.services.stock_check import StockCheckService
from web.services.wording import WordingService

VIEWS = Path(__file__).parent / "views"


def create_app(settings: Settings | None = None, make_forecaster=None, ask_gemma=None, clock=None) -> FastAPI:
    """make_forecaster and ask_gemma replace the two models, for tests."""
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.forecasting.warm_up()  # a background thread, /healthz answers at once
        yield

    app = FastAPI(title="Shop Stock Predictor", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json",
                  lifespan=lifespan)
    state = app.state
    state.settings = settings
    state.forecasting = ForecastingGateway(settings.tabpfn_backend, settings.tabpfn_token, settings.forecast_workers,
                                           make_forecaster)
    state.wording_gateway = WordingGateway(settings.gemma_backend, settings.gemma_api_key, settings.gemma_model, ask_gemma)
    state.samples = SampleRepository()
    jobs = JobStore(settings.job_minutes * 60, **({"clock": clock} if clock else {}))
    state.jobs = jobs
    state.checks = StockCheckService(settings, FileRepository(FILE_TYPES, settings.max_upload_mb), state.samples, jobs,
                                     state.forecasting, WordingService(state.wording_gateway),
                                     HonestyService(settings.run_honesty, settings.horizon_days))
    state.orders = OrderService()

    # two files at the cap plus the form fields. uploads stay in memory: the spool only goes to disk past this size
    body_cap = 2 * settings.max_upload_bytes + 256 * 1024
    MultiPartParser.spool_max_size = body_cap

    @app.middleware("http")
    async def cap_body(request: Request, call_next):
        if request.method == "POST":
            length = request.headers.get("content-length")
            if length is None and "multipart" in request.headers.get("content-type", ""):
                return JSONResponse({"problem": "Uploads need a Content-Length header."}, status_code=411)
            if length is not None and (not length.isdigit() or int(length) > body_cap):
                return JSONResponse({"problem": f"Each file can be at most {settings.max_upload_mb:g} MB."},
                                    status_code=413)
        return await call_next(request)

    @app.exception_handler(Problem)
    async def problem(_: Request, error: Problem):
        return JSONResponse(error.body, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def bad_request(_: Request, error: RequestValidationError):
        fields = ", ".join(".".join(map(str, part["loc"][1:])) or "body" for part in error.errors())
        return JSONResponse({"problem": f"Some of the request is missing or wrong: {fields}."}, status_code=422)

    for router in (meta.router, samples.router, checks.router, orders.router):
        app.include_router(router)
    app.mount("/", StaticFiles(directory=VIEWS, html=True), name="views")
    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
