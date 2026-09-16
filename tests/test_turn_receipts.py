"""Honest turn metadata: UI redesign step 2 (receipts and provenance).

Every tool that runs yields a receipt; the done event and the saved message carry the
receipts plus how far the final answer is backed by them (T2-23, T2-25, T2-28, P1-1).
"""

import json
import sqlite3

from kai.core.brain import Brain
from kai.core.engine import tool_receipt
from kai.memory import cerebellum as cb
from kai.store import sessions
from tests.test_grounding_paths import (
    _brain,
    _call,
    _isolated,  # noqa: F401 — autouse: no real embedder, empty learn queue
    _ollama,
    _registry,
)


def test_a_receipt_shows_the_call_and_hides_secrets():
    r = tool_receipt(
        "web.fetch",
        {"url": "x" * 200, "api_key": "sk-123", "n": 3},
        {"success": True, "output": "ok"},
        42,
    )
    assert r["args"]["api_key"] == "•••" and r["args"]["n"] == 3
    assert len(r["args"]["url"]) == 118 and r["args"]["url"].endswith("…")
    assert r["ok"] and r["output"] == "ok" and r["duration_ms"] == 42
    failed = tool_receipt(
        "files.read", {}, {"success": False, "error": "Not found", "kind": "unavailable"}, 5
    )
    assert not failed["ok"] and failed["error"] == "Not found" and failed["output"] == ""


def test_number_trace_counts_and_lists_strays():
    assert cb.number_trace("disk 55.3% of 312.1 GB, 999 files", "55.3% used of 312.1 GB") == (
        3,
        [999.0],
    )
    assert cb.number_trace("anything 5", "no numbers here") == (0, [])


def test_a_tool_turn_yields_receipts_and_a_grounded_done_event():
    tool_out = '{"disk_root_filesystem_only": "55.3% used (163.7/312.1 GB)"}'
    ollama = _ollama(
        [
            _call("system.info"),
            {"message": {"content": "Disk is fine."}},
            {"message": {"content": "- disk 55.3% used (163.7/312.1 GB)"}},
        ],
        "Your disk is 55.3% used (163.7 of 312.1 GB).",
    )
    brain = _brain(ollama, _registry(["system.info"], tool_out))
    out = list(brain.run_stream("check my system, how full is my disk"))
    receipts = [m["tool_result"] for _, done, m in out if m.get("tool_result")]
    assert [r["name"] for r in receipts] == ["system.info"] and receipts[0]["ok"]
    done = out[-1][2]
    assert done["grounding"] == {"state": "grounded", "checked": 3, "traced": 3}
    assert done["receipts"][0]["name"] == "system.info"
    saved = sessions.get_messages(brain.session_id, user_id=brain.user_id)
    assert saved[-1]["meta"]["grounding"]["state"] == "grounded"
    assert saved[-1]["meta"]["receipts"][0]["name"] == "system.info"


def _stub(receipts):
    b = object.__new__(Brain)
    b._turn_receipts = receipts
    return b


def _tool_msg(name, output, success=True):
    return {
        "role": "tool",
        "content": json.dumps({"success": success, "output": output, "_source": name}),
    }


def test_provenance_states():
    ok = [{"name": "system.info", "ok": True}]
    msgs = [_tool_msg("system.info", "55.3% used of 312.1 GB")]
    assert _stub([])._grounding_for(msgs, "DNS maps names to addresses.", False) == {
        "state": "unchecked"
    }
    assert _stub(ok)._grounding_for(msgs, "anything", True) == {"state": "waiting"}
    assert _stub(ok)._grounding_for(msgs, "Your disk looks healthy.", False) == {"state": "ran"}
    partial = _stub(ok)._grounding_for(msgs, "55.3% used; 91 GB free; 12 GB cache", False)
    assert (
        partial["state"] == "partial"
        and partial["checked"] == 3
        and partial["untraced"] == [91.0, 12.0]
    )
    failed = _stub([{"name": "files.read", "ok": False}])._grounding_for(
        [], "It says hello.", False
    )
    assert failed == {"state": "tool_failed", "failed": ["files.read"]}
    reads = [_tool_msg("files.read", "hello")]
    assert _stub(ok)._grounding_for(reads, "I deleted the file.", False) == {"state": "unbacked"}


def test_the_watch_passes_yields_through_and_keeps_receipts():
    def gen():
        got = yield "a", False, {}
        yield "", False, {"tool_result": {"name": "time.now", "ok": True, "sent": got}}
        return "finished"

    b = _stub([])
    w = b._watch(gen())
    assert next(w) == ("a", False, {})
    assert w.send("hi")[2]["tool_result"]["sent"] == "hi"
    try:
        next(w)
    except StopIteration as stop:
        assert stop.value == "finished"
    assert [r["name"] for r in b._turn_receipts] == ["time.now"]


def test_old_databases_gain_the_meta_column(tmp_path):
    from kai.store.db import _maybe_add_message_meta_column

    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute(
        "CREATE TABLE session_messages (id INTEGER PRIMARY KEY, role TEXT, latency_ms INTEGER)"
    )
    _maybe_add_message_meta_column(conn)
    assert "meta" in {r[1] for r in conn.execute("PRAGMA table_info(session_messages)")}
