"""Wording the plan in a language. The slots, examples and fact check in note_wording.py apply to every Gemma."""
import threading

import pandas as pd

from core.note_wording import friendly_lines
from web.repositories.wording import WordingGateway


class WordingService:
    def __init__(self, gateway: WordingGateway):
        self.gateway = gateway

    def lines(self, plan: pd.DataFrame, language: str) -> list[tuple[str, str | None]]:
        """(plain line, AI line or None) per plan row. With no Gemma set up, every line stays plain."""
        return self.worded(plan, language)[0]

    def worded(self, plan: pd.DataFrame, language: str,
               seconds: float | None = None) -> tuple[list[tuple[str, str | None]], bool]:
        """The lines, and whether every line stayed plain because Gemma's free quota was used up.

        seconds caps gemma's whole share. lines it has not worded by then stay plain.
        """
        deadline = None if seconds is None else self.gateway.clock() + seconds
        hits, lock = [0], threading.Lock()

        def limited() -> None:
            with lock:
                hits[0] += 1
        lines = friendly_lines(plan, language, self.gateway.ask(limited, deadline), self.gateway.workers)
        all_plain = bool(lines) and all(ai is None for _, ai in lines)
        return lines, all_plain and hits[0] >= len(lines)
