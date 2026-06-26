"""Tests for the crew triage tree (kai/core/crew.py).

Pure logic — no DB, no LLM. Verifies the six execution profiles, the tools-first
ordering, ambiguity→BOSS, BACKGROUND-first, and that every tool category maps to a
specialist (drift guard)."""
import os
os.environ.setdefault("KAI_ENTRYPOINT", "test")

from kai.core.crew import (
    Profile, triage, is_long_running_query, tools_for_specialist,
    CREW_CATEGORIES, CATEGORY_TO_SPECIALIST, SPECIALISTS,
    load_specialist_prompt, parse_specialist_status, SpecialistResult,
    NEEDS_TO_SPECIALIST,
)


# ── No-tools branch ──────────────────────────────────────────────────────────────

def test_chat_when_no_tools_no_think():
    r = triage(tools_open=False, needs_think=False, category_scores=[])
    assert r.profile is Profile.CHAT
    assert r.specialist is None and r.tools is False and r.think is False
    assert r.lane == "chat"


def test_reason_when_no_tools_but_think():
    r = triage(tools_open=False, needs_think=True, category_scores=[])
    assert r.profile is Profile.REASON
    assert r.tools is False and r.think is True


def test_think_cap_forces_no_think_in_chat_branch():
    r = triage(tools_open=False, needs_think=True, category_scores=[], think_capped=True)
    assert r.profile is Profile.CHAT and r.think is False


# ── FAST: single confident specialist ────────────────────────────────────────────

def test_fast_single_confident_specialist():
    # two top categories, BOTH owned by Gus → still one specialist
    r = triage(
        tools_open=True, needs_think=False,
        category_scores=[("system_health", 0.62), ("system_control", 0.40)],
    )
    assert r.profile is Profile.FAST
    assert r.specialist == "Gus" and r.tools is True and r.think is False
    assert r.lane == "fast"


def test_fast_think_when_single_specialist_and_think():
    r = triage(
        tools_open=True, needs_think=True,
        category_scores=[("system_health", 0.55)],
    )
    assert r.profile is Profile.FAST_THINK
    assert r.specialist == "Gus" and r.think is True


def test_specialist_resolves_per_domain():
    cases = {
        "file_operations": "Dewey",
        "search_and_info": "Scout",
        "notes_and_memory": "Remy",
        "containers": "Cargo",
    }
    for cat, expected in cases.items():
        r = triage(tools_open=True, needs_think=False, category_scores=[(cat, 0.5)])
        assert r.specialist == expected, f"{cat} should route to {expected}"


# ── BOSS: spread or low confidence (ambiguity routes up) ──────────────────────────

def test_boss_when_two_specialists():
    r = triage(
        tools_open=True, needs_think=False,
        category_scores=[("file_operations", 0.50), ("notes_and_memory", 0.45)],
    )
    assert r.profile is Profile.BOSS
    assert r.specialist is None and r.tools is True
    assert r.think is True  # BOSS always reasons


def test_boss_when_single_but_low_confidence():
    # one specialist, but top score below FAST_CONFIDENCE → ambiguity routes up
    r = triage(
        tools_open=True, needs_think=False,
        category_scores=[("system_health", 0.20)],
    )
    assert r.profile is Profile.BOSS


def test_boss_respects_think_cap():
    r = triage(
        tools_open=True, needs_think=False, think_capped=True,
        category_scores=[("file_operations", 0.5), ("network", 0.5)],
    )
    assert r.profile is Profile.BOSS and r.think is False


def test_boss_when_tools_open_but_nothing_matched():
    r = triage(tools_open=True, needs_think=False, category_scores=[("network", 0.05)])
    assert r.profile is Profile.BOSS and r.specialist is None


# ── BACKGROUND: long-running beats lane ───────────────────────────────────────────

def test_background_when_long_running():
    r = triage(
        tools_open=True, needs_think=True, long_running=True,
        category_scores=[("system_health", 0.7), ("file_operations", 0.5)],
    )
    assert r.profile is Profile.BACKGROUND
    assert r.specialist == "Gus"      # owner of the top category
    assert r.tools is True and r.think is False
    assert r.lane == "background"


def test_long_running_needs_tools_open():
    # long-running hint but no tools → falls through to chat branch
    r = triage(tools_open=False, needs_think=False, long_running=True, category_scores=[])
    assert r.profile is Profile.CHAT


def test_is_long_running_query():
    assert is_long_running_query("run a deep scan of my system")
    assert is_long_running_query("do a full diagnostic")
    assert is_long_running_query("scan all nodes in the cluster")
    assert not is_long_running_query("what are my temps")


