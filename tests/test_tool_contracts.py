"""Tool contracts: regressions from the 2026-09-10 hardening pass (packet E).

A call that can't act here fails without widening the tool set (D3a, T1-06). A call with
invented argument names gets the real ones back (T1-34). Narrated recovery never fires a tool
that needs arguments (T1-14), and a code alias isn't offered as a second tool (T1-07). Also
covered: pc.updates wording (T1-17), sandbox bare names (T1-19), session-tier temperatures
(T1-27), traceroute fallback and the approval label.
"""

import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import kai.config as cfg
import kai.tools  # noqa: F401 — registers every tool
from kai.core.engine import TurnEngine, _try_recover_tool_call
from kai.tools.registry import ToolRegistry, ToolUnavailable, registry


class _Engine(TurnEngine):
    session_id = None
    user_id = 0

    def __init__(self, tools):
        self._tools = tools

    tool_registry = property(lambda self: self._tools)
    skill_registry = property(lambda self: None)
    disabled_tools = property(lambda self: set())


def _one_tool(fn, parameters=None):
    reg = ToolRegistry()
    reg.tool(name="qa.tool", description="qa", parameters=parameters or {})(fn)
    return reg


def test_a_tool_that_cant_act_here_fails_without_widening_the_tools():
    """T1-06 / D3a: "only available on Windows" came back success=True."""

    def windows_only():
        raise ToolUnavailable("This tool is only available on Windows.")

    res = _Engine(_one_tool(windows_only))._execute_tool("qa.tool", {}, "t")
    assert res == {
        "success": False,
        "error": "This tool is only available on Windows.",
        "kind": "unavailable",
    }
    assert TurnEngine._classify_tool_result(res) == (False, False)


def test_wrong_argument_names_get_the_real_ones_without_widening():
    """T1-34: study.ask_library(query=...) cost a failed round plus the full tool schema."""

    def ask(question: str) -> str:
        return question

    reg = _one_tool(ask, {"question": {"type": "string", "required": True}})
    res = _Engine(reg)._execute_tool("qa.tool", {"query": "owls"}, "t")
    assert res["kind"] == "bad_args" and "question (string, required)" in res["error"]
    assert TurnEngine._classify_tool_result(res) == (False, False)


def test_a_typeerror_inside_the_tool_is_still_a_hard_failure():
    def broken() -> str:
        return len(5)

    res = _Engine(_one_tool(broken))._execute_tool("qa.tool", {}, "t")
    assert "kind" not in res
    assert TurnEngine._classify_tool_result(res) == (True, False)


def test_a_tool_returning_a_dict_does_not_crash_the_turn():
    """2026-09-21: design.render returned a dict and the classifier's regex raised on it,
    so the file was made but the turn died before Kai could reply."""
    assert TurnEngine._classify_tool_result({"success": True, "output": {"files": []}}) == (
        False,
        False,
    )
    coded = {"success": True, "output": {"code": "0x80070005"}}
    assert TurnEngine._classify_tool_result(coded) == (False, True)


def test_narrated_recovery_skips_tools_that_need_arguments():
    """T1-14: "I am appending ... now" fired files.append with {} and escalated."""
    known = {"files.append", "system.info"}
    needs = {n for n in known if registry.required_params(n)}
    assert "files.append" in needs and "system.info" not in needs
    assert _try_recover_tool_call("I'm running files.append now.", known, needs) is None
    assert _try_recover_tool_call("Running system.info now.", known, needs) is not None


def test_a_code_alias_resolves_but_isnt_offered_as_a_second_tool():
    """T1-07: pc.windows_updates showed up next to pc.updates in every offered set."""
    offered = {s["function"]["name"] for s in registry.get_schema()}
    assert "pc.updates" in offered and "pc.windows_updates" not in offered
    sliced = {s["function"]["name"] for s in registry.schema_for(["pc.updates"])}
    assert "pc.windows_updates" not in sliced
    assert registry.resolve_name("pc.windows_updates") == "pc.updates"


