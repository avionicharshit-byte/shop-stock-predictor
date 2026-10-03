import threading

import pandas as pd
from conftest import ROOT, FakeTabPFN, make_client, wait_for

from core.intake import load_sales, load_stock
from core.predictor import forecast, split_rare_items
from core.reorder import reorder_plan
from web.services.stock_check import closest_to_running_out

SALES, STOCK = str(ROOT / "data" / "sample_sales.csv"), str(ROOT / "data" / "sample_stock.csv")


def csv_bytes(path: str) -> bytes:
    return open(path, "rb").read()


def test_health_and_config(client):
    assert client.get("/healthz").json() == {"ok": True}
    config = client.get("/api/config").json()
    assert config["languages"] == ["Hindi", "English", "Hinglish"]
    assert config["limits"] == {"max_items": 25, "max_upload_mb": 5.0, "horizon_days": 7}


def test_samples_download(client):
    reply = client.get("/api/sample/stock")
    assert reply.status_code == 200 and "sample_stock.csv" in reply.headers["content-disposition"]
    assert reply.content == csv_bytes(STOCK)
    assert client.get("/api/sample/other").status_code == 404


def test_sample_check_end_to_end(client):
    reply = client.post("/api/checks", data={"use_sample": "true", "language": "English"})
    assert reply.status_code == 202
    status = wait_for(client, reply.json()["job_id"])
    assert status["state"] == "done" and status["progress"] == 1.0, status
    result = status["result"]

    # the plan must be exactly what the domain code decides
    regular, rare = split_rare_items(load_sales(SALES))
    expected = reorder_plan(forecast(regular, provider=FakeTabPFN()), load_stock(STOCK))
    assert [line["item"] for line in result["plan"]] == list(expected["item"])
    for line, row in zip(result["plan"], expected.itertuples()):
        assert (line["order_units"], line["order_packs"], line["likely_to_sell"], line["busy_week"]) == \
               (row.order_units, row.order_packs, row.likely_to_sell, row.busy_week)
        assert line["runs_out_on"] == row.runs_out_on.date().isoformat()
        assert line["runs_out_weekday"] == f"{row.runs_out_on:%A}"
        assert line["by_ai"] and line["item"] in line["line"] and str(line["order_units"]) in line["line"]
    assert result["worded_by_ai"] == len(expected)
    assert result["rare_items"] == rare
    assert result["items_checked"] == 10 and len(result["enough_stock"]) == 10 - len(expected)
    assert result["capped"] is None and result["not_checked"] == []
    honesty = result["honesty"]
    assert set(honesty) >= {"tabpfn", "plain", "same_weekday", "busy_week_covered", "per_item"}
    assert honesty["tabpfn"]["daily_error"] >= 0 and len(honesty["per_item"]) == 10

    note = client.post(f"/api/checks/{reply.json()['job_id']}/note", json={"language": "Hinglish"}).json()
    assert len(note["lines"]) == len(expected) and note["worded_by_ai"] == len(expected)
    assert [line["item"] for line in note["plan"]] == list(expected["item"])
    assert all(line["by_ai"] and line["line"] == text for line, text in zip(note["plan"], note["lines"]))


def test_note_marks_each_line_gemma_worded():
    from conftest import echo_gemma
    # gemma answers only for some items in the second language, the rest stay plain
    def picky_gemma(prompt):
        return None if "Hinglish" in prompt and "by Tuesday" in prompt else echo_gemma(prompt)

    client = make_client(ask_gemma=picky_gemma)
    job_id = client.post("/api/checks", data={"use_sample": "true", "language": "English"}).json()["job_id"]
    result = wait_for(client, job_id)["result"]
    note = client.post(f"/api/checks/{job_id}/note", json={"language": "Hinglish"}).json()
    assert set(note) == {"lines", "worded_by_ai", "plan", "gemma_limited"} and not note["gemma_limited"]
    assert [line["item"] for line in note["plan"]] == [line["item"] for line in result["plan"]]
    for line, text in zip(note["plan"], note["lines"]):
        assert set(line) == {"item", "line", "line_plain", "by_ai"} and line["line"] == text
        assert line["by_ai"] == ("Tuesday" not in line["line_plain"])
        if not line["by_ai"]:
            assert line["line"] == line["line_plain"]
    assert 0 < note["worded_by_ai"] == sum(line["by_ai"] for line in note["plan"]) < len(note["plan"])


