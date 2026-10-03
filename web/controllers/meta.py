from fastapi import APIRouter, Request

from web.config import LANGUAGES
from web.models import AppConfig, Limits

router = APIRouter()


@router.get("/healthz")
def health() -> dict:
    return {"ok": True}


@router.get("/api/config", response_model=AppConfig)
def config(request: Request) -> AppConfig:
    state = request.app.state
    settings = state.settings
    return AppConfig(tabpfn=state.forecasting.status(), gemma=state.wording_gateway.status(), languages=LANGUAGES,
                     limits=Limits(max_items=settings.max_items, max_upload_mb=settings.max_upload_mb,
                                   horizon_days=settings.horizon_days))
