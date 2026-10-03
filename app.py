"""Shop Stock Predictor. Run with: streamlit run app.py"""
from html import escape

import pandas as pd
import streamlit as st

from intake import SALES_COLUMNS, STOCK_COLUMNS, DataProblem, clean_sales, clean_stock, open_sales, open_stock
from message import friendly_note
from order_sheet import order_html, order_text
from predictor import check_enough_days, forecast, honesty_test, split_rare_items
from reorder import items_without_stock, reorder_plan
from thermal import PAPER_WIDTHS, PrinterProblem, find_printers, order_receipt, send_to_printer

SAMPLE_SALES, SAMPLE_STOCK = "data/sample_sales.csv", "data/sample_stock.csv"
FILE_TYPES = ["pdf", "xlsx", "xls", "csv"]
ASK = {"date": "the date", "item": "the item name", "qty_sold": "how many were sold", "stock_left": "how many are left"}

st.set_page_config(page_title="Shop Stock Predictor", page_icon=":material/inventory_2:")
# the "working on it" spinner sits in the middle, under the full-width button, not off to the left
st.html("""<style>
div:has(> [data-testid='stSpinner']) { width: 100%; display: flex; justify-content: center; }
/* the "checking" pop-up: in the middle of the screen, roomy, with large text and a thick progress bar */
[data-testid='stDialog'] { align-items: center; }
[data-testid='stDialog'] > div { width: min(640px, 92vw); }
[data-testid='stDialog'] [role='dialog'] { width: 100%; padding: 20px 16px 28px; }
[data-testid='stDialog'] [role='dialog'] h2 { font-size: 28px; }
[data-testid='stDialog'] [data-testid='stProgress'] p { font-size: 19px; margin-bottom: 10px; }
[data-testid='stDialog'] [data-testid='stProgress'] [role='progressbar'] > div { height: 14px; border-radius: 7px; }
/* see-through greys and a mid teal, so the cards read well on both the light and the dark theme */
.note-line { display: flex; gap: 14px; align-items: flex-start; padding: 14px 16px; line-height: 1.5;
             background: rgba(128, 128, 128, 0.08); border: 1px solid rgba(128, 128, 128, 0.28);
             border-left: 5px solid #12998C; border-radius: 8px; }
.note-number { flex: none; width: 28px; height: 28px; border-radius: 50%; background: rgba(18, 153, 140, 0.18);
               font-weight: 600; text-align: center; line-height: 28px; font-size: 14px; }
</style>""")
st.title("Shop Stock Predictor")
st.write("See what will run out this week and how much to order. Everything runs on this laptop, "
         "so the shop's sales never leave it.")


def ask_for_unknown_columns(opened, needed: dict, file_label: str) -> dict:
    """Only shown when a column could not be recognised. Returns what the user picked."""
    picked = {}
    for name in opened.unknown(needed):
        picked[name] = st.selectbox(f"In the {file_label} file, which column has {ASK[name]}?",
                                    opened.rows.columns, index=None, key=f"{file_label}-{name}")
    return picked


st.subheader("1. Add the shop's two reports")
sales_box, stock_box = st.columns(2)
sales_file = sales_box.file_uploader("Sales report", type=FILE_TYPES, help="Every sale with its date. PDF, Excel or CSV.")
stock_file = stock_box.file_uploader("Stock report", type=FILE_TYPES, help="How many of each item are left. PDF, Excel or CSV.")
if st.toggle("No files handy? Try it with sample data"):
    sales_file, stock_file = SAMPLE_SALES, SAMPLE_STOCK
with st.expander("What should the files look like?"):
    st.write("**Sales**: every sale with its date. The bill-wise sales report from the billing app works as it is. "
             "So does any PDF, Excel or CSV with a date, an item name and a quantity.")
    st.dataframe(pd.read_csv(SAMPLE_SALES).head(3), hide_index=True)
    st.download_button("Download a sample sales file", open(SAMPLE_SALES, "rb").read(), "sample_sales.csv")
    st.write("**Stock**: how many of each item are left today. The stock summary from the billing app works as it is. "
             "A pack size column is optional.")
    st.dataframe(pd.read_csv(SAMPLE_STOCK).head(3), hide_index=True)
    st.download_button("Download a sample stock file", open(SAMPLE_STOCK, "rb").read(), "sample_stock.csv")
if not (sales_file and stock_file):
    missing = "both reports" if not (sales_file or stock_file) else "the stock report too" if sales_file else "the sales report too"
    st.info(f"Add {missing} to continue.")
    st.stop()

try:
    opened_sales, opened_stock = open_sales(sales_file), open_stock(stock_file)
    picked_sales = ask_for_unknown_columns(opened_sales, SALES_COLUMNS, "sales")
    picked_stock = ask_for_unknown_columns(opened_stock, STOCK_COLUMNS, "stock")
    if None in [*picked_sales.values(), *picked_stock.values()]:
        st.stop()
    sales, stock = clean_sales(opened_sales, picked_sales), clean_stock(opened_stock, picked_stock)
    check_enough_days(sales)