def test_pending_apt_updates_are_not_described_as_done(monkeypatch):
    """T1-17: "apt: 15 upgraded, ..." was read as "up to date" 3/3."""
    from kai.tools.system import pc_tools

    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/apt" if n == "apt" else None)

    def sim(line):
        return lambda *a, **k: SimpleNamespace(stdout=f"Inst foo\n{line}\n", returncode=0)

    monkeypatch.setattr(
        pc_tools.subprocess,
        "run",
        sim("15 upgraded, 0 newly installed, 0 to remove and 2 not upgraded."),
    )
    out = pc_tools._linux_updates()
    assert out.startswith("15 update(s) available, not installed yet") and "2 held back" in out
    monkeypatch.setattr(
        pc_tools.subprocess,
        "run",
        sim("0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded."),
    )
    assert pc_tools._linux_updates() == "System is up to date (apt): no updates pending."


def test_sandbox_bare_names_mean_files_in_kaifiles():
    """T1-19: "qa_delete_me.txt" resolved against the server's cwd and was reported missing."""
    from kai.core._app_state import set_current_user_id
    from kai.tools.compute import sandbox

    cfg.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    target = cfg.WORKSPACE_DIR / "qa_bare.txt"
    target.write_text("x")
    set_current_user_id(0)
    out = sandbox.propose_delete("qa_bare.txt")
    assert "PROPOSAL" in out and str(target.resolve()) in out
    with pytest.raises(ToolUnavailable, match="Not found"):
        sandbox.propose_delete("qa_missing.txt")


def test_the_approval_prompt_says_what_approving_does():
    from kai.core._app_state import set_current_user_id
    from kai.tools.compute import sandbox

    cfg.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    (cfg.WORKSPACE_DIR / "qa_label.txt").write_text("x")
    set_current_user_id(0)
    pid = re.search(r"PROPOSAL \[([0-9a-f]+)\]", sandbox.propose_delete("qa_label.txt")).group(1)
    assert sandbox.proposal_label(pid) == "Delete qa_label.txt"
    assert sandbox.proposal_label("nope") is None


@pytest.mark.parametrize(
    "text, key, value",
    [
        ("Your CPU temperature is 58°C.", "cpu_temp_c", "58"),
        ("The CPU is at 54°C", "cpu_temp_c", "54"),
        ("CPU: Temp: 54°C  Load: 10%", "cpu_temp_c", "54"),
        ("cpu temp: 48C", "cpu_temp_c", "48"),
        ("GPU Core: 72°C", "gpu_temp_c", "72"),
        ("Your GPU temperature is 65°C.", "gpu_temp_c", "65"),
    ],
)
def test_session_tier_captures_temperatures_as_kai_says_them(text, key, value):
    """T1-27: none of these was captured, so "what was my CPU temp a minute ago?" failed."""
    from kai.memory.extractor import extract_volatile_observations

    assert extract_volatile_observations(text).get(key) == value


def test_a_load_percentage_is_not_read_as_a_temperature():
    from kai.memory.extractor import extract_volatile_observations

    found = extract_volatile_observations("CPU: 23% busy, GPU idle")
    assert "cpu_temp_c" not in found and found.get("cpu_pct") == "23"


def test_traceroute_falls_back_to_tracepath():
    """T1-06: traceroute wasn't installed, though tracepath and mtr were."""
    from kai.tools._shell import ShellResult
    from kai.tools.web import network

    def fake(args, timeout):
        if args[0] == "traceroute":
            return ShellResult(
                "", "[Errno 2] No such file or directory: 'traceroute'", False, False
            )
        return ShellResult(" 1?: [LOCALHOST] pmtu 1500\n 1:  192.168.1.1  1.2ms", "", False, True)

    with (
        patch.object(network, "run_argv", side_effect=fake),
        patch.object(network, "_IS_WINDOWS", False),
    ):
        assert "192.168.1.1" in network.traceroute("1.1.1.1")
