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

from kai.store.db import _reset_for_tests

_reset_for_tests()

from kai.memory import episodic, extractor, procedural, semantic
from kai.memory.manager import MemoryManager
from kai.persona.identity import build_identity_block

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
    mm.commit_turn(user_text="My name is Alex and I use Python", assistant_text="Got it.")
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
    block = build_identity_block()
    assert len(block) > 0


# ── Recent-session recall (the memory-across-resets fix) ──────────────────────────


def test_recent_sessions_recalls_previous_and_excludes_current():
    """'What were we doing last?' must recall the PREVIOUS session by recency and
    leave out the live one. Before this, there was no recency-based recall path —
    search_history is keyword-only, so Kai claimed she had no record."""
    from kai.core._app_state import set_current_session_id, set_current_user_id
    from kai.store import sessions as S
    from kai.tools.memory.memory_tools import recent_sessions

    set_current_user_id(0)

    prev = S.new_session("help me clean up disk space", user_id=0)
    S.append_message(prev, "user", "help me clean up disk space", 0, user_id=0)
    S.append_message(prev, "assistant", "Found old VMs eating space.", 1, user_id=0)
    S.append_message(prev, "user", "revisit the audio setup this weekend", 2, user_id=0)

    cur = S.new_session("what were we doing last", user_id=0)
    S.append_message(cur, "user", "what were we doing last", 0, user_id=0)

    set_current_session_id(cur)
    out = recent_sessions()
    assert "disk space" in out  # recalls the previous session
    assert "audio setup this weekend" in out  # surfaces where it left off
    assert "what were we doing last" not in out  # excludes the live session

    # Excluding a different session surfaces the other one — recency, not a fixed list.
    set_current_session_id(prev)
    other = recent_sessions(limit=1)
    assert "what were we doing last" in other
    assert "disk space" not in other


def test_append_message_stores_latency():
    """Assistant messages persist their generation time; user messages don't."""
    from kai.store import sessions as S

    sid = S.new_session("timing test", user_id=0)
    S.append_message(sid, "user", "how long did that take", 0, user_id=0)
    S.append_message(sid, "assistant", "About two seconds.", 1, user_id=0, latency_ms=2345)

    msgs = S.get_messages(sid, user_id=0)
    by_role = {m["role"]: m for m in msgs}
    assert by_role["assistant"]["latency_ms"] == 2345
    assert by_role["user"]["latency_ms"] is None


# ── Per-user DB connection cache (tree.py / state.py) ────────────────────────────


def test_tree_connection_is_cached_and_reused(tmp_path, monkeypatch):
    """Repeated tree ops on the same user reuse one connection instead of
    reopening the file every call (the old `with _connect(...)` leak)."""
    from kai.memory import tree as mtree

    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)

    c1 = mtree._conn("77")
    c2 = mtree._conn("77")
    assert c1 is c2, "second call should reuse the cached connection"

    # Different user → different connection (keyed by resolved path).
    assert mtree._conn("88") is not c1

    # Eviction reopens a fresh connection (so the file can be deleted).
    mtree._close("77")
    assert mtree._conn("77") is not c1


# ── Archive atomicity (episodic.archive_and_clear_turns) ─────────────────────────


def test_archive_clears_turns_and_keeps_archive_with_transcript():
    """Archiving must, in one transaction, drop the raw turns AND leave exactly one
    archive entry plus its transcript — never both turns and archive, never an
    orphaned transcript."""
    uid = 9991  # isolate from other tests sharing the temp DB
    episodic.add_entry("turn one", entry_type="turn", user_id=uid)
    episodic.add_entry("turn two", entry_type="turn", user_id=uid)
    assert episodic.get_pending_turns_text(user_id=uid)  # turns are staged

    mm = MemoryManager(user_id=uid)
    mm.archive_history("summary of the two turns")

    # No raw turns remain
    assert episodic.get_pending_turns_text(user_id=uid) == ""
    # Exactly one archive entry, and its transcript is retrievable
    archives = episodic.search_non_turns("summary", user_id=uid)
    assert len(archives) == 1
    assert episodic.get_transcript(archives[0].id, user_id=uid) is not None


def test_archive_rolls_back_if_turn_delete_fails(monkeypatch):
    """If the turn-deletion step raises mid-transaction, the raw turns must survive
    rather than being half-archived."""
    uid = 9992
    episodic.add_entry("keep me", entry_type="turn", user_id=uid)

    from kai.store.db import get_conn

    real_conn = get_conn()

    class FailingConn:
        def execute(self, sql, *args, **kwargs):
            if sql.strip().startswith("DELETE FROM episodic_entries"):
                raise RuntimeError("simulated delete failure")
            return real_conn.execute(sql, *args, **kwargs)

        def commit(self):
            return real_conn.commit()

        def rollback(self):
            return real_conn.rollback()

    monkeypatch.setattr("kai.memory.episodic.get_conn", lambda: FailingConn())
    with pytest.raises(RuntimeError):
        episodic.archive_and_clear_turns("summary", user_id=uid)

    monkeypatch.undo()  # restore the real get_conn before asserting
    assert "keep me" in episodic.get_pending_turns_text(user_id=uid)


# ── State JSON deserialization is corruption-tolerant (state.py) ─────────────────


def test_state_load_ignores_unknown_and_corrupt_json(tmp_path, monkeypatch):
    """A state blob written before a field changed (extra key) or an outright
    corrupt blob must fall back to defaults, not crash the read."""
    from kai.memory import state as mstate

    monkeypatch.setattr(mstate, "_STATE_DIR", tmp_path)

    # Stale/extra key alongside a valid one — unknown key is dropped, valid kept.
    conn = mstate._conn("u1")
    import json as _json
    import time as _time

    conn.execute(
        "INSERT INTO state VALUES (?, ?, ?)",
        ("user", _json.dumps({"terseness": 0.9, "obsolete_field": "x"}), _time.time()),
    )
    conn.commit()
    loaded = mstate.load_user_state("u1")
    assert loaded.terseness == 0.9
    assert not hasattr(loaded, "obsolete_field")

    # Outright garbage JSON → defaults, no exception. (Compare meaningful fields,
    # not last_updated, which defaults to time.time() and so always differs.)
    conn.execute(
        "INSERT INTO state VALUES (?, ?, ?)",
        ("kai", "{not valid json", _time.time()),
    )
    conn.commit()
    loaded_kai = mstate.load_kai_state("u1")
    defaults = mstate.KaiState()
    assert loaded_kai.self_confidence == defaults.self_confidence
    assert loaded_kai.intuition_active == defaults.intuition_active


def test_state_connection_is_cached(tmp_path, monkeypatch):
    from kai.memory import state as mstate

    monkeypatch.setattr(mstate, "_STATE_DIR", tmp_path)
    assert mstate._conn("5") is mstate._conn("5")


# ── Cleanup ─────────────────────────────────────────────────────────────────────


def teardown_module(module):
    try:
        os.unlink(_tmp.name)
    except Exception:
        pass
