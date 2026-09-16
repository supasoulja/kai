"""The approval card as a state machine: UI redesign step 3.

A parked call carries an id. A card's Go ahead or Skip acts only on that call, so an older card
can never run or cancel a newer one, and a reloaded page can find the call still waiting.
"""

from types import SimpleNamespace

from kai.core.brain import STALE_APPROVAL_TEXT, Brain
from kai.skills.registry import SkillRegistry
from tests.test_grounding_paths import (
    _brain,
    _isolated,  # noqa: F401 — autouse: no real embedder
    _ollama,
    _registry,
)
from tests.test_isolation import _batch, _Engine, _identity_tools


def test_a_parked_call_carries_an_id_the_card_can_name():
    eng = _Engine(SkillRegistry(), _identity_tools())
    _, _, events, outcome = _batch(eng, "system.kill_process", {"name": "x"})
    assert outcome.confirm_intercepted
    assert events[0]["call_id"] and events[0]["call_id"] == eng._pending_confirm["call_id"]


def _park(brain, call_id):
    brain._pending_confirm = {
        "name": "system.kill_process",
        "args": {"name": "x"},
        "label": "Stopping a process",
        "call_id": call_id,
    }


def test_an_older_cards_go_ahead_never_runs_the_call_waiting_now():
    reg = _registry(["system.kill_process"], "stopped")
    brain = _brain(_ollama([], "Stopped it."), reg)
    _park(brain, "newer")
    out = list(brain.run_stream("go ahead", confirm_call_id="older"))
    assert "".join(t for t, done, _ in out if not done) == STALE_APPROVAL_TEXT
    reg.execute.assert_not_called()
    assert brain._pending_confirm["call_id"] == "newer"


def test_the_matching_cards_go_ahead_runs_its_call():
    reg = _registry(["system.kill_process"], "stopped")
    brain = _brain(_ollama([], "Stopped the process."), reg)
    _park(brain, "card-1")
    out = list(brain.run_stream("go ahead", confirm_call_id="card-1"))
    reg.execute.assert_called_once_with("system.kill_process", {"name": "x"})
    receipts = [m["tool_result"] for _, _, m in out if m.get("tool_result")]
    assert receipts and receipts[0]["call_id"] == "card-1"
    assert brain._pending_confirm is None


def test_skip_on_an_older_card_leaves_the_newer_call_alone():
    b = SimpleNamespace(_pending_confirm={"name": "system.kill_process", "call_id": "newer"})
    assert Brain.cancel_pending_confirm(b, "older") is None and b._pending_confirm
    assert Brain.cancel_pending_confirm(b, "newer") == "system.kill_process"
    assert b._pending_confirm is None


def test_the_waiting_call_is_summarised_for_a_reloaded_page():
    pending = {"name": "n", "label": "l", "args": {"a": 1}, "call_id": "c", "trace_id": "t"}
    assert Brain.pending_confirm_summary(SimpleNamespace(_pending_confirm=pending)) == {
        "call_id": "c",
        "name": "n",
        "label": "l",
        "args": {"a": 1},
    }
    assert Brain.pending_confirm_summary(SimpleNamespace(_pending_confirm=None)) is None
