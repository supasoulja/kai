"""
Phase 2 tests — Brain logic with mocked Ollama.
No real Ollama connection needed.
Run with: python -m pytest tests/test_brain.py -v
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("KAI_TEST_MODE", "1")

import kai.config as cfg

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
cfg.DB_PATH = Path(_tmp.name)

from kai.store.db import _reset_for_tests

_reset_for_tests()

from kai.core.brain import Brain, OllamaClient, _strip_thinking
from kai.memory.manager import MemoryManager

# ── Helpers ─────────────────────────────────────────────────────────────────────


def make_mock_stream(response_text: str):
    """Return a side_effect callable that yields streaming tokens like chat_stream."""

    def _stream(*args, **kwargs):
        yield response_text, False, {}
        yield "", True, {}

    return _stream


def make_mock_ollama(response_text: str = "Hello from Kai.", tool_calls: list | None = None):
    """Build a mock OllamaClient that returns a preset response."""
    mock = MagicMock(spec=OllamaClient)
    mock.is_alive.return_value = True
    mock.installed_models.return_value = [cfg.CHAT_MODEL, cfg.EMBED_MODEL]
    mock.embed.return_value = [0.0] * 2560  # match episodic_vec schema dimensions

    msg = {"content": response_text}
    if tool_calls:
        msg["tool_calls"] = tool_calls

    mock.chat.return_value = {"message": msg}
    # chat_stream is used for the final streamed answer (no-tools path)
    mock.chat_stream.side_effect = make_mock_stream(response_text)
    return mock


def make_brain(response_text: str = "Hello.", tool_calls=None):
    memory = MemoryManager(embed_fn=lambda t: [0.0] * 2560)
    ollama = make_mock_ollama(response_text, tool_calls)
    return Brain(memory=memory, ollama=ollama), memory


# ── Public surface (no private poking) ───────────────────────────────────────


def test_final_temperature_property_reflects_internal():
    brain, _ = make_brain()
    brain._final_temp = 0.42
    assert brain.final_temperature == 0.42


def test_prime_indexes_seeds_tool_index_and_flags():
    brain, _ = make_brain()
    brain.prime_indexes({"system.temps": [0.1, 0.2]}, router_ready=True)
    assert brain._tool_index == {"system.temps": [0.1, 0.2]}
    assert brain._tool_index_ready is True
    assert brain._memory_router_ready is True


def test_prime_indexes_none_leaves_index_but_sets_router_flag():
    brain, _ = make_brain()
    brain.prime_indexes(None, router_ready=False)
    assert brain._tool_index_ready is False
    assert brain._memory_router_ready is False


def test_append_external_turn_adds_to_history():
    brain, _ = make_brain()
    brain.append_external_turn("user", "[Document uploaded: notes.pdf]")
    hist = brain.snapshot_history()
    assert hist[-1] == {"role": "user", "content": "[Document uploaded: notes.pdf]"}


# ── _strip_thinking ──────────────────────────────────────────────────────────────


def test_strip_thinking_extracts_think_tags():
    thinking, clean = _strip_thinking("<think>internal reasoning</think>The answer is 42.")
    assert thinking == "internal reasoning"
    assert clean == "The answer is 42."


def test_strip_thinking_no_tags():
    thinking, clean = _strip_thinking("Just a plain response.")
    assert thinking == ""
    assert clean == "Just a plain response."


def test_strip_thinking_multiline():
    text = "<think>\nline one\nline two\n</think>\nFinal answer."
    thinking, clean = _strip_thinking(text)
    assert "line one" in thinking
    assert clean == "Final answer."


# ── Basic conversation ──────────────────────────────────────────────────────────


def test_brain_returns_response():
    brain, _ = make_brain("Hey, what's up?")
    result = brain.run("Hello Kai")
    assert result == "Hey, what's up?"


def test_brain_commits_to_memory():
    brain, memory = make_brain("Got it.")
    brain.run("My name is Alex")
    # commit_turn runs on the background pool — drain() blocks until it's done.
    brain.drain()
    episodes = memory.recent_episodes(limit=1)
    assert len(episodes) == 1
    assert "Alex" in episodes[0].content


def test_brain_extracts_facts_from_user_input():
    brain, memory = make_brain("Noted.")
    brain.run("My name is Alex")
    # Fact extraction runs on the background pool — wait for it deterministically.
    brain.drain()
    assert memory.get_fact("user_name") == "Alex"


def test_brain_context_injected_into_system_prompt():
    brain, memory = make_brain("Sure.")
    memory.set_fact("user_name", "Alex")
    brain.run("What do you know about me?")
    # No tools → chat_stream is called directly (chat is not called)
    call_args = brain.ollama.chat_stream.call_args
    messages = call_args[0][0]  # first positional arg
    system_msg = messages[0]
    assert system_msg["role"] == "system"
    assert "Alex" in system_msg["content"]


# ── Tool call flow ──────────────────────────────────────────────────────────────


def test_brain_executes_tool_and_finalizes():
    """Round 1: chat() returns tool_calls → tool executed. Round 2: chat() returns final answer."""
    tool_call_response = {
        "message": {
            "content": "",
            "tool_calls": [{"function": {"name": "time.now", "arguments": {}}}],
        }
    }
    # Round 2: no tool_calls → early-exit with this content
    final_response = {"message": {"content": "It is Tuesday."}}

    mock_ollama = MagicMock(spec=OllamaClient)
    mock_ollama.embed.return_value = [0.0] * 2560
    mock_ollama.chat.side_effect = [tool_call_response, final_response]

    mock_registry = MagicMock()
    mock_registry.get_schema.return_value = [{"type": "function", "function": {"name": "time.now"}}]
    mock_registry.execute.return_value = "2026-02-20 14:00"

    memory = MemoryManager(embed_fn=lambda t: [0.0] * 2560)
    brain = Brain(memory=memory, tool_registry=mock_registry, ollama=mock_ollama)

    result = brain.run("What time is it?")
    assert result == "It is Tuesday."
    mock_registry.execute.assert_called_once_with("time.now", {})


def test_brain_handles_no_tool_registry():
    """Without a registry, no tools are offered; brain streams a normal response."""
    mock_ollama = MagicMock(spec=OllamaClient)
    mock_ollama.embed.return_value = [0.0] * 2560
    mock_ollama.chat_stream.side_effect = make_mock_stream("I can't save notes without a tool.")

    memory = MemoryManager(embed_fn=lambda t: [0.0] * 2560)
    brain = Brain(memory=memory, tool_registry=None, ollama=mock_ollama)

    result = brain.run("Save a note")
    assert result == "I can't save notes without a tool."
    # chat() should never be called — tool rounds are skipped entirely
    mock_ollama.chat.assert_not_called()


def test_brain_thinking_stripped_from_response():
    brain, _ = make_brain("<think>let me think about this...</think>Here's my answer.")
    result = brain.run("Question")
    assert "<think>" not in result
    assert result == "Here's my answer."


# ── Tool-model levels ───────────────────────────────────────────────────────────


def _tool_call_response(name="time.now", args=None):
    return {
        "message": {
            "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": args or {}}}],
        }
    }


def test_tool_rounds_use_selected_tool_model():
    """With a tool level applied, rounds run on that model (think off) and
    the final answer still streams on the chat model in Kai's voice."""
    tool_model = cfg.TOOL_MODEL_LEVELS["light"]["model"]
    mock_ollama = MagicMock(spec=OllamaClient)
    mock_ollama.embed.return_value = [0.0] * 2560
    mock_ollama.installed_models.return_value = [cfg.CHAT_MODEL, tool_model]
    mock_ollama.chat.side_effect = [
        _tool_call_response(),
        {"message": {"content": "tool-model prose — must be discarded"}},
    ]
    mock_ollama.chat_stream.side_effect = make_mock_stream("It is Tuesday.")

    mock_registry = MagicMock()
    mock_registry.get_schema.return_value = [{"type": "function", "function": {"name": "time.now"}}]
    mock_registry.execute.return_value = "2026-06-09 21:00"

    memory = MemoryManager(embed_fn=lambda t: [0.0] * 2560)
    brain = Brain(memory=memory, tool_registry=mock_registry, ollama=mock_ollama)
    brain.apply_tool_level("light")

    result = brain.run("What time is it?")
    for call in mock_ollama.chat.call_args_list:
        assert call.kwargs["model"] == tool_model
        assert call.kwargs["think"] is False
    # The tool model's prose was discarded — the reply came from the streamed chat model
    assert result == "It is Tuesday."
    assert mock_ollama.chat_stream.call_args.kwargs["model"] == cfg.CHAT_MODEL


