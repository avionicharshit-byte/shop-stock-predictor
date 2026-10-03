"""The order list the shopkeeper sends to a supplier: as WhatsApp text and as a printable page."""
from html import escape

import pandas as pd


def order_text(shop_name: str, address: str, order: pd.DataFrame, date: pd.Timestamp) -> str:
    header = [f"Order from {shop_name}" if shop_name else "Order", address, f"{date:%d %b %Y}"]
    lines = [f"{number}. {row.Item} - {row.Quantity:g} pieces" for number, row in enumerate(order.itertuples(), 1)]
    return "\n".join([*filter(None, header), "", *lines])


def order_html(shop_name: str, address: str, order: pd.DataFrame, date: pd.Timestamp) -> str:
    rows = "".join(
        f"<tr><td>{number}</td><td>{escape(str(row.Item))}</td><td class='qty'>{row.Quantity:g}</td><td></td></tr>"
        for number, row in enumerate(order.itertuples(), 1))
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Order list</title>
<style>
  body {{ font-family: system-ui, sans-serif; color: #111; background: #fff; margin: 24px; }}
  h1 {{ font-size: 22px; margin: 0; }}
  p {{ margin: 4px 0; color: #444; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
  th, td {{ border: 1px solid #999; padding: 6px 10px; text-align: left; font-size: 14px; }}
  th {{ background: #eee; }}
  .qty {{ text-align: right; }}
  .print {{ text-align: center; margin-bottom: 20px; }}
  button {{ padding: 12px 36px; font-size: 16px; font-weight: 600; cursor: pointer; color: #fff;
            background: #0F766E; border: 0; border-radius: 8px; }}
  button:hover {{ background: #0B5F58; }}
  @media print {{ .print {{ display: none; }} body {{ margin: 0; }} }}
</style></head>
<body>
  <div class="print"><button onclick="window.print()">Print this order list</button></div>
  <h1>{escape(shop_name) or "Order list"}</h1>
  <p>{escape(address)}</p>
  <p>Order list, {date:%d %b %Y}</p>
  <table>
    <tr><th>#</th><th>Item</th><th>Quantity (pieces)</th><th>Rate (supplier fills in)</th></tr>
    {rows}
  </table>
</body></html>"""
