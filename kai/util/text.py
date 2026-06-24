"""
Small text helpers shared across modules.

Kept separate from brain.py so lightweight callers (e.g. sleep.py) can reuse
them without importing the whole conversation engine.
"""
import re

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def strip_thinking(text: str) -> tuple[str, str]:
    """Split a model response into (thinking, clean).

    `thinking` is the content of the first <think>…</think> block (stripped);
    `clean` is the text with all <think> blocks removed.
    """
    match = _THINK_RE.search(text)
    thinking = match.group(1).strip() if match else ""
    clean = _THINK_RE.sub("", text).strip()
    return thinking, clean
