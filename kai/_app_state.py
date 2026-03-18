"""
Global app state — set once at startup by web.py or cli.py.
Lets tools access the embed function and current user without circular imports.

Thread-local user_id:
  web.py sets the current user_id before every tool dispatch so tools can
  scope DB queries per-user without changing their function signatures.
"""
import threading
from typing import Callable

_embed_fn: Callable[[str], list[float]] | None = None
_local = threading.local()


def set_embed_fn(fn: Callable[[str], list[float]]) -> None:
    global _embed_fn
    _embed_fn = fn


def get_embed_fn() -> Callable[[str], list[float]] | None:
    return _embed_fn


def set_current_user_id(uid: int) -> None:
    """Set the user_id for the current thread (called before tool dispatch)."""
    _local.user_id = uid


def get_current_user_id() -> int:
    """Get the user_id for the current thread. Returns 0 if not set."""
    return getattr(_local, "user_id", 0)
