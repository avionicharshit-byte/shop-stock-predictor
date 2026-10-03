import math
import pandas as pd


def items_without_stock(predicted: pd.DataFrame, stock: pd.DataFrame) -> list[str]:
    return sorted(set(predicted["item"]) - set(stock["item"]))


def reorder_plan(predicted: pd.DataFrame, stock: pd.DataFrame) -> pd.DataFrame:
    # plain arithmetic on purpose, the AI never decides a quantity
    stock = stock.set_index("item")
    plan = []
    for item, days in predicted.sort_values("date").groupby("item"):
        if item not in stock.index:
            continue
        left, pack = float(stock.at[item, "stock_left"]), int(stock.at[item, "pack_size"])
        need = math.ceil(days["predicted_qty"].sum())  # whole pieces, so the table and the order agree
        busy_week_need = max(need, math.ceil(days["busy_qty"].sum()))
        if need <= left:
            continue
        sold_so_far = days["predicted_qty"].cumsum()
        runs_out_on = days.loc[sold_so_far > left, "date"].iloc[0]
        # he is told about an item when a normal week would empty the shelf, and the order covers a busy week
        packs = math.ceil((busy_week_need - left) / pack)
        plan.append({"item": item, "stock_left": int(left), "likely_to_sell": need, "busy_week": busy_week_need,
                     "runs_out_on": runs_out_on, "order_packs": packs, "order_units": packs * pack})
    columns = ["item", "stock_left", "likely_to_sell", "busy_week", "runs_out_on", "order_packs", "order_units"]
    return pd.DataFrame(plan, columns=columns).sort_values("runs_out_on").reset_index(drop=True)