def test_forecast_and_honesty_side_by_side_match_one_after_another():
    class OneAtATime(FakeTabPFN):
        workers = 1

    def finished(make_forecaster):
        client = make_client(make_forecaster=make_forecaster)
        job_id = client.post("/api/checks", data={"use_sample": "true", "language": "Hindi"}).json()["job_id"]
        status = wait_for(client, job_id)
        assert status["state"] == "done" and status["progress"] == 1.0, status
        return status["result"]

    side_by_side, one_after_another = finished(FakeTabPFN), finished(OneAtATime)
    assert side_by_side == one_after_another
    assert side_by_side["honesty"] is not None and side_by_side["plan"]


def test_forecast_and_honesty_share_one_bounded_pool():
    running, most, lock = [0], [0], threading.Lock()
    seen = []

    class CountingTabPFN(FakeTabPFN):
        workers = 3

        def guess(self, history, future_dates):
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
                seen.append(history["date"].max())
            try:
                import time
                time.sleep(0.02)
                return super().guess(history, future_dates)
            finally:
                with lock:
                    running[0] -= 1

    client = make_client(make_forecaster=CountingTabPFN)
    status = wait_for(client, client.post("/api/checks", data={"use_sample": "true"}).json()["job_id"])
    assert status["state"] == "done" and status["result"]["honesty"] is not None
    assert most[0] == 3  # never more calls at once than FORECAST_WORKERS
    # the forecast's 10 calls are all queued before the honesty test's 10 on the shortened history
    assert len(seen) == 20 and seen[:10] == [max(seen)] * 10 and max(seen) not in seen[10:]


def test_honesty_that_cannot_run_still_finishes_the_progress():
    seen = []
    client = make_client()
    jobs = client.app.state.jobs
    update = jobs.update

    def watch(job, **changes):
        seen.append((changes.get("stage"), changes.get("progress")))
        update(job, **changes)
    jobs.update = watch
    client.app.state.checks.honesty.days = 400  # far more days than the sample has
    status = wait_for(client, client.post("/api/checks", data={"use_sample": "true"}).json()["job_id"])
    assert status["state"] == "done" and status["result"]["honesty"] is None
    assert "honesty test needs" in status["result"]["honesty_problem"]
    shown = [progress for _, progress in seen if progress is not None]
    assert shown == sorted(shown) and ("wording", 0.87) in seen


def test_check_without_gemma_keeps_plain_lines():
    client = make_client(ask_gemma=None, gemma_backend="off")
    assert client.get("/api/config").json()["gemma"] == "off"
    status = wait_for(client, client.post("/api/checks", data={"use_sample": "true"}).json()["job_id"])
    plan = status["result"]["plan"]
    assert plan and status["result"]["worded_by_ai"] == 0
    assert all(line["line"] == line["line_plain"] and not line["by_ai"] for line in plan)


