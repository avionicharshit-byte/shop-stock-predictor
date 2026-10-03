"""The recorded sample run and the credit budget that keeps free quotas from running out on visitors."""
import json
from datetime import datetime, timezone

import pytest
from conftest import ROOT, FakeTabPFN, echo_gemma, make_client, wait_for

from web.config import Settings
from web.repositories.quota import CreditMeter, looks_like_quota, parse_usage
from web.repositories.wording import GemmaLimited, ask_google_gemma
from web.services.quota import QuotaService

SENTENCE = ("Currently, you have used {used} of the allowed limit of 20000000 credits. "
            "The limit will reset at 2026-11-01 00:00:00 UTC.")
RECORDED = json.loads((ROOT / "data" / "sample_result.json").read_text())
DAY = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)


def usage(used: int):
    return lambda: SENTENCE.format(used=used)


class Counted:
    """Stand-in models that count every call, to prove the recorded run makes none."""

    def __init__(self):
        self.forecasters, self.prompts = 0, 0

    def forecaster(self):
        self.forecasters += 1
        return FakeTabPFN()

    def gemma(self, prompt):
        self.prompts += 1
        return echo_gemma(prompt)


def test_recorded_sample_makes_no_model_calls():
    counted = Counted()
    client = make_client(make_forecaster=counted.forecaster, ask_gemma=counted.gemma, recorded_sample=True,
                         daily_credits=0)
    assert client.get("/api/config").json()["recorded_sample"] is True
    reply = client.post("/api/checks", data={"use_sample": "true"})
    assert reply.status_code == 202 and reply.json()["recorded"] is True
    status = client.get(f"/api/checks/{reply.json()['job_id']}").json()  # done at once, no waiting
    assert status["state"] == "done" and status["progress"] == 1.0 and not status["quota"]
    result = status["result"]
    assert result["recorded"] is True and result["recorded_on"] == "2026-10-03"
    assert result["language"] == "Hindi" and result["plan"] == RECORDED["result"]["plan"]
    assert counted.forecasters == 0 and counted.prompts == 0


def test_recorded_sample_has_all_three_languages():
    counted = Counted()
    client = make_client(make_forecaster=counted.forecaster, ask_gemma=counted.gemma, recorded_sample=True)
    assert set(RECORDED["notes"]) == {"Hindi", "English", "Hinglish"}
    job_id = client.post("/api/checks", data={"use_sample": "true", "language": "English"}).json()["job_id"]
    result = client.get(f"/api/checks/{job_id}").json()["result"]
    english = RECORDED["notes"]["English"]
    assert result["language"] == "English" and [line["line"] for line in result["plan"]] == english["lines"]
    assert result["worded_by_ai"] == english["worded_by_ai"]
    for language in ("Hindi", "English", "Hinglish"):
        note = client.post(f"/api/checks/{job_id}/note", json={"language": language}).json()
        assert note == RECORDED["notes"][language]
        assert len(note["lines"]) == len(result["plan"])
    assert counted.forecasters == 0 and counted.prompts == 0


def test_recorded_sample_works_with_tabpfn_off():
    client = make_client(make_forecaster=None, tabpfn_backend="api", tabpfn_token=None, recorded_sample=True)
    reply = client.post("/api/checks", data={"use_sample": "true"})
    assert reply.status_code == 202 and reply.json()["recorded"] is True


def test_parse_usage_sentence():
    parsed = parse_usage(SENTENCE.format(used=2000000))
    assert (parsed.used, parsed.limit) == (2_000_000, 20_000_000)
    assert parsed.resets == datetime(2026, 11, 1, tzinfo=timezone.utc)
    unlimited = parse_usage("Currently, you have used 5 of the allowed limit of Unlimited credits. "
                            "The limit will reset at 2026-11-01 00:00:00 UTC.")
    assert unlimited.limit is None and unlimited.used == 5
    assert parse_usage("Your plan: lots of credits left.") is None
    assert parse_usage(None) is None


