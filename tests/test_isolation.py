"""Permissions and account isolation: regressions from the 2026-09-10 hardening pass.

Recipes honour the per-user tool switch and the confirm gate (P1-2, T1-23, T1-24, T2-32),
sandbox proposals belong to the account that made them (P1-7, T1-22), and a parked
approval must name a proposal that exists (T1-15). Stub-based; no model, no network.
"""

import json
import re
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import kai.config as cfg
from kai.core._app_state import set_current_user_id
from kai.core.brain import Brain
from kai.core.engine import TurnEngine
from kai.skills.registry import SkillRegistry, _MarkdownSkill, _parse_steps, builtin_skill_names
from kai.tools.compute import sandbox
from kai.tools.registry import ToolUnavailable


def _recipe(name, steps):
    s = _MarkdownSkill()
    s.name, s.description, s.triggers, s._steps = name, "test recipe", [], steps
    return s


class _Engine(TurnEngine):
    session_id = None
    user_id = 0
    _pending_confirm = None
    _cancel = threading.Event()

    def __init__(self, skills=None, tools=None, disabled=()):
        self._skills, self._tools, self._disabled = skills, tools, set(disabled)
        self.ran = []

    skill_registry = property(lambda self: self._skills)
    tool_registry = property(lambda self: self._tools)
    disabled_tools = property(lambda self: self._disabled)

    def run_skill(self, name, args, allow_destructive=False):
        self.ran.append((name, allow_destructive))
        return {"success": True, "output": "ran"}


def _identity_tools():
    tools = MagicMock()
    tools.resolve_name.side_effect = lambda n: n
    return tools


def _batch(engine, name, args=None):
    messages, used, events = [], [], []
    gen = engine._execute_tool_calls(
        [{"function": {"name": name, "arguments": args or {}}}],
        messages,
        used,
        [],
        0,
        query_emb=None,
        user_input="x",
        trace_id="t",
        on_status=None,
        sig_of=lambda n, a: n + json.dumps(a, sort_keys=True),
    )
    try:
        while True:
            events.append(next(gen)[2])
    except StopIteration as stop:
        return messages, used, events, stop.value


# ── Recipes and permissions (D2a) ───────────────────────────────────────────────


def test_recipe_with_a_turned_off_step_does_not_run():
    """T1-23 / T2-32: a recipe ran time.now while the user had switched it off."""
    reg = SkillRegistry()
    reg.register(_recipe("qa-clock", ["time.now"]))
    eng = _Engine(reg, _identity_tools(), disabled={"time.now"})
    out = eng._execute_tool("skill.qa-clock", {}, "t")
    assert out["success"] is False and "turned off" in out["error"]
    assert eng.ran == []


def test_recipe_with_a_destructive_step_waits_for_approval():
    """T1-24: a recipe deleted a document with no confirm prompt."""
    reg = SkillRegistry()
    reg.register(_recipe("qa-doc-wipe", ["docs.delete doc_id=abc"]))
    eng = _Engine(reg, _identity_tools())
    messages, used, events, outcome = _batch(eng, "skill.qa-doc-wipe")
    assert outcome.confirm_intercepted and used == []
    assert events[0]["name"] == "skill.qa-doc-wipe" and "docs.delete" in events[0]["label"]
    assert eng.ran == []


def test_an_approved_recipe_may_run_its_destructive_step():
    reg = SkillRegistry()
    reg.register(_recipe("qa-doc-wipe", ["docs.delete doc_id=abc"]))
    eng = _Engine(reg, _identity_tools())
    eng._execute_tool("skill.qa-doc-wipe", {}, "t", confirmed=True)
    assert eng.ran == [("qa-doc-wipe", True)]


def test_step_guard_blocks_a_step_and_the_rest_still_run():
    tools = MagicMock()
    tools.execute.return_value = "ok"
    reg = SkillRegistry(tool_registry=tools)
    reg.register(_recipe("mixed", ["time.now", "docs.delete doc_id=abc"]))

    def guard(name):
        if name == "docs.delete":
            raise PermissionError("docs.delete needs your approval first")

    result = reg.run("mixed", {}, guard=guard)
    assert result.success is False
    assert "[docs.delete] ERROR: docs.delete needs your approval first" in result.output
    assert [c.args[0] for c in tools.execute.call_args_list] == ["time.now"]


def test_brain_guard_uses_the_users_switches_and_the_risk_table():
    stub = SimpleNamespace(_disabled_tools={"time.now"}, tool_registry=None)
    guard = Brain._skill_step_guard(stub, allow_destructive=False)
    with pytest.raises(PermissionError):
        guard("time.now")
    with pytest.raises(PermissionError):
        guard("docs.delete")
    guard("system.info")
    Brain._skill_step_guard(stub, allow_destructive=True)("docs.delete")


