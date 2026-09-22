"""
CLI colon-command dispatch table (tech-debt M9). The :help test is the point:
help text is generated from COMMANDS, so a new command cannot go undocumented.
Run with: python -m pytest tests/test_cli_commands.py -v
"""

import os

os.environ.setdefault("KAI_TEST_MODE", "1")

from types import SimpleNamespace

import kai.config as cfg
from kai.apps import cli


class FakeMemory:
    def __init__(self, facts=(), rules=()):
        self._facts = list(facts)
        self._rules = list(rules)
        self.user_id = 0
        self.deleted = []
        self.saved = []

    def list_facts(self, include_settings=True):
        return self._facts

    def list_rules(self):
        return self._rules

    def recent_episodes(self, limit=10):
        return []

    def delete_fact(self, key):
        self.deleted.append(key)

    def set_fact(self, key, value, source=""):
        self.saved.append((key, value, source))


class FakeBrain:
    def __init__(self):
        self.model = "fake:1b"
        self._think = False
        self.tool_registry = None
        self.temperature = None

    def set_temperature(self, t):
        self.temperature = t
        return t

    def apply_preset(self, key):
        return {"label": key.upper(), "think": True, "temp": 0.9}

    def apply_tool_level(self, key):
        return {"label": key.upper(), "model": None, "available": True}


def _run(cmd, brain=None, memory=None):
    return cli.handle_command(cmd, brain or FakeBrain(), memory or FakeMemory())


# ── Dispatch ───────────────────────────────────────────────────────────────────


def test_every_command_dispatches_to_its_handler(monkeypatch):
    calls = []
    spied = {
        name: (lambda b, m, a, _n=name: calls.append((_n, a)), syntax, desc)
        for name, (_fn, syntax, desc) in cli.COMMANDS.items()
    }
    monkeypatch.setattr(cli, "COMMANDS", spied)

    for name in spied:
        assert _run(f"{name} an-arg") is True, name

    assert [c[0] for c in calls] == list(spied)
    assert {c[1] for c in calls} == {"an-arg"}


def test_missing_arg_becomes_empty_string(monkeypatch):
    seen = []
    monkeypatch.setitem(cli.COMMANDS, ":memory", (lambda b, m, a: seen.append(a), "", "spy"))
    assert _run("  :memory  ") is True
    assert seen == [""]


def test_arg_is_the_rest_of_the_line_unstripped(monkeypatch):
    seen = []
    monkeypatch.setitem(cli.COMMANDS, ":forget", (lambda b, m, a: seen.append(a), "", "spy"))
    _run(":forget  some key  ")
    assert seen == ["some key"]


def test_lookup_is_case_insensitive(monkeypatch):
    seen = []
    monkeypatch.setitem(cli.COMMANDS, ":facts", (lambda b, m, a: seen.append(a), "", "spy"))
    assert _run(":FACTS") is True
    assert seen == [""]


def test_unknown_command_returns_false(capsys):
    assert _run(":nope") is False
    assert _run(":") is False
    assert _run("") is False
    assert capsys.readouterr().out == ""


# ── :help is generated, so it cannot drift ─────────────────────────────────────


def test_help_mentions_every_registered_command(capsys):
    assert _run(":help") is True
    out = capsys.readouterr().out

    for name, (_fn, syntax, desc) in cli.COMMANDS.items():
        label = f"{name} {syntax}" if syntax else name
        assert label in out, f"{name} missing from :help"
        assert desc in out, f"{name} description missing from :help"

    assert ":quit / exit" in out  # caught by the REPL loop, not the table
    assert out == cli.HELP_TEXT + "\n"


def test_help_has_one_line_per_command():
    body = [ln for ln in cli.HELP_TEXT.splitlines() if ln.startswith("  :")]
    assert len(body) == len(cli.COMMANDS) + 1  # + :quit / exit


# ── Handlers ───────────────────────────────────────────────────────────────────


def test_facts_empty_and_populated(capsys):
    _run(":facts", memory=FakeMemory())
    assert "No facts stored yet." in capsys.readouterr().out

    fact = SimpleNamespace(key="user_name", value="jt", source="chat")
    _run(":facts", memory=FakeMemory(facts=[fact]))
    assert "user_name = jt  [chat]" in capsys.readouterr().out