def test_unparseable_usage_allows_the_check():
    client = make_client(read_usage=lambda: "a new format nobody expected")
    assert client.get("/api/config").json()["quota"]["live_checks_left_today"] is None
    reply = client.post("/api/checks", data={"use_sample": "true"})
    assert reply.status_code == 202
    assert wait_for(client, reply.json()["job_id"])["state"] == "done"

    def broken():
        raise ConnectionError("down")
    assert make_client(read_usage=broken).post("/api/checks", data={"use_sample": "true"}).status_code == 202


def test_monthly_reserve_refuses_a_check():
    # 550,000 left, the reserve keeps 500,000, the sample costs 100,000
    client = make_client(read_usage=usage(19_450_000))
    reply = client.post("/api/checks", data={"use_sample": "true"})
    assert reply.status_code == 429
    assert reply.json() == {"problem": "This month's free quota for live checks is used up. It resets on 1 November. "
                                       "The recorded sample run still works.", "quota": True}
    assert client.get("/api/config").json()["quota"] == {"live_checks_left_today": 0,
                                                         "resets": "2026-11-01T00:00:00+00:00"}
    # with room for exactly one more check it still runs
    assert make_client(read_usage=usage(19_400_000)).post("/api/checks", data={"use_sample": "true"}).status_code == 202


def test_daily_budget_refuses_then_resets_on_a_new_utc_day():
    now = [DAY]
    client = make_client(daily_credits=250_000, read_usage=usage(2_000_000), now=lambda: now[0])
    for _ in range(2):
        reply = client.post("/api/checks", data={"use_sample": "true"})
        assert reply.status_code == 202
        wait_for(client, reply.json()["job_id"])
    assert client.get("/api/config").json()["quota"]["live_checks_left_today"] == 0
    refused = client.post("/api/checks", data={"use_sample": "true"})
    assert refused.status_code == 429
    assert refused.json() == {"problem": "Today's free quota for live checks is used up. It resets at midnight UTC. "
                                         "The recorded sample run still works.", "quota": True}
    now[0] = datetime(2026, 10, 4, 0, 0, 1, tzinfo=timezone.utc)
    assert client.get("/api/config").json()["quota"] == {"live_checks_left_today": 2,
                                                         "resets": "2026-10-05T00:00:00+00:00"}
    assert client.post("/api/checks", data={"use_sample": "true"}).status_code == 202


def test_daily_spend_survives_a_restart_through_the_account_usage():
    # memory starts empty after a restart, but the account's usage has grown since the first reading today
    used, tick = [2_000_000], [0.0]
    meter = CreditMeter(lambda: SENTENCE.format(used=used[0]), now=lambda: DAY, clock=lambda: tick[0])
    quota = QuotaService(Settings(daily_credits=250_000), meter, counts=True)
    assert quota.outlook().live_checks_left_today == 2
    used[0] += 200_000  # checks run elsewhere on this account
    assert quota.outlook().live_checks_left_today == 2  # still the cached reading
    tick[0] += 61
    assert quota.outlook().live_checks_left_today == 0
    with pytest.raises(Exception) as refused:
        quota.reserve(10)
    assert refused.value.status == 429 and refused.value.body["quota"] is True


def test_cost_of_a_check():
    meter = CreditMeter()
    assert QuotaService(Settings(), meter, counts=True).cost(10) == 100_000
    assert QuotaService(Settings(), meter, counts=True).cost(25) == 250_000
    assert QuotaService(Settings(run_honesty=False), meter, counts=True).cost(10) == 50_000
    assert QuotaService(Settings(), meter, counts=False).reserve(10) == 0  # tabpfn on this machine costs nothing


def test_cost_counts_the_capped_items():
    seen = []
    client = make_client(max_items=3, read_usage=usage(2_000_000))
    meter = client.app.state.quota.meter
    reserve = meter.reserve
    meter.reserve = lambda decide: seen.append(reserve(decide)) or seen[-1]
    client.post("/api/checks", data={"use_sample": "true"})
    assert seen == [3 * 2 * 5000]