def test_tool_rounds_fall_back_to_chat_model_no_thinking():
    """Granite not installed → rounds run on the chat model with think=False.
    Thinking is the latency killer; the pre-LLM fast-paths + narrated-intent
    recovery keep tool-calling reliable without it (the de-bloat change)."""
    mock_ollama = MagicMock(spec=OllamaClient)
    mock_ollama.embed.return_value = [0.0] * 2560
    mock_ollama.installed_models.return_value = [cfg.CHAT_MODEL]
    mock_ollama.chat.side_effect = [
        _tool_call_response(),
        {"message": {"content": "It is Tuesday."}},
    ]
    mock_registry = MagicMock()
    mock_registry.get_schema.return_value = [{"type": "function", "function": {"name": "time.now"}}]
    mock_registry.execute.return_value = "2026-06-09 21:00"

    memory = MemoryManager(embed_fn=lambda t: [0.0] * 2560)
    brain = Brain(memory=memory, tool_registry=mock_registry, ollama=mock_ollama)
    brain.apply_tool_level("light")  # granite missing → falls back

    result = brain.run("What time is it?")
    for call in mock_ollama.chat.call_args_list:
        assert call.kwargs["model"] == brain.model
        assert call.kwargs["think"] is False
    # Fallback rounds keep the direct-answer path (it IS the chat model's voice)
    assert result == "It is Tuesday."


