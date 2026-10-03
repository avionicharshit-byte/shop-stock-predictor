import sys
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.predictor import BUSY_WEEK, _guess_one_item
from web.config import Settings
from web.main import create_app


class FakeRegressor:
    """Stands in for TabPFN: the item's mean sales per day, and half again for a busy day."""

    def fit(self, features, target):
        self.mean = float(np.mean(target))
        return self

    def predict(self, features, output_type="mean", quantiles=None):
        assert output_type == "main" and quantiles == [BUSY_WEEK]
        rows = len(features)
        return {"mean": np.full(rows, self.mean), "quantiles": [np.full(rows, self.mean * 1.5)]}


class FakeTabPFN:
    workers = 4

    def guess(self, history, future_dates):
        return _guess_one_item(FakeRegressor(), history, future_dates)

    def finish(self):
        pass


def echo_gemma(prompt: str) -> str:
    """Stands in for Gemma: answers with the alert it was asked to reword, which keeps every fact."""
    return prompt.rsplit("Alert: ", 1)[1].splitlines()[0]


def make_client(make_forecaster=FakeTabPFN, ask_gemma=echo_gemma, **settings) -> TestClient:
    base = {"gemma_backend": "google", "tabpfn_token": None, "checks_per_hour": 100, "running_per_ip": 10,
            "running_total": 10}
    app = create_app(Settings(**{**base, **settings}), make_forecaster=make_forecaster, ask_gemma=ask_gemma)
    return TestClient(app)


def wait_for(client: TestClient, job_id: str, seconds: float = 20) -> dict:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        status = client.get(f"/api/checks/{job_id}").json()
        if status["state"] in ("done", "failed"):
            return status
        time.sleep(0.05)
    raise AssertionError("the check did not finish")


@pytest.fixture
def client():
    return make_client()
