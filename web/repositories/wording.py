"""Where Gemma runs: Google's Gemini API (the web default), Ollama on this machine, or nowhere (plain lines)."""
import logging
import re
import time
from functools import partial

import requests

from core.note_wording import ask_ollama

log = logging.getLogger(__name__)
RETRY_STATUS = {429, 500, 502, 503, 504}
BACKOFF = (1.0, 2.5)  # seconds before the second and third try
GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# gemma 4 thinks before it answers. "minimal" cut a line from about 11 s to 2 s with the same wording
LESS_THINKING = {"thinkingConfig": {"thinkingLevel": "minimal"}}
LABEL = re.compile(r"^(hindi|english|hinglish)\s*:\s*", re.IGNORECASE)  # with full thinking it repeats "Hindi:"
QUOTA_TEXT = re.compile(r"quota|resource_exhausted|rate.?limit", re.IGNORECASE)


class GemmaLimited(Exception):
    """Gemma refused for quota or rate reasons. A stand-in model may raise it, the real one reports it."""


def google_answer(body: dict) -> str | None:
    """The answer text in a generateContent reply. Gemma 4 sends its thinking first, in parts marked thought."""
    try:
        parts = body["candidates"][0]["content"]["parts"]
        answers = [part["text"] for part in parts if not part.get("thought") and isinstance(part.get("text"), str)]
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    return LABEL.sub("", answers[-1].strip()).strip('"').strip() if answers else None


GOOGLE_TIMEOUT = 40  # seconds for one line, retries included


def ask_google_gemma(prompt: str, api_key: str, model: str, post=requests.post, timeout: float = GOOGLE_TIMEOUT,
                     sleep=time.sleep, clock=time.monotonic, on_limited=None) -> str | None:
    """Gemma through Google's API. None when it does not answer. Gemma there takes a plain prompt, no system part.

    A busy or failing answer (429, 5xx, timeout, dropped connection) is asked again, all within timeout seconds.
    on_limited is called when it gave up because the free quota or rate limit was hit.
    """
    def limited() -> None:
        if on_limited is not None:
            on_limited()

    deadline, extra, retries = clock() + timeout, LESS_THINKING, 0
    while True:
        try:
            reply = post(GOOGLE_URL.format(model=model), headers={"x-goog-api-key": api_key},
                         json={"contents": [{"parts": [{"text": prompt}]}],
                               "generationConfig": {"temperature": 0.2, **extra}},
                         timeout=min(timeout, max(deadline - clock(), 1)))
            # a model that refuses the thinking setting answers 400, then it is asked again without it
            if reply.status_code == 400 and extra:
                extra = {}
                continue
            if reply.status_code not in RETRY_STATUS:
                if reply.status_code >= 400 and QUOTA_TEXT.search(getattr(reply, "text", "") or ""):
                    log.info("gemma gave no line: http %s, over its quota", reply.status_code)
                    limited()
                    return None
                reply.raise_for_status()
                return google_answer(reply.json())
            reason = f"http {reply.status_code}"
        except (requests.Timeout, requests.ConnectionError) as error:
            reason = type(error).__name__
        except (requests.RequestException, ValueError) as error:
            log.info("gemma gave no line: %s", type(error).__name__)
            return None
        # a retry is only worth it with a few seconds left for the answer
        if retries == len(BACKOFF) or clock() + BACKOFF[retries] + 5 > deadline:
            log.info("gemma gave no line: %s, out of retries", reason)
            if reason == "http 429":
                limited()
            return None
        log.info("gemma %s, asking again", reason)
        sleep(BACKOFF[retries])
        retries += 1


MIN_SECONDS_LEFT = 3  # too little time for an answer, so the line stays plain


class WordingGateway:
    def __init__(self, backend: str, api_key: str | None, model: str, ask=None, clock=time.monotonic):
        """ask replaces the real model, for tests."""
        self.backend, self.api_key, self.model, self._ask, self.clock = backend, api_key, model, ask, clock

    def status(self) -> str:
        if self._ask is not None:
            return self.backend
        if self.backend == "google":
            return "google" if self.api_key else "off"
        return "ollama" if self.backend == "ollama" else "off"

    @property
    def workers(self) -> int:
        # each Google call takes seconds, so 4 lines go at once, well under its 30 a minute
        return 4 if self.status() == "google" else 1

    def ask(self, on_limited=None, deadline: float | None = None):
        """The function that sends a prompt to Gemma, or None to keep every line plain.

        on_limited is called once per prompt that got no line because Gemma's quota or rate limit was hit.
        deadline (on self.clock) ends all asking: a slow or retrying gemma cannot hold the check past it.
        """
        ask = self._ask_for(on_limited)
        if ask is None or deadline is None:
            return ask
        google = self._ask is None and self.status() == "google"

        def in_time(prompt):
            left = deadline - self.clock()
            if left < MIN_SECONDS_LEFT:
                return None
            if google:
                return ask_google_gemma(prompt, api_key=self.api_key, model=self.model, on_limited=on_limited,
                                        timeout=min(GOOGLE_TIMEOUT, left))
            return ask(prompt)
        return in_time

    def _ask_for(self, on_limited=None):
        if self._ask is not None:
            if on_limited is None:
                return self._ask
            stand_in = self._ask

            def ask(prompt):
                try:
                    return stand_in(prompt)
                except GemmaLimited:
                    on_limited()
                    return None
            return ask
        return {"google": partial(ask_google_gemma, api_key=self.api_key, model=self.model, on_limited=on_limited),
                "ollama": ask_ollama}.get(self.status())
