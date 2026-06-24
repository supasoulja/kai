"""
Wave 3 — per-user privacy controls for silent background learning.
Run with: python -m pytest tests/test_privacy.py -v
"""
import os
import pytest

os.environ.setdefault("KAI_TEST_MODE", "1")

import tempfile
from pathlib import Path

# Patch DB_PATH before importing anything that opens a connection.
import kai.config as cfg
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
cfg.DB_PATH = Path(_tmp.name)

from kai.store.db import _reset_for_tests, get_conn
_reset_for_tests()

from kai.memory import privacy


@pytest.fixture(autouse=True)
def _clean_facts():
    # Each test starts from the default (unset) state for both users used here.
    conn = get_conn()
    conn.execute("DELETE FROM semantic_facts WHERE key LIKE 'privacy_%'")
    conn.execute("DELETE FROM usage_patterns")
    conn.commit()
    yield


def test_defaults_follow_config_constants():
    cfg.LEARN_FROM_CONVERSATION = True
    cfg.PATTERN_ENABLED = True
    assert privacy.learning_enabled(user_id=1) is True
    assert privacy.patterns_enabled(user_id=1) is True


def test_default_honors_a_false_config_constant():
    cfg.PATTERN_ENABLED = False
    try:
        assert privacy.patterns_enabled(user_id=1) is False
    finally:
        cfg.PATTERN_ENABLED = True


def test_explicit_opt_out_overrides_default_on():
    privacy.set_learning_enabled(1, False)
    privacy.set_patterns_enabled(1, False)
    assert privacy.learning_enabled(1) is False
    assert privacy.patterns_enabled(1) is False


def test_explicit_opt_in_overrides_default_off():
    cfg.PATTERN_ENABLED = False
    try:
        privacy.set_patterns_enabled(1, True)
        assert privacy.patterns_enabled(1) is True
    finally:
        cfg.PATTERN_ENABLED = True


def test_preferences_are_per_user():
    privacy.set_patterns_enabled(1, False)
    # User 2 never expressed a preference → still the default.
    assert privacy.patterns_enabled(1) is False
    assert privacy.patterns_enabled(2) is True


def test_forget_usage_patterns_deletes_only_that_user():
    conn = get_conn()
    for uid in (1, 1, 2):
        conn.execute(
            "INSERT INTO usage_patterns (user_id, tool_name, topic, hour_of_day, day_of_week, ts) "
            "VALUES (?, 'system.temps', NULL, 9, 1, 0)",
            (uid,),
        )
    conn.commit()

    removed = privacy.forget_usage_patterns(user_id=1)
    assert removed == 2
    remaining = conn.execute("SELECT COUNT(*) FROM usage_patterns").fetchone()[0]
    assert remaining == 1  # user 2's row untouched