# ── Map integrity (drift guards) ──────────────────────────────────────────────────

def test_all_registry_categories_map_to_a_specialist():
    """Every tool category in the registry must be owned by exactly one specialist,
    or triage silently drops a whole domain."""
    from kai.tools.registry import _TOOL_CATEGORIES
    registry_cats = set(_TOOL_CATEGORIES.keys())
    mapped_cats = set(CATEGORY_TO_SPECIALIST.keys())
    assert registry_cats == mapped_cats, (
        f"unmapped: {registry_cats - mapped_cats}; stale: {mapped_cats - registry_cats}"
    )


def test_no_category_owned_by_two_specialists():
    seen: dict[str, str] = {}
    for specialist, cats in CREW_CATEGORIES.items():
        for cat in cats:
            assert cat not in seen, f"{cat} owned by both {seen[cat]} and {specialist}"
            seen[cat] = specialist


def test_tools_for_specialist_derives_from_categories():
    cat_tools = {"system_health": ["system.info", "system.temps"], "network": ["network.ping"]}
    tools = tools_for_specialist("Gus", cat_tools)
    assert "system.info" in tools and "network.ping" in tools
    assert "search.web" in tools  # Gus carries it for inline diagnostic lookups
    # Dewey owns none of those categories → just empty (no search.web for Dewey)
    assert tools_for_specialist("Dewey", cat_tools) == []


# ── Specialist prompts (loaded from docs/crew_prompts/) ───────────────────────────

def test_every_specialist_and_otto_prompt_loads():
    for name in (*SPECIALISTS, "Otto"):
        prompt = load_specialist_prompt(name)
        assert prompt and len(prompt) > 50, f"{name} prompt empty/too short"


def test_specialist_prompt_is_internal_and_scoped():
    gus = load_specialist_prompt("Gus")
    assert "never talk to the user" in gus.lower()
    assert "system.info" in gus            # its tool slice is rendered in
    otto = load_specialist_prompt("Otto")
    assert "dispatch" in otto.lower() and "finish" in otto.lower()