def test_forget_deletes_and_reports(capsys):
    mem = FakeMemory()
    _run(":forget  user_name ", memory=mem)
    assert mem.deleted == ["user_name"]
    assert "Deleted: user_name" in capsys.readouterr().out


def test_forget_without_arg_prints_usage(capsys):
    mem = FakeMemory()
    _run(":forget", memory=mem)
    assert mem.deleted == []
    assert "  Usage: :forget <key>" in capsys.readouterr().out


def test_temp_sets_and_reports(capsys):
    brain = FakeBrain()
    _run(":temp 1.25", brain=brain)
    assert brain.temperature == 1.25
    assert "Temperature: 1.25 (this session)" in capsys.readouterr().out


def test_temp_rejects_non_numeric(capsys):
    brain = FakeBrain()
    _run(":temp hot", brain=brain)
    assert brain.temperature is None
    assert f"  Usage: :temp <{cfg.TEMP_MIN}-{cfg.TEMP_MAX}>" in capsys.readouterr().out


def test_mode_applies_preset_and_persists(capsys):
    brain, mem = FakeBrain(), FakeMemory()
    preset = next(iter(cfg.GEN_PRESETS))
    _run(f":mode {preset.upper()}", brain=brain, memory=mem)
    assert mem.saved == [("gen_preset", preset, "user_setting")]
    assert "thinking ON" in capsys.readouterr().out


def test_mode_rejects_unknown_preset(capsys):
    mem = FakeMemory()
    _run(":mode sideways", memory=mem)
    assert mem.saved == []
    assert f"  Usage: :mode <{' | '.join(cfg.GEN_PRESETS)}>" in capsys.readouterr().out


def test_toollevel_applies_and_persists(capsys):
    mem = FakeMemory()
    level = next(iter(cfg.TOOL_MODEL_LEVELS))
    _run(f":toollevel {level}", memory=mem)
    assert mem.saved == [("tool_level", level, "user_setting")]
    assert f"Tool level: {level.upper()}" in capsys.readouterr().out


def test_toollevel_rejects_unknown_level(capsys):
    mem = FakeMemory()
    _run(":toollevel turbo", memory=mem)
    assert mem.saved == []
    assert f"  Usage: :toollevel <{' | '.join(cfg.TOOL_MODEL_LEVELS)}>" in capsys.readouterr().out


def test_model_switches_model_and_think_flag(capsys, monkeypatch):
    from kai.llm import models as _models

    monkeypatch.setattr(_models, "get_model", lambda n: {"ollama_id": "big:70b", "think": True})
    brain = FakeBrain()
    _run(":model big", brain=brain)
    assert brain.model == "big:70b"
    assert brain._think is True
    assert "Switched to: big:70b (thinking ON)" in capsys.readouterr().out


def test_model_unknown_leaves_brain_alone(capsys, monkeypatch):
    from kai.llm import models as _models

    monkeypatch.setattr(_models, "get_model", lambda n: None)
    brain = FakeBrain()
    _run(":model ghost", brain=brain)
    assert brain.model == "fake:1b"
    assert "Unknown model 'ghost'." in capsys.readouterr().out


def test_tools_without_registry(capsys):
    _run(":tools", brain=FakeBrain())
    assert "No tool registry loaded." in capsys.readouterr().out


def test_tools_lists_registry(capsys):
    brain = FakeBrain()
    brain.tool_registry = SimpleNamespace(list_tools=lambda: ["a.one", "b.two"])
    _run(":tools", brain=brain)
    out = capsys.readouterr().out
    assert "2 tools registered:" in out
    assert "a.one" in out


def test_debug_toggles_config(capsys, monkeypatch):
    monkeypatch.setattr(cfg, "DEBUG", False)
    _run(":debug")
    assert cfg.DEBUG is True
    assert "Debug mode: ON" in capsys.readouterr().out
    _run(":debug")
    assert cfg.DEBUG is False
    assert "Debug mode: OFF" in capsys.readouterr().out
