"""
Phase 0/1 tests — memory foundation, no LLM required.
Run with: python -m pytest tests/test_memory.py -v
"""
import os
import pytest

# Use a temp DB for tests so they don't touch real data
os.environ.setdefault("KAI_TEST_MODE", "1")

import tempfile
from pathlib import Path

# Patch DB_PATH before importing anything else
import kai.config as cfg
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
cfg.DB_PATH = Path(_tmp.name)

from kai.db import _reset_for_tests
_reset_for_tests()

from kai.memory import semantic, procedural, episodic, extractor
from kai.memory.manager import MemoryManager
from kai.identity import build_identity_block, seed_founding_entry


# ── Semantic ────────────────────────────────────────────────────────────────────

def test_semantic_set_and_get():
    semantic.set_fact("user_name", "Alex")
    assert semantic.get_fact("user_name") == "Alex"


def test_semantic_upsert():
    semantic.set_fact("user_name", "Alex")
    semantic.set_fact("user_name", "Jim")
    assert semantic.get_fact("user_name") == "Jim"


def test_semantic_delete():
    semantic.set_fact("temp_key", "temp_value")
    semantic.delete_fact("temp_key")
    assert semantic.get_fact("temp_key") is None


def test_semantic_list():
    semantic.set_fact("lang", "Python")
    facts = semantic.list_facts()
    keys = [f.key for f in facts]
    assert "lang" in keys


# ── Procedural ──────────────────────────────────────────────────────────────────

def test_procedural_set_and_get():
    procedural.set_rule("tone", "direct")
    assert procedural.get_rule("tone") == "direct"


def test_procedural_list():
    procedural.set_rule("response_length", "brief")
    rules = procedural.list_rules()
    keys = [r.key for r in rules]
    assert "response_length" in keys


# ── Episodic ────────────────────────────────────────────────────────────────────

def test_episodic_add_and_recent():
    entry_id = episodic.add_entry("Testing episodic memory", entry_type="turn")
    assert entry_id is not None
    recent = episodic.recent(limit=5)
    contents = [e.content for e in recent]
    assert "Testing episodic memory" in contents


def test_episodic_text_search():
    episodic.add_entry("Alex talked about his Python project")
    results = episodic.search("Python project")
    assert len(results) > 0
    assert any("Python" in r.content for r in results)


# ── Extractor ───────────────────────────────────────────────────────────────────

def test_extractor_name():
    saved = extractor.extract_and_save("My name is Alex")
    keys = [k for k, v in saved]
    assert "user_name" in keys
    assert semantic.get_fact("user_name") == "Alex"


def test_extractor_call_me():
    saved = extractor.extract_and_save("Call me Jay")
    keys = [k for k, v in saved]
    assert "user_name" in keys


def test_extractor_no_false_positives():
    saved = extractor.extract_and_save("What time is it?")
    assert saved == []


# ── MemoryManager ───────────────────────────────────────────────────────────────

def test_manager_commit_turn():
    mm = MemoryManager()
    mm.commit_turn(
        user_text="My name is Alex and I use Python",
        assistant_text="Got it."
    )
    assert mm.get_fact("user_name") == "Alex"
    recent = mm.recent_episodes(limit=3)
    assert any("Alex" in e.content for e in recent)


def test_manager_render_context():
    mm = MemoryManager()
    mm.set_fact("user_name", "Alex")
    mm.set_rule("tone", "direct")
    rendered = mm.render_context("Python")
    assert "[SEMANTIC]" in rendered
    assert "[PROCEDURAL]" in rendered


# ── Identity ────────────────────────────────────────────────────────────────────

def test_identity_seed_and_load():
    seed_founding_entry()
    block = build_identity_block()
    assert len(block) > 0
    # Seeding twice shouldn't duplicate
    seed_founding_entry()


# ── Cleanup ─────────────────────────────────────────────────────────────────────

def teardown_module(module):
    try:
        os.unlink(_tmp.name)
    except Exception:
        pass
