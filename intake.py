"""Take the shop's files as they come and turn them into clean sales and stock tables.

Safe things are fixed quietly (header names, spacing, date styles, title rows).
Anything that would need a guess is reported or asked about, never invented.
"""
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pdfplumber

from pdf_to_sales import read_bills

# header names seen in billing-app exports, most trusted first
SALES_COLUMNS = {
    "date": ["date", "bill date", "sale date", "invoice date", "day"],
    "item": ["item", "item name", "product", "product name", "particulars", "description", "name"],
    "qty_sold": ["qty_sold", "qty sold", "quantity sold", "sold qty", "units sold", "qty", "quantity", "units"],
}
STOCK_COLUMNS = {
    "item": SALES_COLUMNS["item"],
    "stock_left": ["stock_left", "stock left", "closing qty", "closing stock", "current stock", "in stock",
                   "available", "balance qty", "stock", "qty", "quantity"],
}
STOCK_EXTRAS = {"pack_size": ["pack_size", "pack size", "pack"], "status": ["status"]}
NOT_GOODS = {"service charge", "previous balance", "gst", "item 1", "discount", "round off"}
HEADER_SEARCH_ROWS = 20


class DataProblem(ValueError):
    """Something wrong with an uploaded file, worded so the shopkeeper can fix it."""


@dataclass
class OpenedFile:
    rows: pd.DataFrame
    found: dict[str, str | None]  # what we need -> the column that holds it, None if we could not tell
    shop_name: str | None = None
    notes: list[str] = field(default_factory=list)

    def unknown(self, needed: dict) -> list[str]:
        return [name for name in needed if self.found.get(name) is None]


def _simplify(header) -> str:
    return re.sub(r"[^a-z0-9]", "", str(header).lower())


def _file_name(file) -> str:
    return getattr(file, "name", str(file))


def _file_bytes(file) -> bytes:
    return file.getvalue() if hasattr(file, "getvalue") else Path(file).read_bytes()