def test_needs_columns_then_resubmit(client):
    sales = pd.read_csv(SALES).rename(columns={"item": "Article"}).to_csv(index=False).encode()
    files = {"sales": ("sales.csv", sales, "text/csv"), "stock": ("stock.csv", csv_bytes(STOCK), "text/csv")}
    reply = client.post("/api/checks", files=files)
    assert reply.status_code == 422
    body = reply.json()
    assert body["problem"] is None
    assert body["needs_columns"] == [{"file": "sales", "name": "item", "ask": "In the sales file, which column has the item name?",
                                      "options": ["date", "Article", "qty_sold"]}]
    reply = client.post("/api/checks", files=files, data={"columns": '{"sales": {"item": "Article"}}'})
    assert reply.status_code == 202
    assert wait_for(client, reply.json()["job_id"])["state"] == "done"
    reply = client.post("/api/checks", files=files, data={"columns": '{"sales": {"item": "Nope"}}'})
    assert reply.status_code == 422 and "Nope" in reply.json()["problem"]


def test_swapped_files_is_a_data_problem(client):
    files = {"sales": ("stock.csv", csv_bytes(STOCK), "text/csv"), "stock": ("sales.csv", csv_bytes(SALES), "text/csv")}
    reply = client.post("/api/checks", files=files)
    assert reply.status_code == 422
    assert "swapped" in reply.json()["problem"]


def test_upload_checks(client):
    reply = client.post("/api/checks", files={"sales": ("sales.txt", b"x", "text/plain"),
                                              "stock": ("stock.csv", csv_bytes(STOCK), "text/csv")})
    assert reply.status_code == 422 and "PDF, Excel or CSV" in reply.json()["problem"]
    small = make_client(max_upload_mb=0.001)
    reply = small.post("/api/checks", files={"sales": ("sales.csv", csv_bytes(SALES), "text/csv"),
                                             "stock": ("stock.csv", csv_bytes(STOCK), "text/csv")})
    assert reply.status_code == 413 and "over 0.001 MB" in reply.json()["problem"]
    huge = client.post("/api/checks", files={"sales": ("sales.csv", b"a" * (11 * 1024 * 1024), "text/csv")})
    assert huge.status_code == 413
    reply = client.post("/api/checks", files={"sales": ("sales.csv", csv_bytes(SALES), "text/csv")})
    assert reply.status_code == 422 and reply.json()["problem"] == "Add the stock report too to continue."
    reply = client.post("/api/checks", data={"use_sample": "true", "language": "French"})
    assert reply.status_code == 422


def test_item_cap_keeps_items_closest_to_running_out():
    client = make_client(max_items=3)
    status = wait_for(client, client.post("/api/checks", data={"use_sample": "true"}).json()["job_id"])
    result = status["result"]
    assert result["capped"]["kept"] == 3 and result["capped"]["dropped"] == 7
    kept = closest_to_running_out(split_rare_items(load_sales(SALES))[0], load_stock(STOCK), 3)
    assert {line["item"] for line in result["plan"]} <= set(kept)
    assert result["items_checked"] == 3 and len(result["honesty"]["per_item"]) == 3


def test_tabpfn_not_configured():
    client = make_client(make_forecaster=None, tabpfn_backend="api", tabpfn_token=None)
    assert client.get("/api/config").json()["tabpfn"] == "off"
    reply = client.post("/api/checks", data={"use_sample": "true"})
    assert reply.status_code == 503
    assert "TabPFN is not configured" in reply.json()["problem"]


def test_rate_limit_per_hour_and_per_visitor():
    client = make_client(checks_per_hour=2)
    for _ in range(2):
        job = client.post("/api/checks", data={"use_sample": "true"}, headers={"X-Forwarded-For": "1.2.3.4, 10.0.0.1"})
        assert job.status_code == 202
        wait_for(client, job.json()["job_id"])
    reply = client.post("/api/checks", data={"use_sample": "true"}, headers={"X-Forwarded-For": "1.2.3.4"})
    assert reply.status_code == 429 and "last hour" in reply.json()["problem"]
    # another visitor is not blocked
    other = client.post("/api/checks", data={"use_sample": "true"}, headers={"X-Forwarded-For": "5.6.7.8"})
    assert other.status_code == 202


