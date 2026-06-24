"""
Wave 5a — the regex fact extractor must not poison singleton facts.
Run with: python -m pytest tests/test_extractor.py -v
"""
import os
import pytest

os.environ.setdefault("KAI_TEST_MODE", "1")

import tempfile
from pathlib import Path
from datetime import datetime, timedelta

import kai.config as cfg
cfg.DB_PATH = Path(tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)

from kai.store.db import _reset_for_tests, get_conn
_reset_for_tests()

from kai.memory import semantic, extractor
from kai.memory import context


@pytest.fixture(autouse=True)
def _clean():
    for f in semantic.list_facts(user_id=7):
        semantic.delete_fact(f.key, user_id=7)
    yield


# ── The data-destroying case the backlog called out ──────────────────────────

def test_casual_state_does_not_clobber_known_role():
    semantic.set_fact("user_role", "developer", source="user_message",
                      confidence=0.5, user_id=7)
    extractor.extract_and_save("I'm a bit tired today", user_id=7)
    assert semantic.get_fact("user_role", user_id=7) == "developer"


def test_state_phrase_is_not_saved_as_role_when_none_exists():
    extractor.extract_and_save("I'm a bit tired", user_id=7)
    assert semantic.get_fact("user_role", user_id=7) is None


def test_low_confidence_capture_cannot_overwrite_explicit_setting():
    # An explicit, high-confidence fact (e.g. from a user setting) is protected.
    semantic.set_fact("user_role", "architect", source="user_setting",
                      confidence=1.0, user_id=7)
    extractor.extract_and_save("I'm a developer", user_id=7)
    assert semantic.get_fact("user_role", user_id=7) == "architect"


# ── Still captures legitimately ──────────────────────────────────────────────

def test_first_role_capture_sets_it():
    saved = extractor.extract_and_save("I'm a developer", user_id=7)
    assert ("user_role", "developer") in saved
    assert semantic.get_fact("user_role", user_id=7) == "developer"


def test_name_updates_with_high_confidence():
    extractor.extract_and_save("My name is Alice", user_id=7)
    extractor.extract_and_save("My name is Bob", user_id=7)
    assert semantic.get_fact("user_name", user_id=7) == "Bob"


def test_repeated_identical_capture_is_noop():
    extractor.extract_and_save("My name is Alice", user_id=7)
    saved = extractor.extract_and_save("My name is Alice", user_id=7)
    assert saved == []  # already stored, nothing re-written


def test_preferences_still_accumulate():
    extractor.extract_and_save("I like coffee", user_id=7)
    extractor.extract_and_save("I like hiking", user_id=7)
    prefs = {f.value for f in semantic.list_facts(user_id=7) if f.key.startswith("preference")}
    assert "coffee" in prefs and "hiking" in prefs


def test_confidence_is_stored_per_pattern():
    extractor.extract_and_save("My name is Alice", user_id=7)   # 1.0
    extractor.extract_and_save("I'm a developer", user_id=7)    # 0.5
    by_key = {f.key: f for f in semantic.list_facts(user_id=7)}
    assert by_key["user_name"].confidence == 1.0
    assert by_key["user_role"].confidence == 0.5


# ── 5b: confidence gate on recall ────────────────────────────────────────────

def test_recall_gate_excludes_low_confidence_facts():
    semantic.set_fact("preference_1", "coffee", confidence=0.3, user_id=7)
    semantic.set_fact("user_name", "Alice", confidence=1.0, user_id=7)
    block = context.build(query="hello", user_id=7)
    keys = {f.key for f in block.semantic}
    assert "user_name" in keys
    assert "preference_1" not in keys  # below RECALL_CONFIDENCE_MIN


def test_recall_gate_keeps_boundary_confidence():
    semantic.set_fact("user_role", "developer", confidence=0.5, user_id=7)
    block = context.build(query="hello", user_id=7)
    assert "developer" in {f.value for f in block.semantic}


# ── 5c: decay / review ───────────────────────────────────────────────────────

def _age_fact(key, days, user_id=7):
    conn = get_conn()
    old = (datetime.now() - timedelta(days=days)).isoformat()
    conn.execute("UPDATE semantic_facts SET updated_at = ? WHERE user_id = ? AND key = ?",
                 (old, user_id, key))
    conn.commit()


def test_review_decays_stale_low_confidence_fact():
    semantic.set_fact("user_role", "developer", confidence=0.5, user_id=7)
    _age_fact("user_role", 40)
    stats = semantic.review_facts(user_id=7)
    by_key = {f.key: f for f in semantic.list_facts(user_id=7)}
    assert by_key["user_role"].confidence == 0.4
    assert stats["decayed"] == 1


def test_review_leaves_fresh_facts_alone():
    semantic.set_fact("user_role", "developer", confidence=0.5, user_id=7)  # fresh
    semantic.review_facts(user_id=7)
    by_key = {f.key: f for f in semantic.list_facts(user_id=7)}
    assert by_key["user_role"].confidence == 0.5


def test_review_never_decays_explicit_facts():
    semantic.set_fact("user_name", "Alice", confidence=1.0, user_id=7)
    _age_fact("user_name", 100)
    semantic.review_facts(user_id=7)
    by_key = {f.key: f for f in semantic.list_facts(user_id=7)}
    assert by_key["user_name"].confidence == 1.0


def test_review_purges_faded_fact():
    semantic.set_fact("user_role", "guess", confidence=0.35, user_id=7)
    _age_fact("user_role", 40)
    stats = semantic.review_facts(user_id=7)  # 0.35 - 0.1 = 0.25 < 0.3 floor → purge
    assert semantic.get_fact("user_role", user_id=7) is None
    assert stats["purged"] >= 1


def test_review_caps_preference_accumulation():
    for i in range(25):
        semantic.set_fact(f"preference_{i}", f"v{i}", confidence=0.7, user_id=7)
    semantic.review_facts(user_id=7, pref_cap=20)
    prefs = [f for f in semantic.list_facts(user_id=7) if f.key.startswith("preference")]
    assert len(prefs) == 20