def test_fast_path_matches_exact_commands_only():
    """The pre-LLM fast-path fires only on an exact, unambiguous whole-input
    command (so a passing mention or a compound request never short-circuits
    the model)."""
    from kai.core.brain import _match_fast_path

    # Exact commands → (tool, args). No-arg tools carry {}.
    assert _match_fast_path("what time is it?") == ("time.now", {})
    assert _match_fast_path("what's the date") == ("time.now", {})
    assert _match_fast_path("check the weather") == ("weather.current", {})
    assert _match_fast_path("show my temps") == ("system.temps", {})
    assert _match_fast_path("what's my disk usage") == ("files.disk_usage", {})
    # Weather carries the extracted location so the fast-path can't drop the city
    # (the Boise→Denver bug: a no-arg weather call returns the IP-geo city).
    assert _match_fast_path("what's the weather in boise") == (
        "weather.current",
        {"location": "boise"},
    )
    assert _match_fast_path("check the weather in London") == (
        "weather.current",
        {"location": "London"},
    )
    assert _match_fast_path("what's the weather in Austin, TX") == (
        "weather.current",
        {"location": "Austin, TX"},
    )
    # A time phrase is NOT a place → local weather ({}), not a bogus location
    assert _match_fast_path("what's the weather for tomorrow") == ("weather.current", {})
    # A real joiner still falls through, even for weather
    assert _match_fast_path("weather in Austin and my disk usage") is None
    # Anything ambiguous, compound, or detailed falls through to the LLM
    assert _match_fast_path("tell me the current time please") is None
    assert _match_fast_path("what time is it and what's the weather") is None
    assert _match_fast_path("write a poem about the weather") is None
    assert _match_fast_path("") is None


def test_fast_path_runs_tool_without_a_tool_round_model_call():
    """An exact fast-path command executes its tool directly and skips the
    tool-round model call entirely (mock_ollama.chat is never used); the answer
    is still streamed by the chat model with the result grounded in messages."""
    mock_ollama = MagicMock(spec=OllamaClient)
    mock_ollama.embed.return_value = [0.0] * 2560
    mock_ollama.installed_models.return_value = [cfg.CHAT_MODEL]
    mock_ollama.chat_stream.side_effect = make_mock_stream("It's 9 in the morning.")

    mock_registry = MagicMock()
    mock_registry.list_tools.return_value = ["time.now"]  # real list → fast-path fires
    mock_registry.get_schema.return_value = [{"type": "function", "function": {"name": "time.now"}}]
    mock_registry.execute.return_value = "2026-06-26 09:00"
    mock_registry.risk_for.return_value = "safe"

    memory = MemoryManager(embed_fn=lambda t: [0.0] * 2560)
    brain = Brain(memory=memory, tool_registry=mock_registry, ollama=mock_ollama)

    result = brain.run("what time is it?")
    mock_ollama.chat.assert_not_called()  # NO tool-round model call
    mock_registry.execute.assert_called_once()  # the tool ran deterministically
    assert mock_registry.execute.call_args[0][0] == "time.now"
    assert result == "It's 9 in the morning."