except DataProblem as problem:
    st.error(str(problem))
    st.stop()

shop_name = opened_stock.shop_name or opened_sales.shop_name
st.success(f"{shop_name + ': r' if shop_name else 'R'}ead sales of {sales['item'].nunique()} items over "
           f"{sales['date'].nunique()} days ({sales['date'].min():%d %b} to {sales['date'].max():%d %b}), "
           f"and stock counts for {len(stock)} items.")
if left_out := [*opened_sales.notes, *opened_stock.notes]:
    with st.expander("What I left out, and why"):
        for note in left_out:
            st.write(note)

st.subheader("2. Check the stock")
language = st.radio("Language for the note", ["Hindi", "English", "Hinglish"], horizontal=True)
inputs = (getattr(sales_file, "file_id", sales_file), getattr(stock_file, "file_id", stock_file),
          tuple(picked_sales.items()), tuple(picked_stock.items()))

@st.dialog("Checking your stock", dismissible=False)
def check_stock_in_popup() -> None:
    """Shows each step while it runs, then closes itself and reveals the results."""
    st.write("This takes about a minute for a big shop. The page opens by itself when it is done.")
    progress = st.progress(2, "Looking at how each item sells...")
    try:
        regular_sales, rare_items = split_rare_items(sales)
        items = regular_sales["item"].nunique()
        predicted = forecast(regular_sales, on_progress=lambda done: progress.progress(
            2 + int(43 * done), f"Predicting next week, item {round(done * items)} of {items}..."))
        result = {"inputs": inputs, "plan": reorder_plan(predicted, stock), "notes": {},
                  "not_checked": items_without_stock(predicted, stock), "rare_items": rare_items}
        try:
            result["test"] = honesty_test(regular_sales, on_progress=lambda done: progress.progress(
                45 + int(40 * done), f"Testing the guesses against last week, item {round(done * items)} of {items}..."))
        except DataProblem as problem:
            result["test"] = str(problem)
        progress.progress(87, f"Writing the note in {language}...")
        result["notes"][language] = friendly_note(result["plan"], language)
    except DataProblem as problem:
        st.session_state["check_problem"] = str(problem)
        st.rerun()
    progress.progress(100, "Done")
    st.session_state["result"] = result
    st.rerun()


if st.button("Check my stock", type="primary", width="stretch"):
    st.session_state.pop("check_problem", None)
    check_stock_in_popup()
if problem := st.session_state.get("check_problem"):
    st.error(problem)

# results are kept between clicks, so changing the language does not predict again
result = st.session_state.get("result")
if not result or result["inputs"] != inputs:
    st.stop()
plan = result["plan"]
if language not in result["notes"]:
    with st.spinner("Writing the note..."):
        result["notes"][language] = friendly_note(plan, language)
note, worded_by_ai = result["notes"][language]

st.subheader("3. What to do")
if plan.empty:
    st.success(note)
note_tab, order_tab, numbers_tab, test_tab = st.tabs(["Note", "Order list", "The numbers", "Honesty test"])

with note_tab:
    if len(plan):
        # one card per item, with the item name in bold, so the lines do not run into each other
        for number, (line, item) in enumerate(zip(note.splitlines(), plan["item"]), 1):
            before, found, after = line.partition(item)
            wording = f"{escape(before)}<strong>{escape(found)}</strong>{escape(after)}"
            st.html(f"<div class='note-line'><span class='note-number'>{number}</span><span>{wording}</span></div>")
        with st.expander("Copy the whole note"):
            st.code(note, language=None, wrap_lines=True)
        st.caption(f"Gemma, the AI on this laptop, worded {worded_by_ai} of {len(plan)} lines. "
                   "It never sees the item names or numbers, and a line with the wrong day is kept plain.")
    else:
        st.write("Nothing to order this week.")

