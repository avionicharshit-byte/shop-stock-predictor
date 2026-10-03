import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gemma3:4b"
# a small model writes much steadier lines when it is shown two finished examples to follow
EXAMPLES = {
    "Hinglish": [
        ("[ITEM] will run out of stock by Monday, order [QTY] pieces.",
         "[ITEM] ka stock Monday tak khatam ho jayega, [QTY] pieces order kar lo."),
        ("[ITEM] is out of stock, order [PACKS] packs ([QTY] pieces).",
         "[ITEM] ka stock khatam ho gaya hai, [PACKS] packs ([QTY] pieces) order kar lo."),
    ],
    "Hindi": [
        ("[ITEM] will run out of stock by Monday, order [QTY] pieces.",
         "[ITEM] का स्टॉक सोमवार तक खत्म हो जाएगा, [QTY] पीस ऑर्डर कर लें।"),
        ("[ITEM] is out of stock, order [PACKS] packs ([QTY] pieces).",
         "[ITEM] का स्टॉक खत्म हो गया है, [PACKS] पैक ([QTY] पीस) ऑर्डर कर लें।"),
    ],
    "English": [
        ("[ITEM] will run out of stock by Monday, order [QTY] pieces.",
         "[ITEM] will finish by Monday, please order [QTY] pieces."),
        ("[ITEM] is out of stock, order [PACKS] packs ([QTY] pieces).",
         "[ITEM] is finished, please order [PACKS] packs ([QTY] pieces)."),
    ],
}
HINDI_DAYS = {"Monday": "सोमवार", "Tuesday": "मंगलवार", "Wednesday": "बुधवार", "Thursday": "गुरुवार",
              "Friday": "शुक्रवार", "Saturday": "शनिवार", "Sunday": "रविवार"}


TEMPLATE = "{item} will run out of stock by {day}, order {qty} pieces."
TEMPLATE_ALREADY_OUT = "{item} is out of stock, order {qty} pieces."
TEMPLATE_ALREADY_OUT_WITH_PACKS = "{item} is out of stock, order {packs} packs ({qty} pieces)."
TEMPLATE_WITH_PACKS = "{item} will run out of stock by {day}, order {packs} packs ({qty} pieces)."
# the AI never sees or retypes the item name or the numbers, it words around these slots and code fills them
SLOTS = {"item": "[ITEM]", "qty": "[QTY]", "packs": "[PACKS]"}


def _template(row) -> str:
    # no pack size known means packs == pieces, so talk in pieces only
    in_pieces = row.order_packs == row.order_units
    if row.stock_left == 0:
        return TEMPLATE_ALREADY_OUT if in_pieces else TEMPLATE_ALREADY_OUT_WITH_PACKS
    return TEMPLATE if in_pieces else TEMPLATE_WITH_PACKS


def _day(row) -> str:
    return f"{row.runs_out_on:%A}"


def _fill(text: str, row) -> str:
    values = {"item": row.item, "qty": str(row.order_units), "packs": str(row.order_packs)}
    for name, slot in SLOTS.items():
        text = text.replace(slot, values[name])
    return re.sub(r"\b1 (piece|pack)s\b", r"1 \1", text)


def _plain_line(row) -> str:
    return _fill(_template(row).format(day=_day(row), **SLOTS), row)


def plain_note(plan: pd.DataFrame) -> str:
    if plan.empty:
        return "Nothing will run out this week."
    return "\n".join(_plain_line(row) for row in plan.itertuples())


def _keeps_the_facts(line: str, row) -> bool:
    """The AI only words the line. Every slot must survive exactly once, with the right day and no stray digits."""
    slots = [slot for name, slot in SLOTS.items() if "{" + name + "}" in _template(row)]
    days_named = {name for name, hindi in HINDI_DAYS.items() if name.lower() in line.lower() or hindi in line}
    days_expected = {_day(row)} if "{day}" in _template(row) else set()
    return (all(line.count(slot) == 1 for slot in slots) and days_named == days_expected
            and not any(char.isdigit() for char in line) and line.count("[") == len(slots))


def ask_ollama(prompt: str) -> str | None:
    """Gemma on this laptop through Ollama. None when it does not answer."""
    try:
        reply = requests.post(OLLAMA_URL, json={"model": MODEL, "prompt": prompt, "stream": False,
                                                 "options": {"temperature": 0.2}}, timeout=180)
        reply.raise_for_status()
        return reply.json()["response"]
    except (requests.RequestException, KeyError):
        return None


Ask = Callable[[str], str | None]  # sends a prompt to Gemma, wherever it runs, and returns its reply or None


def _reworded(row, language: str, ask: Ask = ask_ollama) -> str | None:
    examples = "\n\n".join(f"Alert: {alert}\n{language}: {reworded}" for alert, reworded in EXAMPLES[language])
    prompt = (f"Rewrite each stock alert for an Indian shopkeeper in {language}. Keep every [SLOT] exactly as it is "
              f"and keep the same day.\n\n{examples}\n\nAlert: "
              + _template(row).format(day=_day(row), **SLOTS) + f"\n{language}:")
    # a line that fails the fact check is asked for once more, then it stays plain
    for _ in range(2):
        reply = ask(prompt)
        if not isinstance(reply, str):
            return None  # no answer at all. retrying that belongs to ask, which knows why
        line = reply.strip().strip('"')
        if "\n" not in line and _keeps_the_facts(line, row):
            return _fill(line, row)
    return None


def friendly_lines(plan: pd.DataFrame, language: str = "Hindi", ask: Ask | None = ask_ollama,
                   workers: int = 1) -> list[tuple[str, str | None]]:
    """(plain line, AI line or None) per plan row. ask=None skips the AI and keeps every line plain.

    workers above 1 sends that many lines at once, for a Gemma that runs elsewhere.
    """
    rows = list(plan.itertuples())
    if not ask:
        return [(_plain_line(row), None) for row in rows]
    with ThreadPoolExecutor(max(1, workers)) as pool:
        worded = list(pool.map(lambda row: _reworded(row, language, ask), rows))
    return [(_plain_line(row), line) for row, line in zip(rows, worded)]


def friendly_note(plan: pd.DataFrame, language: str = "Hindi", ask: Ask | None = ask_ollama,
                  workers: int = 1) -> tuple[str, int]:
    """Returns (note, how many lines the AI wrote). A line that fails the fact check stays plain."""
    if plan.empty:
        return plain_note(plan), 0
    lines = friendly_lines(plan, language, ask, workers)
    return "\n".join(ai or plain for plain, ai in lines), sum(ai is not None for _, ai in lines)
