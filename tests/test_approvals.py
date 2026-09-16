"""Approvals are real state — regressions from the 2026-09-10 hardening pass.

Each test names the tester finding it pins (audits/hardening-2026-09-10/). Built on
plain stubs, so no DB, model or network is needed.
"""

import json
import threading
from types import SimpleNamespace

import pytest

from kai.core.brain import _CONFIRM_RE, Brain, _evidence_from, _is_failure_marker
from kai.core.engine import TurnEngine


@pytest.mark.parametrize(
    "text",
    [
        "yes",
        "Yes.",
        "yes please",
        "yes, go ahead",
        "go ahead",
        "Go ahead!",
        "do it",
        "confirm",
        "approved",
        "run it",
    ],
)
def test_explicit_consent_confirms(text):
    assert _CONFIRM_RE.match(text)


@pytest.mark.parametrize(
    "text",
    [
        "ok",
        "okay",
        "sure",
        "right",
        "please",
        "alright",
        "mhm",
        "y",
        "scan",
        "yes but first explain what it does",
        "no",
    ],
)
def test_ambiguous_replies_do_not_confirm(text):
    """T1-26: "right", "please" and "alright" each ran a parked system.kill_process."""
    assert not _CONFIRM_RE.match(text)


class _History:
    def clear(self):
        pass

    def replace(self, messages):
        return len(messages)


def _brain_stub():
    return SimpleNamespace(
        _history=_History(),
        session_id="s1",
        _pending_confirm={"name": "system.kill_process", "args": {}, "label": "Killing process"},
        _cancel=threading.Event(),
    )


def test_new_chat_drops_a_parked_call():
    """T1-25 / P1-4: kill_process parked, New chat, then "ok" ran it."""
    b = _brain_stub()
    Brain.clear_history(b)
    assert b._pending_confirm is None


def test_loading_a_session_drops_a_parked_call():
    b = _brain_stub()
    Brain.load_session(b, "s2", [])
    assert b._pending_confirm is None


def test_skip_cancels_the_parked_call():
    """T1-11 / T2-24: Skip only hid the bar; the call stayed parked server-side."""
    b = _brain_stub()
    assert Brain.cancel_pending_confirm(b) == "system.kill_process"
    assert b._pending_confirm is None
    assert Brain.cancel_pending_confirm(b) is None


def test_parked_placeholder_is_not_evidence():
    """T1-15: grounding read the ⏸ placeholder as proof a file had been deleted."""
    msgs = [
        {
            "role": "tool",
            "content": json.dumps(
                {"output": "⏸ sandbox.approve requires your OK", "success": True, "pending": True}
            ),
        },
        {"role": "tool", "content": json.dumps({"output": "disk 55.3% used", "success": True})},
    ]
    evidence = _evidence_from(msgs, limit=None)
    assert "55.3" in evidence
    assert "requires your OK" not in evidence


def test_fallback_never_claims_done():
    """T1-20: "Done — used system.kill_process." after the tool said it isn't available."""
    stub = SimpleNamespace(_cancel=threading.Event())
    text = Brain._fallback_text(stub, ["system.kill_process"])
    assert "Done" not in text
    assert "system.kill_process" in text
    assert _is_failure_marker(text), "the placeholder must not be replayed into history"
    assert not _is_failure_marker("I ran system.info and your disk is fine.")


class _Engine(TurnEngine):
    session_id = None
    user_id = 0
    _pending_confirm = None
    _cancel = threading.Event()

    def __init__(self):
        pass


def _run_batch(engine, tool_calls):
    messages, tools_used, events = [], [], []
    gen = engine._execute_tool_calls(
        tool_calls,
        messages,
        tools_used,
        [],
        0,
        query_emb=None,
        user_input="kill it",
        trace_id="t",
        on_status=None,
        sig_of=lambda name, args: name + json.dumps(args, sort_keys=True),
    )
    try:
        while True:
            events.append(next(gen)[2])
    except StopIteration as stop:
        return messages, tools_used, events, stop.value


def test_parked_call_is_not_counted_as_used_and_carries_its_args():
    """T2-23: "1 action taken" for a scan that never ran. T1-11: the bar never showed the args."""
    eng = _Engine()
    call = {"function": {"name": "system.kill_process", "arguments": {"process_name": "qa-proc"}}}
    messages, tools_used, events, outcome = _run_batch(eng, [call])
    assert outcome.confirm_intercepted
    assert tools_used == []
    assert events[0]["args"] == {"process_name": "qa-proc"}
    assert json.loads(messages[-1]["content"])["pending"] is True


def test_approving_a_persona_edit_writes_the_file(tmp_path, monkeypatch):
    """T1-43: the decorator sat on the helper, so approval returned a tuple and wrote nothing."""
    import kai.tools  # noqa: F401 — registers every tool
    from kai.tools.registry import registry
    from kai.tools.system import self_inspect

    persona = tmp_path / "persona.md"
    persona.write_text("# Kai\n\n## Voice\n\nDirect.\n", encoding="utf-8")
    monkeypatch.setattr(self_inspect, "PERSONA_PATH", persona)
    out = registry.execute(
        "self.apply_persona_update", {"section": "QA Probe", "content": "probe line"}
    )
    assert isinstance(out, str) and "added" in out
    assert "## QA Probe" in persona.read_text(encoding="utf-8")
