from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from web.models import OrderRequest, ReceiptRequest

router = APIRouter(prefix="/api/orders")


@router.post("/text")
def order_text(body: OrderRequest, request: Request) -> dict:
    return {"text": request.app.state.orders.text(body)}


@router.post("/sheet", response_class=HTMLResponse)
def order_sheet(body: OrderRequest, request: Request) -> HTMLResponse:
    return HTMLResponse(request.app.state.orders.sheet(body))


@router.post("/receipt", response_class=Response)
def order_receipt(body: ReceiptRequest, request: Request) -> Response:
    return Response(request.app.state.orders.receipt(body), media_type="application/octet-stream")
