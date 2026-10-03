import subprocess
import sys

import numpy as np
import pandas as pd
import pytest
from conftest import ROOT, FakeTabPFN

from intake import load_sales
from predictor import forecast
from web.config import Settings


def test_predictor_imports_without_torch_or_tabpfn():
    # None in sys.modules makes any import of these fail, as on a machine without them
    code = ("import sys; sys.modules.update(torch=None, tabpfn=None, tabpfn_client=None); "
            "import predictor; print(predictor.LocalTabPFN.__name__)")
    run = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert "torch" not in sys.modules or sys.modules["torch"] is None


def test_parallel_forecast_matches_one_by_one():
    sales = load_sales(ROOT / "data" / "sample_sales.csv")

    class OneByOne(FakeTabPFN):
        workers = 1

    progress = []
    parallel = forecast(sales, provider=FakeTabPFN(), on_progress=progress.append)
    pd.testing.assert_frame_equal(parallel, forecast(sales, provider=OneByOne()))
    assert progress[-1] == 1.0 and len(progress) == sales["item"].nunique()


def test_settings_from_env():
    assert Settings.from_env({}).gemma_backend == "off"
    assert Settings.from_env({}).max_items == 40
    env = Settings.from_env({"GEMMA_API_KEY": "k", "TABPFN_BACKEND": "local", "RUN_HONESTY": "false", "PORT": "8000"})
    assert (env.gemma_backend, env.max_items, env.run_honesty, env.port) == ("google", None, False, 8000)
    assert Settings.from_env({"MAX_ITEMS": "0"}).max_items is None
    assert Settings.from_env({}).gemma_model == "gemma-4-26b-a4b-it"


def test_pdftotext_has_a_timeout(monkeypatch):
    import pdf_to_sales
    seen = {}

    def run(*args, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired("pdftotext", kwargs["timeout"])

    monkeypatch.setattr(pdf_to_sales.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        pdf_to_sales.read_bills(b"%PDF")
    assert seen["timeout"] == 60

    from intake import DataProblem
    from web.repositories.uploads import FileRepository, UploadedFile
    with pytest.raises(DataProblem, match="Could not read"):
        FileRepository((".pdf",), 1).open_sales(UploadedFile("bills.pdf", b"%PDF"))


def test_fake_regressor_is_deterministic():
    from conftest import FakeRegressor
    answer = FakeRegressor().fit(None, np.array([1, 2, 3])).predict(np.zeros((2, 1)), output_type="main", quantiles=[0.8])
    assert list(answer["mean"]) == [2, 2] and list(answer["quantiles"][0]) == [3, 3]


def test_jobs_are_dropped_after_their_time():
    from web.repositories.jobs import JobStore, LimitReached
    now = [0.0]
    store = JobStore(keep_seconds=1800, clock=lambda: now[0])
    job = store.create("1.2.3.4", "Hindi", per_hour=1, running_per_owner=2, running_total=4)
    store.update(job, state="done")
    with pytest.raises(LimitReached):
        store.create("1.2.3.4", "Hindi", per_hour=1, running_per_owner=2, running_total=4)
    now[0] = 1801
    assert store.get(job.id) is None
    now[0] = 3601
    store.create("1.2.3.4", "Hindi", per_hour=1, running_per_owner=2, running_total=4)
