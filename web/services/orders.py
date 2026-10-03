"""The order list for the supplier, as WhatsApp text, a printable page or receipt printer bytes."""
import pandas as pd

from order_sheet import order_html, order_text
from thermal import PAPER_WIDTHS, order_receipt
from web.models import OrderRequest, ReceiptRequest


class OrderService:
    def __init__(self, today=pd.Timestamp.today):
        self.today = today

    @staticmethod
    def _order(request: OrderRequest) -> pd.DataFrame:
        lines = [line for line in request.lines if line.quantity > 0]
        return pd.DataFrame({"Item": [line.item for line in lines], "Quantity": [line.quantity for line in lines]})

    def text(self, request: OrderRequest) -> str:
        return order_text(request.shop_name, request.address, self._order(request), self.today())

    def sheet(self, request: OrderRequest) -> str:
        return order_html(request.shop_name, request.address, self._order(request), self.today())

    def receipt(self, request: ReceiptRequest) -> bytes:
        """ESC/POS bytes, the same ones the laptop version sends to the printer."""
        return order_receipt(request.shop_name, request.address, self._order(request), self.today(),
                             PAPER_WIDTHS[request.paper])
