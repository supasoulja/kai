"""
Global app state — set once at startup by web.py or cli.py.
Lets tools access the embed function without circular imports.
"""
from typing import Callable

_embed_fn: Callable[[str], list[float]] | None = None


def set_embed_fn(fn: Callable[[str], list[float]]) -> None:
    global _embed_fn
    _embed_fn = fn


def get_embed_fn() -> Callable[[str], list[float]] | None:
    return _embed_fn