def test_rate_limit_running_at_once():
    release = threading.Event()

    class SlowTabPFN(FakeTabPFN):
        def guess(self, history, future_dates):
            release.wait(10)
            return super().guess(history, future_dates)

    client = make_client(make_forecaster=SlowTabPFN, running_per_ip=2)
    jobs = [client.post("/api/checks", data={"use_sample": "true"}) for _ in range(2)]
    assert [job.status_code for job in jobs] == [202, 202]
    third = client.post("/api/checks", data={"use_sample": "true"})
    assert third.status_code == 429 and "still running" in third.json()["problem"]
    assert client.get(f"/api/checks/{jobs[0].json()['job_id']}").json()["state"] in ("queued", "running")
    release.set()
    for job in jobs:
        assert wait_for(client, job.json()["job_id"])["state"] == "done"


def test_failed_model_call_fails_the_job_plainly():
    class BrokenTabPFN(FakeTabPFN):
        def guess(self, history, future_dates):
            raise ConnectionError("network down")

    client = make_client(make_forecaster=BrokenTabPFN)
    status = wait_for(client, client.post("/api/checks", data={"use_sample": "true"}).json()["job_id"])
    assert status["state"] == "failed" and "did not answer" in status["problem"]


def test_unknown_job_and_note_before_done(client):
    assert client.get("/api/checks/nope").status_code == 404
    assert client.post("/api/checks/nope/note", json={"language": "Hindi"}).status_code == 404


def test_orders(client):
    body = {"shop_name": "Sharma Store", "address": "Main Road", "lines": [
        {"item": "Wire <2.5mm>", "quantity": 10}, {"item": "Switch", "quantity": 0}]}
    text = client.post("/api/orders/text", json=body).json()["text"]
    assert text.startswith("Order from Sharma Store\nMain Road\n") and "1. Wire <2.5mm> - 10 pieces" in text
    assert "Switch" not in text
    sheet = client.post("/api/orders/sheet", json=body)
    assert sheet.headers["content-type"].startswith("text/html")
    assert "Wire &lt;2.5mm&gt;" in sheet.text and "<!doctype html>" in sheet.text
    assert client.post("/api/orders/text", json={"lines": "x"}).status_code == 422


def test_order_receipt_is_the_laptop_receipt(client):
    from core import receipt_printer
    from web.services.orders import OrderService

    day = pd.Timestamp("2026-10-03")
    client.app.state.orders = OrderService(today=lambda: day)
    body = {"shop_name": "Sharma Store", "address": "Main Road, Jaipur", "lines": [
        {"item": "Wire 2.5mm red coil long name", "quantity": 10}, {"item": "Switch", "quantity": 0},
        {"item": "Bulb", "quantity": 4}]}
    order = pd.DataFrame({"Item": ["Wire 2.5mm red coil long name", "Bulb"], "Quantity": [10.0, 4.0]})

    two = client.post("/api/orders/receipt", json=body)
    assert two.status_code == 200 and two.headers["content-type"] == "application/octet-stream"
    assert two.content == receipt_printer.order_receipt("Sharma Store", "Main Road, Jaipur", order, day, 32)

    three = client.post("/api/orders/receipt", json={**body, "paper": "3 inch (80 mm)"})
    assert three.content == receipt_printer.order_receipt("Sharma Store", "Main Road, Jaipur", order, day, 48)
    assert b"\n" + b"-" * 48 + b"\n" in three.content and b"-" * 49 not in three.content

    bad = client.post("/api/orders/receipt", json={**body, "paper": "4 inch"})
    assert bad.status_code == 422 and "paper" in bad.json()["problem"]


def test_page_and_its_files_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "<title>Shop Stock Predictor</title>" in page.text
    for path in ("/styles.css", "/app.js", "/fonts/mukta-400-latin.woff2", "/fonts/mukta/OFL.txt"):
        assert client.get(path).status_code == 200, path
    assert "fonts.googleapis.com" not in page.text and "fonts.googleapis.com" not in client.get("/styles.css").text