def test_duplicate_tool_calls_not_reexecuted():
    """A tool model re-issuing the exact same call must not re-run the tool —
    first repeat gets pointed at the existing result, second repeat ends the
    rounds so the answer uses the data already gathered."""
    mock_ollama = MagicMock(spec=OllamaClient)
    mock_ollama.embed.return_value = [0.0] * 2560
    mock_ollama.installed_models.return_value = [cfg.CHAT_MODEL, "granite4.1:3b"]
    mock_ollama.chat.side_effect = [
        _tool_call_response(),
        _tool_call_response(),
        _tool_call_response(),
    ]
    mock_ollama.chat_stream.side_effect = make_mock_stream("CPU is at 47C.")

    mock_registry = MagicMock()
    mock_registry.get_schema.return_value = [{"type": "function", "function": {"name": "time.now"}}]
    mock_registry.execute.return_value = "2026-06-09 21:00"

    memory = MemoryManager(embed_fn=lambda t: [0.0] * 2560)
    brain = Brain(memory=memory, tool_registry=mock_registry, ollama=mock_ollama)
    brain.apply_tool_level("light")

    result = brain.run("What time is it?")
    mock_registry.execute.assert_called_once()  # ran once, not three times
    assert mock_ollama.chat.call_count == 3  # two repeats, then rounds ended
    assert result == "CPU is at 47C."


# ── Cerebellum integration ───────────────────────────────────────────────────────


def test_cerebellum_stop_ends_rounds_without_escalation(monkeypatch):
    """A Cerebellum STOP must end the tool rounds — not re-arm the full
    schema with 'Do not give up' (the bug this guards against)."""
    import kai.llm.embed as ke
    from kai.memory import cerebellum as cb

    monkeypatch.setattr(ke, "embed", lambda t: [0.1] * 384)
    monkeypatch.setattr(cfg, "CEREBELLUM_ENABLED", True)
    monkeypatch.setattr(
        cb,
        "pre_check",
        lambda *a, **k: cb.CerebellarResult(cb.Verdict.STOP, "test stop", 0.9),
    )

    mock_ollama = MagicMock(spec=OllamaClient)
    mock_ollama.embed.return_value = [0.0] * 2560
    mock_ollama.installed_models.return_value = [cfg.CHAT_MODEL]
    mock_ollama.chat.side_effect = [_tool_call_response()]
    mock_ollama.chat_stream.side_effect = make_mock_stream("The chain was stopped.")

    mock_registry = MagicMock()
    mock_registry.get_schema.return_value = [{"type": "function", "function": {"name": "time.now"}}]

    memory = MemoryManager(embed_fn=lambda t: [0.0] * 2560)
    brain = Brain(memory=memory, tool_registry=mock_registry, ollama=mock_ollama)

    result = brain.run("What time is it?")
    assert result == "The chain was stopped."
    assert mock_ollama.chat.call_count == 1  # no second round fired
    mock_registry.execute.assert_not_called()  # the stopped tool never ran
    final_messages = json.dumps(mock_ollama.chat_stream.call_args[0][0])
    assert "safety check stopped" in final_messages
    assert "Do not give up" not in final_messages


def test_cerebellum_loop_counts_identical_calls_only(monkeypatch):
    """Only literally-identical (tool + args) repeats are a loop — reading
    several different files is progress."""
    from kai.memory import cerebellum as cb

    monkeypatch.setattr(cb, "_embed_action", lambda *a: None)  # isolate the loop check
    intent = [0.1] * 384
    same = cb.call_signature("files.read", {"path": "a.txt"})
    r = cb.pre_check("files.read", {"path": "a.txt"}, intent, [same, same, same])
    assert r.verdict == cb.Verdict.STOP
    different = [cb.call_signature("files.read", {"path": p}) for p in ("a", "b", "c")]
    r = cb.pre_check("files.read", {"path": "d"}, intent, different)
    assert r.verdict == cb.Verdict.CLEAR


# ── Narrated tool-call recovery ──────────────────────────────────────────────────