def _pdf_tables(data: bytes) -> pd.DataFrame:
    """Every table row in the PDF, page after page."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        rows = [row for page in pdf.pages for table in page.extract_tables() for row in table]
    if not rows:
        raise DataProblem("I could not find a table in this PDF. It may be a scan or a photo. "
                          "Export the report as Excel or CSV instead.")
    return pd.DataFrame(rows).replace("", None)


def _read_grid(file) -> pd.DataFrame:
    """Every cell as found, with no header chosen yet."""
    data, name = _file_bytes(file), _file_name(file).lower()
    if not data.strip():
        raise DataProblem(f"{Path(name).name} is empty.")
    try:
        if name.endswith(".pdf"):
            return _pdf_tables(data)
        if name.endswith((".xlsx", ".xls")):
            return pd.read_excel(io.BytesIO(data), header=None)
        return pd.read_csv(io.BytesIO(data), header=None, dtype=str, skip_blank_lines=False)
    except DataProblem:
        raise
    except Exception:
        raise DataProblem(f"Could not open {Path(name).name}. Use a PDF, Excel or CSV export.") from None


def _open_table(file, needed: dict, extras: dict | None = None) -> OpenedFile:
    grid = _read_grid(file)
    wanted = {name: [_simplify(alias) for alias in aliases] for name, aliases in {**needed, **(extras or {})}.items()}
    known = {alias for aliases in wanted.values() for alias in aliases}
    # reports often start with a title, shop name and date range. the header is the first row that looks like one
    hits = [sum(_simplify(cell) in known for cell in row) for row in grid.head(HEADER_SEARCH_ROWS).itertuples(index=False)]
    header_row = next((index for index, count in enumerate(hits) if count >= 2), 0)
    shop_name = next((str(cell).split(":", 1)[1].strip() for cell in grid.head(header_row)[0]
                      if str(cell).lower().startswith("shop:")), None)
    header = grid.iloc[header_row]
    rows = grid.iloc[header_row + 1:]
    rows = rows[~rows.eq(header).all(axis=1)].reset_index(drop=True)  # a PDF repeats the header on every page
    rows.columns = [" ".join(str(cell).split()) for cell in header]
    rows = rows.loc[:, [column not in ("nan", "None") for column in rows.columns]].dropna(how="all")
    found, taken = {}, set()
    for name, aliases in wanted.items():
        match = next((column for alias in aliases for column in rows.columns
                      if _simplify(column) == alias and column not in taken), None)
        found[name] = match
        taken.add(match)
    if all(found[name] is None for name in needed):
        raise DataProblem(f"I could not find {' or '.join(needed)} columns in {Path(_file_name(file)).name}. "
                          "Is this the right report?")
    return OpenedFile(rows, found, shop_name)


def _to_dates(values: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(values):
        return values
    text = values.astype(str).str.strip()
    # 2026-09-03 is read as written. anything else is read day first, the Indian way (03-09-2026 = 3 Sep)
    year_first = text.str.match(r"\d{4}-")
    dates = pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns]")
    dates[year_first] = pd.to_datetime(text[year_first], errors="coerce", format="ISO8601")
    dates[~year_first] = pd.to_datetime(text[~year_first], errors="coerce", dayfirst=True, format="mixed")
    return dates


def _looks_like_dates(values: pd.Series) -> bool:
    if pd.to_numeric(values, errors="coerce").notna().mean() > 0.5:
        return False
    return _to_dates(values.dropna().head(50)).notna().mean() > 0.8


def open_sales(file) -> OpenedFile:
    if _file_name(file).lower().endswith(".pdf"):
        # the billing app's bill-wise report has its own layout. any other PDF is read as plain tables below
        bills, shop_name = read_bills(_file_bytes(file))
        if not bills.empty:
            return OpenedFile(bills, {name: name for name in SALES_COLUMNS}, shop_name)
    opened = _open_table(file, SALES_COLUMNS)
    if opened.found["date"] is None:
        opened.found["date"] = next((column for column in opened.rows.columns
                                     if column not in opened.found.values() and _looks_like_dates(opened.rows[column])), None)
    if opened.found["date"] is None and not any(_looks_like_dates(opened.rows[column]) for column in opened.rows.columns):
        if any(_simplify(column) in map(_simplify, STOCK_COLUMNS["stock_left"][:6]) for column in opened.rows.columns):
            raise DataProblem("This looks like the stock file, not the sales file. Are the two files swapped?")
        raise DataProblem("This sales file has no dates, only a total per item. To see when things run out I need "
                          "sales with a date on each row: the bill-wise or day-wise sales report.")
    return opened


def open_stock(file) -> OpenedFile:
    opened = _open_table(file, STOCK_COLUMNS, STOCK_EXTRAS)
    if any(_simplify(column) in map(_simplify, SALES_COLUMNS["date"]) for column in opened.rows.columns):
        raise DataProblem("This looks like the sales file, not the stock file. Are the two files swapped?")
    return opened


def _pick_columns(opened: OpenedFile, needed: dict, chosen: dict | None, extras: dict | None = None) -> pd.DataFrame:
    found = {**opened.found, **(chosen or {})}
    if missing := [name for name in needed if found.get(name) is None]:
        raise DataProblem(f"Could not tell which column is: {', '.join(missing)}. Columns in the file: {', '.join(opened.rows.columns)}.")
    keep = {found[name]: name for name in [*needed, *(extras or {})] if found.get(name) is not None}
    table = opened.rows[list(keep)].rename(columns=keep)
    table = table[table["item"].notna()].copy()
    table["item"] = table["item"].astype(str).str.strip()
    return table


def _drop(table: pd.DataFrame, bad: pd.Series, reason: str, notes: list[str]) -> pd.DataFrame:
    if bad.any():
        notes.append(f"Left out {int(bad.sum())} {reason}.")
    return table[~bad]


def clean_sales(opened: OpenedFile, chosen: dict | None = None) -> pd.DataFrame:
    """One row per item per day, with a zero for every day an item did not sell."""
    notes = opened.notes
    sales = _pick_columns(opened, SALES_COLUMNS, chosen)
    not_goods = sales["item"].str.lower().isin(NOT_GOODS)
    if not_goods.any():
        notes.append(f"Left out lines that are not goods: {', '.join(sorted(sales.loc[not_goods, 'item'].unique()))}.")
    sales = sales[~not_goods]
    sales["qty_sold"] = pd.to_numeric(sales["qty_sold"], errors="coerce")
    sales = _drop(sales, sales["qty_sold"].isna(), "rows with no quantity", notes)
    sales["date"] = _to_dates(sales["date"])
    if len(sales) and sales["date"].isna().mean() > 0.5:
        raise DataProblem("Could not read the dates in the sales file. Use dates like 03-09-2026 or 2026-09-03.")
    sales = _drop(sales, sales["date"].isna(), "rows with a date I could not read", notes)
    if sales.empty:
        raise DataProblem("The sales file has no usable rows.")
    sales["date"] = sales["date"].dt.normalize()
    sales = sales.groupby(["date", "item"], as_index=False)["qty_sold"].sum()
    sales["qty_sold"] = sales["qty_sold"].clip(lower=0)  # more returned than sold on a day counts as none sold
    # a day with no row for an item means none were sold that day
    every_day = pd.MultiIndex.from_product(
        [pd.date_range(sales["date"].min(), sales["date"].max()), sorted(sales["item"].unique())], names=["date", "item"])
    return sales.set_index(["date", "item"]).reindex(every_day, fill_value=0).reset_index()


def clean_stock(opened: OpenedFile, chosen: dict | None = None) -> pd.DataFrame:
    notes = opened.notes
    stock = _pick_columns(opened, STOCK_COLUMNS, chosen, STOCK_EXTRAS)
    if "status" in stock.columns:
        gone = stock["status"].astype(str).str.lower().str.startswith(("deleted", "inactive"))
        stock = _drop(stock, gone, "deleted or inactive items", notes)
    # a blank or "--" count means nobody knows the stock, so that item is left out rather than guessed
    stock["stock_left"] = pd.to_numeric(stock["stock_left"], errors="coerce")
    stock = _drop(stock, stock["stock_left"].isna(), "items with no stock count", notes)
    if stock.empty:
        raise DataProblem("The stock file has no usable rows.")
    negative = stock["stock_left"] < 0
    if negative.any():
        notes.append(f"{int(negative.sum())} items show a negative count, treated as none left.")
    stock["stock_left"] = stock["stock_left"].clip(lower=0)
    packs = pd.to_numeric(stock["pack_size"], errors="coerce") if "pack_size" in stock.columns else 1
    stock["pack_size"] = pd.Series(packs, index=stock.index).fillna(1).clip(lower=1).astype(int)
    return stock.groupby("item", as_index=False).agg(stock_left=("stock_left", "sum"), pack_size=("pack_size", "first"))


def load_sales(file) -> pd.DataFrame:
    return clean_sales(open_sales(file))


def load_stock(file) -> pd.DataFrame:
    return clean_stock(open_stock(file))
