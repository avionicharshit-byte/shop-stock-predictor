"""Read the billing app's bill-wise "Sales report" PDF.

Usage: python -m core.pdf_to_sales report.pdf sales.csv
"""
import re
import subprocess
import sys

import pandas as pd

BILL = re.compile(r"^\s*\d{2}:\d{2} [AP]M · (\d{2} \w{3} \d{4})\s+BILL #")
ENTRY = re.compile(r"^\s*\d{2}:\d{2} [AP]M · \d{2} \w{3} \d{4}")
ITEM = re.compile(r"^\s*(.+?)\s{2,}(\d+(?:\.\d+)?)\s+₹([\d,.]+)\s+₹([\d,.]+)\s*$")
SHOP = re.compile(r"^\s*(.+?)\s{2,}Sales report")
PDFTOTEXT_TIMEOUT = 60  # seconds


def read_bills(pdf: bytes) -> tuple[pd.DataFrame, str | None]:
    """Returns (one row per bill line, shop name)."""
    # a timeout, since a hosted upload can be any file at all
    text = subprocess.run(["pdftotext", "-layout", "-", "-"], input=pdf, capture_output=True, check=True,
                          timeout=PDFTOTEXT_TIMEOUT).stdout.decode()
    rows, bill_date, shop_name = [], None, None
    for line in text.splitlines():
        if shop_name is None and (shop := SHOP.match(line)):
            shop_name = shop.group(1)
        if bill := BILL.match(line):
            bill_date = pd.to_datetime(bill.group(1), format="%d %b %Y")
        elif ENTRY.match(line):
            bill_date = None  # a quick sale or expense, it has no item lines
        elif bill_date is not None and (item := ITEM.match(line)):
            rows.append({"date": bill_date, "item": item.group(1).strip(), "qty_sold": float(item.group(2))})
    return pd.DataFrame(rows, columns=["date", "item", "qty_sold"]), shop_name


if __name__ == "__main__":
    bills, _ = read_bills(open(sys.argv[1], "rb").read())
    bills.groupby(["date", "item"], as_index=False)["qty_sold"].sum().to_csv(sys.argv[2], index=False)
    print(f"{len(bills)} bill lines, {bills['item'].nunique()} items, {bills['date'].nunique()} days -> {sys.argv[2]}")