def test_narrated_recovery_guards():
    from kai.core.engine import _try_recover_tool_call

    known = {"files.read", "pc.deep_scan"}
    # Intent markers ("let me", "I'll use") fire recovery
    assert _try_recover_tool_call("Let me files.read that for you.", known) is not None
    # A "?" in a LATER sentence doesn't block recovery
    assert _try_recover_tool_call("I'll use pc.deep_scan now. Anything else?", known) is not None
    # A question to the user must NOT fire the tool
    assert _try_recover_tool_call("Want me to run pc.deep_scan?", known) is None
    # A bare mention without an action verb is prose, not a call
    assert _try_recover_tool_call("pc.deep_scan found nothing last week.", known) is None


def test_narrated_intent_extracts_arg():
    """A narrated action fires its tool WITH the extracted arg — the session bug
    where a promised action never ran (recovery alone fires empty args, so the arg
    is what makes the call useful).
    """
    from kai.core.engine import _match_narrated_intent, _try_recover_tool_call

    known = {"search.web", "files.read"}
    rec = _try_recover_tool_call("I'm searching the web for the latest gemma release.", known)
    assert rec == {
        "function": {"name": "search.web", "arguments": {"query": "the latest gemma release"}}
    }
    # "google" phrasing, with trailing punctuation trimmed
    assert _match_narrated_intent("Let me google for ROCm 6.2 support.", known) == {
        "function": {"name": "search.web", "arguments": {"query": "ROCm 6.2 support"}}
    }
    # The pattern wants "look up" adjacent — "look that up" is deliberately not a match
    assert _match_narrated_intent("Let me look that up for ROCm 6.2 support.", known) is None
    # Intent is skipped when its target tool isn't registered (can't route it)
    assert _match_narrated_intent("Searching the web for foo", {"files.read"}) is None


# ── Follow-up tool selection ─────────────────────────────────────────────────────


def test_follow_up_selection_uses_intent_message_embedding(monkeypatch):
    """'yes please' must select tool categories with the embedding of the
    message that carried the intent — not the bare confirmation (which once
    produced a hallucinated shell tool)."""
    import kai.llm.embed as ke

    monkeypatch.setattr(ke, "embed", lambda t: [float(len(t))] * 8)
    brain, _ = make_brain()
    reg = MagicMock()
    brain.tool_registry = reg
    brain._tool_index = {"system": [0.0] * 8}
    intent_msg = "check my cpu temps please"
    # The assistant reply also contains tool keywords (CPU/GPU) — the user
    # message must still win the selection scan.
    history = [
        {"role": "user", "content": intent_msg},
        {"role": "assistant", "content": "CPU is at 46C, GPU load is 1%"},
    ]
    brain._select_tool_schema("yes please", history, [1.0] * 8, "chat")
    sel_emb = reg.select_tools_by_category.call_args[0][0]
    assert sel_emb == [float(len(intent_msg))] * 8


# ── Flow recorder ────────────────────────────────────────────────────────────────


def test_flow_recorder_roundtrip(monkeypatch):
    from kai.core import flow as flow_rec

    monkeypatch.setattr(cfg, "FLOW_TRACE", True)
    flow_rec.record("flowtest1", "route", input="hi there", think=False)
    flow_rec.record("flowtest1", "final_answer", text="yo")
    steps = flow_rec.get_flow("flowtest1")
    assert [s["kind"] for s in steps] == ["route", "final_answer"]
    assert steps[0]["input"] == "hi there"
    assert steps[1]["text"] == "yo"
    recent = flow_rec.recent_turns(limit=5)
    assert any(t["trace_id"] == "flowtest1" for t in recent)


def test_flow_live_tap_fires(monkeypatch):
    """Live viewers (:flowlive, /flow page) hear every step as it's recorded."""
    from kai.core import flow as flow_rec

    monkeypatch.setattr(cfg, "FLOW_TRACE", True)
    seen = []

    def tap(tid, kind, data):
        seen.append((tid, kind, data))

    flow_rec.subscribe(tap)
    try:
        flow_rec.record("taptest", "route", input="watch me")
    finally:
        flow_rec.unsubscribe(tap)
    assert seen == [("taptest", "route", {"input": "watch me"})]


