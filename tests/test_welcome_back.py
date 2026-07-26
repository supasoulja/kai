"""
Wave 5d/5e — welcome-back note persistence (past turn 1, and across crashes).
Run with: python -m pytest tests/test_welcome_back.py -v
"""

import os

import pytest

os.environ.setdefault("KAI_TEST_MODE", "1")

import tempfile
from pathlib import Path

import kai.config as cfg

cfg.DB_PATH = Path(tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)
# Isolate the welcome-back / checkpoint files to a temp dir.
_TMP_MEM = Path(tempfile.mkdtemp())
cfg.MEMORY_DIR = _TMP_MEM

from kai.store.db import _reset_for_tests

_reset_for_tests()

from kai.core import sleep
from kai.memory import context

# Point the sleep module's file paths at the temp dir (they were bound at import).
sleep._WELCOME_BACK_FILE = _TMP_MEM / "welcome_back.txt"
sleep._SLEEP_LOG_FILE = _TMP_MEM / "sleep_log.txt"
sleep._CHECKPOINT_FILE = _TMP_MEM / "session_checkpoint.txt"


@pytest.fixture(autouse=True)
def _clean():
    for f in (sleep._WELCOME_BACK_FILE, sleep._CHECKPOINT_FILE):
        if f.exists():
            f.unlink()
    context._welcome_back_used = False
    context._session_welcome_back = ""
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
    assert sleep._CHECKPOINT_FILE.exists()
    # Simulate next startup after a crash (no clean welcome_back was written).
    sleep.promote_checkpoint_on_startup()
    note = sleep.load_welcome_back()
    assert note is not None and "parser" in note
    assert not sleep._CHECKPOINT_FILE.exists()  # consumed


def test_clean_welcome_back_supersedes_checkpoint():
    sleep.checkpoint_session([{"role": "user", "content": "half-finished thought"}])
    sleep.save_welcome_back("clean note from a graceful shutdown")
    sleep.promote_checkpoint_on_startup()
    # The clean note wins; the stale checkpoint is discarded.
    assert sleep.load_welcome_back() == "clean note from a graceful shutdown"
    assert not sleep._CHECKPOINT_FILE.exists()


def test_clean_shutdown_clears_checkpoint():
    sleep.checkpoint_session([{"role": "user", "content": "anything"}])
    sleep.clear_checkpoint()
    assert not sleep._CHECKPOINT_FILE.exists()
