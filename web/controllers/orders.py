from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web.models import OrderRequest

router = APIRouter(prefix="/api/orders")


@router.post("/text")
def order_text(body: OrderRequest, request: Request) -> dict:
    return {"text": request.app.state.orders.text(body)}


@router.post("/sheet", response_class=HTMLResponse)
def order_sheet(body: OrderRequest, request: Request) -> HTMLResponse:
    return HTMLResponse(request.app.state.orders.sheet(body))
