"""
Global app state — set once at startup by web.py or cli.py.
Lets tools access the embed function and current user without circular imports.

Thread-local user_id:
  web.py sets the current user_id before every tool dispatch so tools can
  scope DB queries per-user without changing their function signatures.
"""

import threading
from collections.abc import Callable

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


def set_current_session_id(session_id: "str | None") -> None:
    """Set the active session id for the current thread (before tool dispatch).

    Lets memory tools tell the *current* conversation apart from past ones — e.g.
    recent_sessions excludes the live session so 'what were we doing last?' returns
    the previous session, not this one.
    """
    _local.session_id = session_id


def get_current_session_id() -> "str | None":
    """Get the active session id for the current thread. None if not set."""
    return getattr(_local, "session_id", None)
