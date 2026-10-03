"""Wording the plan in a language. The slots, examples and fact check in note_wording.py apply to every Gemma."""
import pandas as pd

from core.note_wording import friendly_lines
from web.repositories.wording import WordingGateway


class WordingService:
    def __init__(self, gateway: WordingGateway):
        self.gateway = gateway

    def lines(self, plan: pd.DataFrame, language: str) -> list[tuple[str, str | None]]:
        """(plain line, AI line or None) per plan row. With no Gemma set up, every line stays plain."""
        return friendly_lines(plan, language, self.gateway.ask(), self.gateway.workers)