def test_flow_reads_are_user_scoped(monkeypatch):
    """A user must only see their own turns: recent_turns/get_flow filter by
    user_id so /debug/flow can't leak another user's recorded steps."""
    from kai.core import flow as flow_rec

    monkeypatch.setattr(cfg, "FLOW_TRACE", True)

    monkeypatch.setattr(flow_rec, "get_current_user_id", lambda: 101)
    flow_rec.record("alice_turn", "route", input="alice secret")
    monkeypatch.setattr(flow_rec, "get_current_user_id", lambda: 202)
    flow_rec.record("bob_turn", "route", input="bob secret")

    alice_ids = {t["trace_id"] for t in flow_rec.recent_turns(limit=50, user_id=101)}
    assert "alice_turn" in alice_ids and "bob_turn" not in alice_ids
    # Cross-user read of a known trace id returns nothing.
    assert flow_rec.get_flow("bob_turn", user_id=101) == []
    assert flow_rec.get_flow("bob_turn", user_id=202)[0]["input"] == "bob secret"
    # No user_id (local CLI viewer) still sees everything.
    all_ids = {t["trace_id"] for t in flow_rec.recent_turns(limit=50)}
    assert {"alice_turn", "bob_turn"} <= all_ids


def test_flow_retention_trims_oldest(monkeypatch):
    """flow_log can't grow unbounded — once past FLOW_LOG_MAX, the oldest rows
    are trimmed on the next amortized check."""
    from kai.core import flow as flow_rec
    from kai.store.db import get_conn

    monkeypatch.setattr(cfg, "FLOW_TRACE", True)
    monkeypatch.setattr(cfg, "FLOW_LOG_MAX", 20)
    monkeypatch.setattr(flow_rec, "_TRIM_EVERY", 10)
    monkeypatch.setattr(flow_rec, "_writes_since_trim", 0)

    conn = get_conn()
    flow_rec._ensure_schema(conn)
    conn.execute("DELETE FROM flow_log")  # isolate the count from other tests
    conn.commit()

    for i in range(60):
        flow_rec.record(f"trace_{i}", "route", input=f"msg {i}")

    count = conn.execute("SELECT COUNT(*) FROM flow_log").fetchone()[0]
    assert count <= cfg.FLOW_LOG_MAX + flow_rec._TRIM_EVERY  # bounded, not 60


# ── No-reply fallback ────────────────────────────────────────────────────────────


def test_empty_model_output_still_streams_fallback():
    """When the model produces zero visible tokens, the fallback text must be
    YIELDED to the consumer — not just persisted — or the UI shows an empty
    bubble (the 06-09 no-reply bug)."""
    mock_ollama = MagicMock(spec=OllamaClient)
    mock_ollama.embed.return_value = [0.0] * 2560
    mock_ollama.chat_stream.side_effect = lambda *a, **k: iter([("", True, {})])

    memory = MemoryManager(embed_fn=lambda t: [0.0] * 2560)
    brain = Brain(memory=memory, ollama=mock_ollama)

    tokens = [tok for tok, done, _ in brain.run_stream("hello there") if not done and tok]
    assert tokens == ["[no response]"]


# ── Memory tree context (gather_context + Version C) ─────────────────────────────


def test_tree_facts_injected_into_context(tmp_path, monkeypatch):
    """With real facts in the tree, the [MEMORY CONTEXT] block appears in the
    turn context; with an empty (seed-only) tree it stays out entirely."""
    import numpy as np

    from kai.memory import state as mstate
    from kai.memory import tree as mtree

    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    monkeypatch.setattr(mstate, "_STATE_DIR", tmp_path)
    brain, _ = make_brain("Sure.")

    mtree.seed_skeleton("0")
    ctx = brain._build_turn_context("what do I do for work?", [1.0] * 384)
    assert "[MEMORY CONTEXT]" not in ctx  # seed-only tree → no block

    mtree.write(
        "0",
        mtree.Node(
            path="user/identity/profession",
            value="stuntman",
            source="stated",
            importance=0.7,
            specificity=0.7,
            embedding=np.ones(384, dtype=np.float32),
        ),
    )
    ctx = brain._build_turn_context("what do I do for work?", [1.0] * 384)
    assert "[MEMORY CONTEXT]" in ctx
    assert "stuntman" in ctx


# ── Durable learning queue + tree redirect ───────────────────────────────────


def _fake_embed(text):
    """Deterministic offline embedding — identical text → identical vector."""
    import hashlib

    import numpy as np

    h = int(hashlib.md5(text.strip().lower().encode()).hexdigest(), 16)
    return np.random.default_rng(h % (2**32)).standard_normal(16).tolist()


