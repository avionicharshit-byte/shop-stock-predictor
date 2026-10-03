import numpy as np
import pandas as pd

ITEMS = {  # item: (average sold per day, weekend boost, pack size)
    "Maggi 70g": (14, 1.5, 24), "Parle-G": (22, 1.1, 30), "Amul Milk 500ml": (35, 1.0, 20),
    "Tata Salt 1kg": (5, 1.0, 10), "Surf Excel 1kg": (3, 1.3, 6), "Lays Classic": (18, 1.8, 40),
    "Coca-Cola 750ml": (9, 2.0, 12), "Aashirvaad Atta 5kg": (4, 1.2, 4),
    "Colgate 100g": (4, 1.0, 12), "Bread": (16, 0.9, 10),
}

rng = np.random.default_rng(7)
days = pd.date_range(end=pd.Timestamp.today().normalize() - pd.Timedelta(days=1), periods=28)
rows = []
for item, (mean, weekend, _) in ITEMS.items():
    for day in days:
        boost = weekend if day.dayofweek >= 5 else 1.0
        rows.append({"date": day.date(), "item": item, "qty_sold": int(rng.poisson(mean * boost))})
pd.DataFrame(rows).to_csv("data/sample_sales.csv", index=False)

stock = [{"item": item, "stock_left": int(mean * rng.uniform(1, 9)), "pack_size": pack}
         for item, (mean, _, pack) in ITEMS.items()]
pd.DataFrame(stock).to_csv("data/sample_stock.csv", index=False)
print("wrote data/sample_sales.csv and data/sample_stock.csv")