with order_tab:
    if plan.empty:
        st.write("Nothing to order this week.")
    else:
        st.caption("These are guesses, so change any quantity or untick an item before you send it.")
        name_box, address_box = st.columns(2)
        shop = name_box.text_input("Shop name", shop_name or "")
        address = address_box.text_input("Shop address and phone")
        edited = st.data_editor(
            pd.DataFrame({"Order": True, "Item": plan["item"], "Quantity": plan["order_units"]}),
            hide_index=True, width="stretch", disabled=["Item"],
            column_config={"Quantity": st.column_config.NumberColumn("Quantity (pieces)", min_value=0, step=1)})
        order = edited[edited["Order"] & (edited["Quantity"] > 0)]
        if order.empty:
            st.info("Tick at least one item to make an order list.")
        else:
            today = pd.Timestamp.today()
            whatsapp_tab, paper_tab, thermal_tab = st.tabs(["Send on WhatsApp", "Print on paper", "Receipt printer"],
                                                           on_change="rerun")
            with whatsapp_tab:
                st.write("Press the copy icon at the top right of the box, then paste it into WhatsApp.")
                st.code(order_text(shop, address, order, today), language=None)
            with paper_tab:
                sheet = order_html(shop, address, order, today)
                st.iframe(sheet, height=min(900, 300 + 34 * len(order)))
                st.download_button("Download the order list", sheet, "order_list.html", "text/html", width="stretch")
            with thermal_tab:
                st.write("For a Bluetooth receipt printer. Switch it on and keep it near the laptop.")
                paper = st.radio("Paper width", list(PAPER_WIDTHS), horizontal=True)
                # the scan starts by itself the first time this tab is opened, nothing to type or press
                if thermal_tab.open and "printers" not in st.session_state:
                    st.session_state["scan_for_printers"] = True
                if st.session_state.pop("scan_for_printers", False):
                    try:
                        with st.spinner("Looking for printers nearby, about 8 seconds..."):
                            st.session_state["printers"] = find_printers()
                    except PrinterProblem as problem:
                        st.session_state["printers"] = []
                        st.error(str(problem))
                printers = st.session_state.get("printers")
                if printers:
                    # two printers of the same model share a name, the address below it tells them apart
                    # nothing is picked for him, so the list never prints on a neighbour's printer by accident
                    picked = st.radio("Pick your printer", printers, format_func=lambda printer: printer[0],
                                      captions=[printer[1] for printer in printers], index=None)
                    if st.button("Print the order list", type="primary", width="stretch", disabled=picked is None):
                        try:
                            with st.spinner("Printing..."):
                                printed_on = send_to_printer(
                                    picked[1], order_receipt(shop, address, order, today, PAPER_WIDTHS[paper]), printers)
                            st.success(f"Printed on {printed_on}.")
                        except PrinterProblem as problem:
                            st.error(str(problem))
                elif printers is not None:
                    st.warning("No receipt printer found. Switch the printer on, keep it near the laptop, "
                               "then press Scan again.")
                if printers is not None and st.button("Scan again", width="stretch"):
                    st.session_state["scan_for_printers"] = True
                    st.rerun()

with numbers_tab:
    if len(plan):
        pieces = plan["order_units"].astype(str) + " pieces"
        st.dataframe(pd.DataFrame({
            "Item": plan["item"],
            "In stock": plan["stock_left"],
            "Likely to sell in 7 days": plan["likely_to_sell"],
            "In a busy week": plan["busy_week"],
            "Runs out": plan["runs_out_on"].dt.strftime("%a %d %b").where(plan["stock_left"] > 0, "Already out"),
            "Order": pieces.where(plan["order_packs"] == plan["order_units"],
                                  plan["order_packs"].astype(str) + " packs (" + pieces + ")"),
        }), hide_index=True, width="stretch")
    else:
        st.write("Every checked item has enough stock for the next 7 days.")
    if result["not_checked"]:
        with st.expander(f"{len(result['not_checked'])} sold items have no stock count, so they were not checked"):
            st.write(", ".join(result["not_checked"]))
    if result["rare_items"]:
        with st.expander(f"{len(result['rare_items'])} items sold on fewer than 3 days, too rare to predict"):
            st.write(", ".join(result["rare_items"]))

with test_tab:
    test = result["test"]
    if isinstance(test, str):
        st.info(test)
    else:
        st.write("I hid the last week of sales, guessed it, then compared with what really sold. "
                 "The numbers show how far off each guess was, in pieces per item. Lower is better.")
        guesses = {"TabPFN (the AI)": "tabpfn", "Plain average": "plain", "Same-weekday average": "same_weekday"}
        st.dataframe(pd.DataFrame({
            "Guess": list(guesses),
            "Off by, per day": [round(test[f"{name}_daily_error"], 2) for name in guesses.values()],
            "Off by, per week": [round(test[f"{name}_weekly_error"], 2) for name in guesses.values()],
        }), hide_index=True, width="stretch")
        st.caption("The two averages need no AI, they are the guesses TabPFN has to beat. It does not always win, "
                   "least of all with only a few weeks of sales. A sudden bulk sale, like one customer buying a lot "
                   f"of wire, is something no guess can see coming. The busy-week level covered "
                   f"{test['busy_week_covered']:.0%} of the items in the hidden week.")
        st.dataframe(test["per_item"].rename(columns={"item": "Item", "really_sold": "Really sold that week",
                                                      "tabpfn": "TabPFN guess", "plain": "Plain average guess",
                                                      "same_weekday": "Same-weekday guess"}),
                     hide_index=True, width="stretch")