def test_pending_learn_queue_roundtrip():
    from kai.memory import pending

    n0 = pending.count(4242)
    pending.enqueue(
        4242, "user text that is plenty long", "assistant reply that is also long", "sess"
    )
    assert pending.count(4242) == n0 + 1
    batch = pending.peek(4242, limit=5)
    assert batch and batch[-1][1].startswith("user text")
    pending.delete(batch[-1][0])
    assert pending.count(4242) == n0


def test_extraction_writes_tree_not_episodic(tmp_path, monkeypatch):
    from kai.memory import extractor as ex
    from kai.memory import tree as mtree

    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    ex._TREE_SEEDED.clear()

    brain, _ = make_brain(
        "profession | The user is a welder at Boeing\npreference | The user prefers short answers"
    )
    monkeypatch.setattr(brain, "get_embed_fn", lambda: _fake_embed)

    brain._extract_knowledge("I weld at Boeing and I hate long answers", "Got it.")

    nodes = [
        n for n in mtree.all_nodes("0") if n.source != "seed" and not n.path.startswith("tools/")
    ]
    paths = sorted(n.path for n in nodes)
    assert any(p.startswith("user/identity/profession/") for p in paths), paths
    assert any(p.startswith("user/preferences/") for p in paths), paths
    assert nodes and all(n.source == "inferred" for n in nodes)


def test_extraction_dedup_reinforces(tmp_path, monkeypatch):
    from kai.memory import extractor as ex
    from kai.memory import tree as mtree

    monkeypatch.setattr(mtree, "_TREE_DIR", tmp_path)
    ex._TREE_SEEDED.clear()

    a1 = ex.save_fact_to_tree(0, "preference", "The user loves espresso", embed_fn=_fake_embed)
    a2 = ex.save_fact_to_tree(0, "preference", "The user loves espresso", embed_fn=_fake_embed)
    assert a1[0] == "created"
    assert a2[0] == "reinforced"
    assert mtree.read("0", a1[1]).frequency == 2


# ── OllamaClient ───────────────────────────────────────────────────────────────


def test_ollama_client_is_alive_false_when_down():
    client = OllamaClient(base_url="http://localhost:9999")  # nothing here
    assert client.is_alive() is False


# ── Background work never rides the cloud brain (PRIV-2) ───────────────────────


def _cloud_brain(response_text: str = "cloud reply"):
    """A Brain whose active chat brain is a stub cloud client, local mock intact."""
    brain, _ = make_brain(response_text)
    cloud = MagicMock()
    cloud.chat.return_value = {"message": {"content": "CATEGORY | a cloud-extracted fact"}}
    cloud.chat_stream.side_effect = make_mock_stream(response_text)
    brain._chat_client = cloud
    brain._chat_model = "cloud-model-x"
    return brain, cloud


def test_knowledge_extraction_stays_local_when_cloud_brain_is_active():
    """Memory formation must not egress. It runs on a bg thread with nothing on
    screen, so a cloud round-trip here is one the user never sees or consents to."""
    brain, cloud = _cloud_brain()
    brain.ollama.chat.return_value = {"message": {"content": "NONE"}}

    brain._extract_knowledge("I use PostgreSQL at work.", "Noted — PostgreSQL it is.")

    cloud.chat.assert_not_called()
    assert brain.ollama.chat.called
    assert brain.ollama.chat.call_args.kwargs["model"] == brain.model


def test_history_compression_stays_local_when_cloud_brain_is_active():
    """Same reason: compression ships raw history, and it is invisible work."""
    brain, cloud = _cloud_brain()
    brain.ollama.chat.return_value = {"message": {"content": "They discussed databases."}}

    summary = brain._summarize_messages(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    )

    assert summary == "They discussed databases."
    cloud.chat.assert_not_called()
    assert brain.ollama.chat.called


def test_visible_reply_still_uses_the_active_cloud_brain():
    """The other half of the contract — pinning background work must not quietly
    demote the brain the user actually chose for the answer they asked for."""
    brain, cloud = _cloud_brain()
    resp = brain._chat([{"role": "user", "content": "hi"}])

    cloud.chat.assert_called_once()
    assert resp["message"]["content"] == "CATEGORY | a cloud-extracted fact"


# ── Cleanup ─────────────────────────────────────────────────────────────────────


def teardown_module(module):
    try:
        os.unlink(_tmp.name)
    except Exception:
        pass
