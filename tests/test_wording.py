import pandas as pd
import pytest

from core.note_wording import _keeps_the_facts, _template, friendly_lines, friendly_note
from web.repositories.wording import WordingGateway, ask_google_gemma, google_answer


def plan(stock_left=10, packs=2, units=24, day="2026-10-05"):  # 5 Oct 2026 is a Monday
    return pd.DataFrame([{"item": "Maggi 70g", "stock_left": stock_left, "likely_to_sell": 30, "busy_week": 40,
                          "runs_out_on": pd.Timestamp(day), "order_packs": packs, "order_units": units}])


ROW = next(plan().itertuples())


def test_fact_check_accepts_a_faithful_line():
    assert _template(ROW) == "{item} will run out of stock by {day}, order {packs} packs ({qty} pieces)."
    assert _keeps_the_facts("[ITEM] ka stock Monday tak khatam, [PACKS] packs ([QTY] pieces) order kar lo.", ROW)
    assert _keeps_the_facts("[ITEM] का स्टॉक सोमवार तक खत्म, [PACKS] पैक ([QTY] पीस) ऑर्डर कर लें।", ROW)


@pytest.mark.parametrize("line", [
    "[ITEM] ka stock Monday tak khatam, [QTY] pieces order kar lo.",                 # drops [PACKS]
    "[ITEM] ka stock Tuesday tak khatam, [PACKS] packs ([QTY] pieces) order kar lo.",  # wrong day
    "[ITEM] का स्टॉक मंगलवार तक खत्म, [PACKS] पैक ([QTY] पीस) ऑर्डर कर लें।",           # wrong day in Hindi
    "[ITEM] ka stock Monday tak khatam, 2 packs ([QTY] pieces) order kar lo.",         # retypes a number
    "[ITEM] [ITEM] Monday, [PACKS] packs ([QTY] pieces).",                             # a slot twice
])
def test_fact_check_rejects_a_changed_line(line):
    assert not _keeps_the_facts(line, ROW)
    plain, worded = friendly_lines(plan(), "Hinglish", ask=lambda prompt: line)[0]
    assert worded is None and plain == "Maggi 70g will run out of stock by Monday, order 2 packs (24 pieces)."


def test_a_good_line_is_filled_and_counted():
    note, by_ai = friendly_note(plan(), "Hinglish", ask=lambda prompt: '"[ITEM] Monday tak, [PACKS] packs ([QTY] pieces)."')
    assert (note, by_ai) == ("Maggi 70g Monday tak, 2 packs (24 pieces).", 1)
    assert friendly_note(plan(), "Hindi", ask=lambda prompt: None) == (
        "Maggi 70g will run out of stock by Monday, order 2 packs (24 pieces).", 0)
    assert friendly_lines(plan(), "Hindi", ask=None, workers=4)[0][1] is None


def NO_SLEEP(seconds):
    pass


class Reply:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self.body


GEMMA_4_REPLY = {"candidates": [{"content": {"role": "model", "parts": [
    {"text": "The user wants Hindi. Keep [ITEM] and Monday...", "thought": True},
    {"text": ' Hindi: "[ITEM] का स्टॉक सोमवार तक खत्म हो जाएगा, [QTY] पीस ऑर्डर कर लें।"\n'}]}}]}


def test_google_answer_skips_thinking_and_labels():
    assert google_answer(GEMMA_4_REPLY) == "[ITEM] का स्टॉक सोमवार तक खत्म हो जाएगा, [QTY] पीस ऑर्डर कर लें।"
    assert google_answer({"candidates": [{"content": {"parts": [{"text": "a"}, {"text": "b"}]}}]}) == "b"
    assert google_answer({"candidates": [{"content": {"parts": [{"text": "x", "thought": True}]}}]}) is None
    assert google_answer({"candidates": []}) is None
    assert google_answer({"promptFeedback": {"blockReason": "OTHER"}}) is None