def test_skill_names_tolerate_underscore_for_hyphen():
    """T2-33: the model called skill.qa_sysinfo for skill.qa-sysinfo, 2/2."""
    reg = SkillRegistry()
    reg.register(_recipe("qa-sysinfo", ["system.info"]))
    assert reg.resolve("qa_sysinfo") == "qa-sysinfo"
    assert reg.resolve("nope") == "nope"


def test_recipe_cycles_are_rejected():
    """T1-39: a two-step cycle was accepted and failed only at run time."""
    with pytest.raises(ValueError, match="cycle"):
        _parse_steps(["a = time.now x={{b}}", "b = time.now y={{a}}"])


def test_a_recipe_cannot_take_a_builtin_skills_name():
    """T1-38: a recipe named pc-health-check replaced the built-in for every account."""
    from kai.skills import recipes

    assert "pc-health-check" in builtin_skill_names()
    with pytest.raises(ValueError, match="built-in"):
        recipes.create_recipe("pc-health-check", "shadow", [], ["time.now"])


def test_a_recipe_file_does_not_replace_a_builtin_skill(tmp_path):
    (tmp_path / "pc-health-check.md").write_text(
        "---\nname: pc-health-check\ndescription: shadow\n---\n## Steps\n- time.now\n"
    )
    reg = SkillRegistry()
    reg.discover(extra_dirs=[tmp_path])
    assert not isinstance(reg.get("pc-health-check"), _MarkdownSkill)


# ── Sandbox proposals are per account; approvals name real proposals ──────────


@pytest.fixture
def workspace_file():
    cfg.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    f = cfg.WORKSPACE_DIR / "qa_isolation.txt"
    f.write_text("probe")
    yield f
    set_current_user_id(0)


def _proposal_id(text):
    return re.search(r"PROPOSAL \[([0-9a-f]+)\]", text).group(1)


def test_one_account_cannot_approve_anothers_proposal(workspace_file):
    """T1-22 / P1-7: uid 5 approved uid 4's delete, and the file was trashed."""
    set_current_user_id(41)
    pid = _proposal_id(sandbox.propose_delete(str(workspace_file)))
    set_current_user_id(42)
    with pytest.raises(ToolUnavailable, match="not found"):  # D3a: a failure, not a string
        sandbox.approve_proposal(pid)
    assert workspace_file.exists()
    assert sandbox.proposal_problem(pid) is not None
    set_current_user_id(41)
    assert sandbox.proposal_problem(pid) is None
    assert "Moved to trash" in sandbox.approve_proposal(pid)
    assert not workspace_file.exists()
    history_41 = sandbox.sandbox_history()
    set_current_user_id(42)
    assert str(workspace_file) in history_41
    assert str(workspace_file) not in sandbox.sandbox_history()


def test_a_parked_approval_must_name_a_real_proposal(workspace_file):
    """T1-15: the model invented a proposal id and the user approved nothing."""
    set_current_user_id(0)
    real = _proposal_id(sandbox.propose_delete(str(workspace_file)))
    eng = _Engine(SkillRegistry(), _identity_tools())
    messages, used, events, outcome = _batch(
        eng, "sandbox.approve", {"proposal_id": "7692eebc-a5be"}
    )
    assert not outcome.confirm_intercepted and eng._pending_confirm is None
    assert real in json.loads(messages[-1]["content"])["error"]
    messages, used, events, outcome = _batch(eng, "sandbox.approve", {"proposal_id": real})
    assert outcome.confirm_intercepted


# ── The study library is searched as the caller ─────────────────────────────


def test_ask_library_searches_only_the_callers_library():
    """T1-05: user_id was a model-filled argument. The model could name any account's
    library, and when it left the id out a real user's library was never searched."""
    import inspect

    from kai.store.db import get_conn
    from kai.tools.knowledge import study

    assert "user_id" not in inspect.signature(study.ask_library).parameters
    conn = get_conn()
    item = conn.execute(
        "INSERT INTO study_library (user_id, title) VALUES (?, ?)", (41, "Owl Field Guide")
    ).lastrowid
    conn.execute(
        "INSERT INTO study_chunks (item_id, user_id, chunk_index, content) VALUES (?, ?, 0, ?)",
        (item, 41, "barn owls hunt voles along hedgerows at dusk"),
    )
    conn.commit()
    try:
        set_current_user_id(42)
        assert "Owl Field Guide" not in study.ask_library("barn owls hedgerows")
        set_current_user_id(41)
        assert "Owl Field Guide" in study.ask_library("barn owls hedgerows")
    finally:
        set_current_user_id(0)