def test_provider_quota_refusal_fails_the_job_with_quota():
    class OverQuota(FakeTabPFN):
        def guess(self, history, future_dates):
            raise RuntimeError("Fail to call fit: [HTTP 429] Too many requests.")

    client = make_client(make_forecaster=OverQuota)
    status = wait_for(client, client.post("/api/checks", data={"use_sample": "true"}).json()["job_id"])
    assert status["state"] == "failed" and status["quota"] is True
    assert status["problem"].startswith("Today's free quota for live checks is used up.")


def test_quota_looking_errors():
    assert looks_like_quota(RuntimeError("Fail to call fit: [HTTP 402] Payment required."))
    assert looks_like_quota(RuntimeError("Fail to call predict: [HTTP 403] Usage limit exceeded for this month."))
    wrapped = ValueError("predict failed")
    wrapped.__cause__ = RuntimeError("You have run out of credits.")
    assert looks_like_quota(wrapped)
    assert not looks_like_quota(ConnectionError("network down"))
    assert not looks_like_quota(RuntimeError("Fail to call fit: [HTTP 500] Internal error."))


def test_gemma_over_its_quota_is_flagged():
    def limited(prompt):
        raise GemmaLimited()

    client = make_client(ask_gemma=limited)
    job_id = client.post("/api/checks", data={"use_sample": "true"}).json()["job_id"]
    result = wait_for(client, job_id)["result"]
    assert result["plan"] and result["worded_by_ai"] == 0 and result["gemma_limited"] is True
    note = client.post(f"/api/checks/{job_id}/note", json={"language": "English"}).json()
    assert note["gemma_limited"] is True and note["worded_by_ai"] == 0

    # gemma failing for other reasons is not flagged
    off = make_client(ask_gemma=lambda prompt: None)
    result = wait_for(off, off.post("/api/checks", data={"use_sample": "true"}).json()["job_id"])["result"]
    assert result["worded_by_ai"] == 0 and result["gemma_limited"] is False


def test_google_429_reports_the_limit():
    class Reply:
        def __init__(self, status, text=""):
            self.status_code, self.text = status, text

        def raise_for_status(self):
            import requests
            raise requests.HTTPError(str(self.status_code))

    hits = []
    assert ask_google_gemma("P", "k", "m", post=lambda *a, **k: Reply(429), sleep=lambda s: None,
                            on_limited=lambda: hits.append(1)) is None
    assert hits == [1]
    assert ask_google_gemma("P", "k", "m", post=lambda *a, **k: Reply(403, '{"status": "RESOURCE_EXHAUSTED"}'),
                            on_limited=lambda: hits.append(2)) is None
    assert hits == [1, 2]
    assert ask_google_gemma("P", "k", "m", post=lambda *a, **k: Reply(503), sleep=lambda s: None,
                            on_limited=lambda: hits.append(3)) is None
    assert hits == [1, 2]


def test_config_quota_shape():
    client = make_client(read_usage=usage(2_000_000), now=lambda: DAY)
    assert client.get("/api/config").json()["quota"] == {"live_checks_left_today": 20,
                                                         "resets": "2026-10-04T00:00:00+00:00"}
    used_up = make_client(read_usage=usage(2_000_000), now=lambda: DAY, daily_credits=0)
    assert used_up.get("/api/config").json()["quota"]["live_checks_left_today"] == 0
    assert used_up.post("/api/checks", data={"use_sample": "true"}).json()["quota"] is True
    assert make_client(tabpfn_backend="local").get("/api/config").json()["quota"] is None


def test_max_items_default_is_25():
    assert Settings().max_items == 25 and Settings.from_env({}).max_items == 25
    assert Settings.from_env({"TABPFN_BACKEND": "local"}).max_items is None
    env = Settings.from_env({"CREDIT_RESERVE": "1", "DAILY_CREDITS": "0", "CREDITS_PER_CALL": "7",
                             "RECORDED_SAMPLE": "no"})
    assert (env.credit_reserve, env.daily_credits, env.credits_per_call, env.recorded_sample) == (1, 0, 7, False)
    assert Settings().credit_reserve == 500_000 and Settings().daily_credits == 2_000_000