def test_ask_google_gemma_request_and_fallbacks():
    calls = []

    def post(url, headers, json, timeout):
        calls.append((url, headers, json, timeout))
        return Reply(200, GEMMA_4_REPLY)

    answer = ask_google_gemma("PROMPT", api_key="k", model="gemma-4-26b-a4b-it", post=post, sleep=NO_SLEEP)
    assert answer.startswith("[ITEM]")
    url, headers, body, timeout = calls[0]
    assert url == "https://generativelanguage.googleapis.com/v1beta/models/gemma-4-26b-a4b-it:generateContent"
    assert headers == {"x-goog-api-key": "k"} and "k" not in url
    assert body["contents"] == [{"parts": [{"text": "PROMPT"}]}] and body["generationConfig"]["temperature"] == 0.2
    assert "systemInstruction" not in body and 39 < timeout <= 40

    # a model that refuses the thinking setting is asked again without it
    replies = iter([Reply(400, {}), Reply(200, GEMMA_4_REPLY)])
    sent = []
    assert ask_google_gemma("P", "k", "m", post=lambda url, headers, json, timeout: sent.append(json) or next(replies))
    assert "thinkingConfig" in sent[0]["generationConfig"] and "thinkingConfig" not in sent[1]["generationConfig"]

    assert ask_google_gemma("P", "k", "m", post=lambda *a, **k: Reply(429, {}), sleep=NO_SLEEP) is None
    assert ask_google_gemma("P", "k", "m", post=lambda *a, **k: Reply(200, {"oops": 1})) is None

    def down(*args, **kwargs):
        import requests
        raise requests.ConnectionError("down")
    assert ask_google_gemma("P", "k", "m", post=down, sleep=NO_SLEEP) is None


def test_google_gemma_through_the_fact_check():
    gateway = WordingGateway("google", "k", "m")
    assert gateway.status() == "google" and gateway.workers == 4
    ask = lambda prompt: google_answer(GEMMA_4_REPLY)
    line_plan = plan(packs=24, units=24)  # pieces only, so the [QTY] line fits
    _, worded = friendly_lines(line_plan, "Hindi", ask)[0]
    assert worded == "Maggi 70g का स्टॉक सोमवार तक खत्म हो जाएगा, 24 पीस ऑर्डर कर लें।"
    assert WordingGateway("google", None, "m").ask() is None
    assert WordingGateway("off", "k", "m").status() == "off"


def replies(*answers):
    """A post that answers each call with the next reply, or raises it when it is an exception."""
    sent = []

    def post(url, headers, json, timeout):
        answer = answers[len(sent)]
        sent.append(timeout)
        if isinstance(answer, Exception):
            raise answer
        return answer
    return post, sent


@pytest.mark.parametrize("failure", [Reply(500, {}), Reply(503, {}), Reply(429, {})])
def test_busy_or_failing_gemma_is_asked_again(failure):
    import requests
    slept = []
    post, sent = replies(failure, requests.Timeout("slow"), Reply(200, GEMMA_4_REPLY))
    assert ask_google_gemma("P", "k", "m", post=post, sleep=slept.append).startswith("[ITEM]")
    assert len(sent) == 3 and slept == [1.0, 2.5]

    # three failures in a row: it gives up, the line stays plain
    post, sent = replies(failure, failure, failure, Reply(200, GEMMA_4_REPLY))
    assert ask_google_gemma("P", "k", "m", post=post, sleep=NO_SLEEP) is None and len(sent) == 3


def test_retries_stay_inside_the_time_budget():
    now = [0.0]
    clock = lambda: now[0]

    def post(url, headers, json, timeout):
        now[0] += 36  # the first try used most of the 40 seconds
        sent.append(timeout)
        return Reply(500, {})
    sent = []
    assert ask_google_gemma("P", "k", "m", post=post, sleep=NO_SLEEP, clock=clock) is None
    assert sent == [40]

    # a retry only gets the seconds that are left
    now[0], sent = 0.0, []
    answers = iter([Reply(500, {}), Reply(200, GEMMA_4_REPLY)])

    def post_twice(url, headers, json, timeout):
        sent.append(timeout)
        now[0] += 10
        return next(answers)
    assert ask_google_gemma("P", "k", "m", post=post_twice, sleep=lambda s: now.__setitem__(0, now[0] + s),
                            clock=clock)
    assert sent == [40, 29.0]


def test_client_errors_are_not_retried():
    post, sent = replies(Reply(403, {}), Reply(200, GEMMA_4_REPLY))
    assert ask_google_gemma("P", "k", "m", post=post, sleep=NO_SLEEP) is None and len(sent) == 1


def test_a_line_failing_the_fact_check_is_asked_once_more():
    good, bad = "[ITEM] Monday tak, [PACKS] packs ([QTY] pieces).", "[ITEM] Tuesday tak, 2 packs."
    asked = []

    def gemma(*lines):
        def ask(prompt):
            asked.append(prompt)
            return lines[len(asked) - 1]
        return ask
    assert friendly_lines(plan(), "Hinglish", gemma(bad, good))[0][1] == "Maggi 70g Monday tak, 2 packs (24 pieces)."
    assert len(asked) == 2 and asked[0] == asked[1]
    asked.clear()
    assert friendly_lines(plan(), "Hinglish", gemma(bad, bad, good))[0][1] is None and len(asked) == 2
    asked.clear()
    # no answer at all is not asked again here, the gateway already retried it
    assert friendly_lines(plan(), "Hinglish", gemma(None, good))[0][1] is None and len(asked) == 1