def test_missing_prompt_raises(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        load_specialist_prompt("Nobody", prompts_dir=tmp_path)


# ── Result contract + status parsing ──────────────────────────────────────────────

def test_parse_status_done():
    assert parse_specialist_status("CPU at 52C, no overheating.") == ("done", "")


def test_parse_status_needs_with_for_line():
    status, residual = parse_specialist_status(
        "needs: memory\nfor: save a note that the last crash was an OOM at 03:14"
    )
    assert status == "needs:memory"
    assert "save a note" in residual


def test_parse_status_blocked():
    status, residual = parse_specialist_status("blocked: no tool for sending email")
    assert status.startswith("blocked:") and "email" in status and residual == ""


def test_result_contract_needs_routes_to_specialist():
    r = SpecialistResult(status="needs:web", findings="read the log", tools=["files.read"])
    assert r.needs == "Scout"
    assert not r.blocked


def test_result_contract_blocked_flag():
    r = SpecialistResult(status="blocked: no tool", findings="")
    assert r.blocked and r.needs is None


def test_needs_tokens_map_to_all_six_targets():
    assert set(NEEDS_TO_SPECIALIST.values()) == {"Gus", "Dewey", "Scout", "Remy", "Cargo", "Envoy"}


# ── Specialist executor (Brain._run_specialist) — mocked model ────────────────────

def _drain(gen):
    """Run a generator to completion, returning its `return` value."""
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value


def _specialist_brain(chat_responses):
    from unittest.mock import MagicMock
    from kai.llm.ollama import OllamaClient
    from kai.memory.manager import MemoryManager
    from kai.core.brain import Brain
    from kai.tools import registry
    import kai.config as cfg
    mock = MagicMock(spec=OllamaClient)
    mock.embed.return_value = [0.0] * 2560
    mock.installed_models.return_value = [cfg.CHAT_MODEL, "granite4.1:8b"]
    mock.chat.side_effect = chat_responses
    memory = MemoryManager(embed_fn=lambda t: [0.0] * 2560)
    return Brain(memory=memory, tool_registry=registry, ollama=mock)


def test_run_specialist_returns_findings_done():
    # worker writes findings immediately (no tool call) — keep_prose keeps the prose
    brain = _specialist_brain([{"message": {"content": "CPU at 52C, nominal."}}])
    result = _drain(brain._run_specialist("Gus", "check my temps"))
    assert result.status == "done"
    assert "52C" in result.findings


def test_run_specialist_escalates_with_needs():
    brain = _specialist_brain([
        {"message": {"content": "needs: web\nfor: look up error code 0x80240032"}}
    ])
    result = _drain(brain._run_specialist("Gus", "diagnose the update error"))
    assert result.status == "needs:web"
    assert result.needs == "Scout"
    assert "0x80240032" in result.for_


# ── Otto decision parsing ─────────────────────────────────────────────────────────

def test_parse_otto_dispatch():
    from kai.core.crew import parse_otto_decision
    assert parse_otto_decision("DISPATCH Gus: check the CPU temperature") == \
        ("dispatch", "Gus", "check the CPU temperature")
    # lowercase name still resolves
    assert parse_otto_decision("dispatch scout: search the web")[1] == "Scout"


def test_parse_otto_finish():
    from kai.core.crew import parse_otto_decision
    kind, summary, _ = parse_otto_decision("FINISH: temps are nominal")
    assert kind == "finish" and "nominal" in summary


def test_parse_otto_dispatch_beats_stray_finish_word():
    from kai.core.crew import parse_otto_decision
    # a DISPATCH line wins even if a later line mentions finish
    txt = "DISPATCH Dewey: read the log then finish up\nFINISH: done"
    assert parse_otto_decision(txt)[:2] == ("dispatch", "Dewey")


def test_parse_otto_unknown_specialist_is_none():
    from kai.core.crew import parse_otto_decision
    assert parse_otto_decision("DISPATCH Bob: do a thing") is None


# ── Otto orchestration (Brain._run_crew) — mocked ─────────────────────────────────

def test_run_crew_dispatches_then_finishes():
    # Otto: dispatch Gus → (Gus answers) → Otto: FINISH
    brain = _specialist_brain([
        {"message": {"content": "DISPATCH Gus: check CPU temperature"}},   # Otto step 1
        {"message": {"content": "CPU is at 50C, nominal."}},                # Gus findings
        {"message": {"content": "FINISH: reported the temp"}},              # Otto step 2
    ])
    findings = _drain(brain._run_crew("is my cpu hot?"))
    assert "[Gus]" in findings and "50C" in findings


def test_run_crew_forces_needs_handback():
    # Otto dispatches Dewey; Dewey needs memory → Remy force-dispatched next
    brain = _specialist_brain([
        {"message": {"content": "DISPATCH Dewey: read crash.log"}},        # Otto step 1
        {"message": {"content": "needs: memory\nfor: save a note about the OOM"}},  # Dewey
        {"message": {"content": "Saved the note."}},                        # Remy (forced)
        {"message": {"content": "FINISH: done"}},                           # Otto step 3
    ])
    findings = _drain(brain._run_crew("read my crash log and note what's wrong"))
    assert "[Remy]" in findings and "Saved the note" in findings


# ── run_stream wiring (Brain._run_crew_turn, 3d) — triage stubbed ─────────────────

def _crew_turn(brain, decision, user_input="do the thing"):
    """Drive _run_crew_turn with a fixed triage decision; return (messages, tools_used)."""
    brain._triage_turn = lambda *a, **k: decision
    messages = [{"role": "user", "content": user_input}]
    tools_used: list[str] = []
    _drain(brain._run_crew_turn(
        user_input, messages, tools_used, query_emb=None,
        handoff_mode="tool", tools_open=True, trace_id="t", on_status=None,
    ))
    return messages, tools_used


def test_crew_turn_fast_injects_findings():
    from kai.core import crew
    brain = _specialist_brain([{"message": {"content": "CPU 50C, nominal."}}])
    decision = crew.TriageResult(profile=crew.Profile.FAST, specialist="Gus", think=False, tools=True)
    messages, tools_used = _crew_turn(brain, decision)
    # findings injected as a tool RESULT (so grounding + the voice model trust it)
    assert any(m["role"] == "tool" and "50C" in m.get("content", "") for m in messages)
    assert "Gus" in tools_used
    assert brain._last_triage_think is False


def test_crew_turn_chat_runs_nothing():
    from kai.core import crew
    brain = _specialist_brain([])  # no model calls expected
    decision = crew.TriageResult(profile=crew.Profile.REASON, specialist=None, think=True, tools=False)
    messages, tools_used = _crew_turn(brain, decision)
    assert not any(m["role"] == "tool" for m in messages)  # no evidence injected
    assert tools_used == []
    assert brain._last_triage_think is True  # REASON → think on, propagated to run_stream


def test_crew_turn_boss_orchestrates():
    from kai.core import crew
    brain = _specialist_brain([
        {"message": {"content": "DISPATCH Gus: check temps"}},
        {"message": {"content": "CPU 50C."}},
        {"message": {"content": "FINISH: done"}},
    ])
    decision = crew.TriageResult(profile=crew.Profile.BOSS, specialist=None, think=True, tools=True)
    messages, tools_used = _crew_turn(brain, decision)
    assert "crew" in tools_used
    assert any(m["role"] == "tool" and "[Gus]" in m.get("content", "") for m in messages)


# ── 3e: semantic axes (HandoffRouter.axis_match) wired into triage ────────────────

def test_axis_modes_mapping():
    from kai.memory.knowledge import HandoffRouter
    assert HandoffRouter._AXIS_MODES["tool"] == ("tool", "researcher")
    assert HandoffRouter._AXIS_MODES["think"] == ("reasoning",)


def test_semantic_tool_axis_opens_tools_on_keyword_miss():
    from unittest.mock import MagicMock
    # keyword gate missed (tools_open=False) but the learned tool axis matches →
    # triage should still route to a tool profile (BOSS here, no category scores).
    brain = _specialist_brain([{"message": {"content": "FINISH: nothing needed"}}])
    router = MagicMock()
    router.axis_match.side_effect = lambda emb, axis, **k: (axis == "tool", 1.0)
    brain._handoff_router = router
    brain._ensure_handoff_router = lambda: None
    brain._tool_index = {}
    messages = [{"role": "user", "content": "how's my rig holding up"}]
    tools_used: list[str] = []
    _drain(brain._run_crew_turn(
        "how's my rig holding up", messages, tools_used, query_emb=[0.0] * 384,
        handoff_mode="chat", tools_open=False, trace_id="t", on_status=None,
    ))
    assert "crew" in tools_used  # semantic tool axis opened the tools branch


def test_semantic_think_axis_sets_reason():
    from unittest.mock import MagicMock
    brain = _specialist_brain([])  # no tools → nothing dispatched
    brain._think = True            # thinking preset so think isn't capped
    router = MagicMock()
    router.axis_match.side_effect = lambda emb, axis, **k: (axis == "think", 1.0)
    brain._handoff_router = router
    brain._ensure_handoff_router = lambda: None
    brain._tool_index = {}
    messages = [{"role": "user", "content": "walk me through the tradeoffs here"}]
    tools_used: list[str] = []
    _drain(brain._run_crew_turn(
        "walk me through the tradeoffs here", messages, tools_used, query_emb=[0.0] * 384,
        handoff_mode="chat", tools_open=False, trace_id="t", on_status=None,
    ))
    assert tools_used == []                # no tools — REASON profile
    assert brain._last_triage_think is True  # think axis turned thinking on


# ── 3f: crew uses the role→model map ──────────────────────────────────────────────

def test_specialist_uses_default_crew_model():
    brain = _specialist_brain([{"message": {"content": "ok done"}}])
    _drain(brain._run_specialist("Gus", "check something"))
    # default crew model = ROLE_MODELS["crew"] (granite4.1:8b), and it's installed
    assert brain.ollama.chat.call_args.kwargs.get("model") == "granite4.1:8b"


def test_specialist_honors_per_agent_override(monkeypatch):
    from kai.llm import roles
    monkeypatch.setattr(
        roles, "crew_model_for",
        lambda name: "qwen3.5:9b" if name == "Gus" else roles.ROLE_MODELS["crew"],
    )
    brain = _specialist_brain([{"message": {"content": "ok"}}])
    brain.ollama.installed_models.return_value = ["qwen3.5:9b", "granite4.1:8b"]
    _drain(brain._run_specialist("Gus", "check something"))
    assert brain.ollama.chat.call_args.kwargs.get("model") == "qwen3.5:9b"


def test_specialist_falls_back_when_override_not_installed(monkeypatch):
    from kai.llm import roles
    monkeypatch.setattr(roles, "crew_model_for", lambda name: "not-installed:99b")
    brain = _specialist_brain([{"message": {"content": "ok"}}])
    brain.ollama.installed_models.return_value = ["granite4.1:8b"]
    _drain(brain._run_specialist("Gus", "check something"))
    # uninstalled override → falls back to the level-resolved model, not the bad id
    assert brain.ollama.chat.call_args.kwargs.get("model") != "not-installed:99b"
