"""
Verify the always-on crew telemetry (kai/core/crew_trace.py) actually lands the
data needed to debug coverage dispatch — the decisions that were previously
invisible because flow_rec only persists when FLOW_TRACE is on.

Drives the coverage scenario (Otto FINISHes early, triage matched two domains, so
coverage force-dispatches both) with mocked models — no Ollama — then reads the
turn back out of the DB and asserts the coverage accounting is recorded, with
FLOW_TRACE explicitly OFF.
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("KAI_ENTRYPOINT", "test")
os.environ.setdefault("KAI_TEST_MODE", "1")

import kai.config as cfg

# Redirect the DB to a temp file BEFORE anything opens a connection.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
cfg.DB_PATH = Path(_tmp.name)

from kai.store.db import _reset_for_tests

_reset_for_tests()

from kai.core import crew_trace


def _drain(gen):
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value


def _specialist_brain(chat_responses):
    from kai.core.brain import Brain
    from kai.llm.ollama import OllamaClient
    from kai.memory.manager import MemoryManager
    from kai.tools import registry
    mock = MagicMock(spec=OllamaClient)
    mock.embed.return_value = [0.0] * 2560
    mock.installed_models.return_value = [cfg.CHAT_MODEL, "granite4.1:8b"]
    mock.chat.side_effect = chat_responses
    memory = MemoryManager(embed_fn=lambda t: [0.0] * 2560)
    return Brain(memory=memory, tool_registry=registry, ollama=mock)


def test_coverage_dispatch_is_recorded_to_the_db(monkeypatch):
    # The whole point: telemetry lands even with the verbose flow trace OFF.
    monkeypatch.setattr(cfg, "FLOW_TRACE", False)

    brain = _specialist_brain([
        {"message": {"content": "FINISH: nothing to do"}},   # Otto — premature
        {"message": {"content": "disk is 82% full"}},        # Dewey (forced by coverage)
        {"message": {"content": "FINISH: got disk"}},        # Otto — still premature
        {"message": {"content": "no containers running"}},   # Cargo (forced by coverage)
        {"message": {"content": "FINISH: done"}},            # Otto — all covered → stop
    ])
    trace_id = "test-cov-" + os.urandom(4).hex()
    _drain(brain._crew.run_crew(
        "check my disk space and what containers are running",
        expected=("Dewey", "Cargo"), trace_id=trace_id,
    ))

    steps = crew_trace.for_turn(trace_id)
    kinds = [s["kind"] for s in steps]

    # Both skipped domains were force-dispatched and recorded.
    cov = [s for s in steps if s["kind"] == "coverage_dispatch"]
    assert {s["specialist"] for s in cov} == {"Dewey", "Cargo"}

    # Each dispatch is tagged with why it ran (coverage, not Otto's choice).
    dispatches = [s for s in steps if s["kind"] == "dispatch"]
    assert dispatches and all(s["source"] == "coverage" for s in dispatches)

    # Specialist outcomes captured.
    assert kinds.count("specialist_result") == 2

    # The finish record has the full coverage accounting for post-hoc analysis.
    finish = [s for s in steps if s["kind"] == "finish"]
    assert len(finish) == 1
    f = finish[0]
    assert f["expected"] == ["Dewey", "Cargo"]
    assert f["dispatched"] == ["Cargo", "Dewey"]   # sorted
    assert f["uncovered"] == []                     # nothing left uncovered
    assert f["coverage_dispatches"] == 2


def test_record_and_read_roundtrip():
    # Direct round-trip of the triage kind (what run_turn writes), incl. list/dict
    # payloads and the score table.
    tid = "test-tri-" + os.urandom(4).hex()
    crew_trace.record(
        tid, "triage", session_id="s1", profile="Profile.BOSS", lane="boss",
        expected=["Dewey", "Cargo"],
        scores=[["disk_analysis", 0.714], ["system_health", 0.638]],
    )
    steps = crew_trace.for_turn(tid)
    assert len(steps) == 1
    assert steps[0]["expected"] == ["Dewey", "Cargo"]
    assert steps[0]["scores"][0] == ["disk_analysis", 0.714]
