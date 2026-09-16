"""
Wave 5d/5e — welcome-back note persistence (past turn 1, and across crashes), and
per-account isolation (T1-40).
Run with: python -m pytest tests/test_welcome_back.py -v
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("KAI_TEST_MODE", "1")

import tempfile
from pathlib import Path

import kai.config as cfg

cfg.DB_PATH = Path(tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)

from kai.store.db import _reset_for_tests

_reset_for_tests()

from kai.core import sleep
from kai.memory import context


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    # sleep resolves its paths from cfg.MEMORY_DIR at call time.
    monkeypatch.setattr(cfg, "MEMORY_DIR", tmp_path)
    context._welcome_back_used.clear()
    context._session_welcome_back.clear()
    context._briefing_used.clear()
    yield


# ── 5d: survives past turn 1 ─────────────────────────────────────────────────


def test_welcome_back_retrievable_after_file_cleared():
    sleep.save_welcome_back("pick up the X migration next time")
    # Turn 1 consumes it…
    first = context._get_and_clear_welcome_back()
    assert "X migration" in first
    # …and a successful response clears the file.
    context.mark_welcome_back_delivered()
    assert sleep.load_welcome_back() is None
    # But it's still recallable for the rest of the session.
    assert "X migration" in context.get_session_welcome_back()


def test_second_turn_greeting_is_empty_but_note_still_available():
    sleep.save_welcome_back("remember the Y refactor")
    context._get_and_clear_welcome_back()  # turn 1
    assert context._get_and_clear_welcome_back() == ""  # not re-greeted
    assert "Y refactor" in context.get_session_welcome_back()


def test_get_session_welcome_back_falls_back_to_disk_before_consumed():
    sleep.save_welcome_back("unconsumed note")
    # Never called _get_and_clear_welcome_back this session.
    assert "unconsumed note" in context.get_session_welcome_back()


# ── 5e: survives hard kills / crashes ────────────────────────────────────────


def test_checkpoint_promoted_when_no_clean_welcome_back():
    history = [
        {"role": "user", "content": "let's debug the parser"},
        {"role": "assistant", "content": "looking at the tokenizer now"},
    ]
    sleep.checkpoint_session(history)
    assert sleep.checkpoint_file().exists()
    # Simulate next startup after a crash (no clean welcome_back was written).
    sleep.promote_checkpoint_on_startup()
    note = sleep.load_welcome_back()
    assert note is not None and "parser" in note
    assert not sleep.checkpoint_file().exists()  # consumed


def test_clean_welcome_back_supersedes_checkpoint():
    sleep.checkpoint_session([{"role": "user", "content": "half-finished thought"}])
    sleep.save_welcome_back("clean note from a graceful shutdown")
    sleep.promote_checkpoint_on_startup()
    # The clean note wins; the stale checkpoint is discarded.
    assert sleep.load_welcome_back() == "clean note from a graceful shutdown"
    assert not sleep.checkpoint_file().exists()


def test_clean_shutdown_clears_checkpoint():
    sleep.checkpoint_session([{"role": "user", "content": "anything"}])
    sleep.clear_checkpoint()
    assert not sleep.checkpoint_file().exists()


# ── Per account (T1-40) ──────────────────────────────────────────────────────


def test_one_accounts_note_is_never_given_to_another():
    """T1-40: after a restart, account 2's greeting delivered account 1's private note,
    and account 1 then got none."""
    sleep.save_welcome_back("repainting the Blue Heron canoe", user_id=1)
    assert context._get_and_clear_welcome_back(user_id=2) == ""
    context.mark_welcome_back_delivered(user_id=2)
    assert "Blue Heron" not in context.get_session_welcome_back(user_id=2)
    assert "Blue Heron" in context._get_and_clear_welcome_back(user_id=1)


def test_briefing_one_shot_is_per_account():
    with patch("kai.memory.briefing.get_pending", side_effect=lambda user_id: f"brief {user_id}"):
        assert context._get_and_clear_briefing(user_id=1) == "brief 1"
        assert context._get_and_clear_briefing(user_id=2) == "brief 2"
        assert context._get_and_clear_briefing(user_id=1) == ""


def test_sleep_cycle_writes_the_brains_own_account():
    history = [
        {"role": "user", "content": "let's plan the garden beds for spring"},
        {"role": "assistant", "content": "raised beds along the south fence, then"},
    ]
    brain = SimpleNamespace(user_id=7, model="m", snapshot_history=lambda: history)
    ollama = MagicMock()
    ollama.chat.return_value = {"message": {"content": "Last session: planned raised garden beds."}}
    with patch("kai.memory.semantic.review_facts", return_value={"decayed": 0, "purged": 0}):
        sleep.run_sleep_cycle(ollama, brain)
    assert "garden" in sleep.load_welcome_back(7)
    assert sleep.load_welcome_back(0) is None
    assert "garden" in sleep.sleep_log_file(7).read_text()


def test_each_accounts_checkpoint_becomes_its_own_note():
    sleep.checkpoint_session([{"role": "user", "content": "account three parser work"}], user_id=3)
    sleep.checkpoint_session([{"role": "user", "content": "account four budget sheet"}], user_id=4)
    sleep.promote_checkpoint_on_startup()
    assert "parser" in sleep.load_welcome_back(3) and "budget" not in sleep.load_welcome_back(3)
    assert "budget" in sleep.load_welcome_back(4)


def test_old_shared_files_go_to_the_owner(monkeypatch):
    (cfg.MEMORY_DIR / "welcome_back.txt").write_text("old shared note")
    (cfg.MEMORY_DIR / "sleep_log.txt").write_text("\n--- 2026-09-01 10:00 ---\nold entry\n")
    monkeypatch.setattr("kai.store.users.get_owner_id", lambda: 1)
    sleep.promote_checkpoint_on_startup()
    assert sleep.load_welcome_back(1) == "old shared note"
    assert not (cfg.MEMORY_DIR / "welcome_back.txt").exists()
    assert "old entry" in sleep.sleep_log_file(1).read_text()


def test_sleep_notes_tool_reads_only_the_callers_journal():
    from kai.core._app_state import set_current_user_id
    from kai.tools.memory.memory_tools import sleep_notes

    sleep.save_welcome_back("account five's private journal entry", user_id=5)
    try:
        set_current_user_id(6)
        assert "private journal" not in sleep_notes()
        set_current_user_id(5)
        assert "private journal" in sleep_notes()
    finally:
        set_current_user_id(0)
