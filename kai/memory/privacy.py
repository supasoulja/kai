"""
Per-user privacy controls for Kai's silent background learning.

Two subsystems quietly build a profile of the user with no explicit opt-in:

  - conversation learning — knowledge extraction after a turn
    (``config.LEARN_FROM_CONVERSATION``)
  - usage-pattern tracking — which tools you run at what time of day
    (``config.PATTERN_ENABLED``)

They default ON (those config constants are the defaults), but each user can now
turn either one off, and the accumulated usage-pattern profile can be wiped.

Preferences live in ``semantic_facts`` (per-user, and already erased by
``delete_user``), so there's no new table or migration. The config constant is
the default whenever a user hasn't expressed a preference.

The HTTP routes / Settings UI that flip these are wired in the Settings-reorg
phase of the Agent-Crew epic; this module is the backend they call.
"""

from __future__ import annotations

import kai.config as cfg
from kai.memory import semantic

_LEARN_KEY = "privacy_learn_from_conversation"
_PATTERN_KEY = "privacy_usage_patterns"


def _enabled(key: str, default: bool, user_id: int) -> bool:
    val = semantic.get_fact(key, user_id=user_id)
    if val is None:
        return default
    return val == "on"


def learning_enabled(user_id: int = 0) -> bool:
    """Whether knowledge should be extracted from this user's conversations."""
    return _enabled(_LEARN_KEY, cfg.LEARN_FROM_CONVERSATION, user_id)


def patterns_enabled(user_id: int = 0) -> bool:
    """Whether this user's tool-usage patterns should be recorded."""
    return _enabled(_PATTERN_KEY, cfg.PATTERN_ENABLED, user_id)


def set_learning_enabled(user_id: int, on: bool) -> None:
    semantic.set_fact(_LEARN_KEY, "on" if on else "off", source="privacy_setting", user_id=user_id)


def set_patterns_enabled(user_id: int, on: bool) -> None:
    semantic.set_fact(
        _PATTERN_KEY, "on" if on else "off", source="privacy_setting", user_id=user_id
    )


def forget_usage_patterns(user_id: int = 0) -> int:
    """Delete every recorded usage pattern for a user. Returns rows removed.

    The deletion path the backlog asked for — turning tracking off stops new
    rows; this clears the history already gathered.
    """
    from kai.store.db import get_conn

    conn = get_conn()
    cur = conn.execute("DELETE FROM usage_patterns WHERE user_id = ?", (user_id,))
    conn.commit()
    return cur.rowcount
