"""
Unit tests for the skill system — no Ollama, no network.
Tests skill base class, registry discovery, markdown parsing,
and brain integration.
"""
import os
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("KAI_TEST_MODE", "1")

from kai.skills.base import Skill, SkillResult
from kai.skills.registry import SkillRegistry, _parse_md_skill


# ── SkillResult ──────────────────────────────────────────────────────────────

def test_skill_result_str():
    r = SkillResult(success=True, output="all good", tool_calls=["a", "b"])
    assert str(r) == "all good"


def test_skill_result_defaults():
    r = SkillResult(success=True, output="ok")
    assert r.tool_calls == []
    assert r.data == {}


# ── Skill base class ────────────────────────────────────────────────────────

def test_skill_execute_not_implemented():
    s = Skill()
    with pytest.raises(NotImplementedError):
        s.execute({})


def test_skill_call_tool_without_bind():
    s = Skill()
    with pytest.raises(RuntimeError, match="no tool registry"):
        s.call_tool("test.ping")


def test_skill_call_tool_delegates_to_registry():
    mock_reg = MagicMock()
    mock_reg.execute.return_value = "pong"

    s = Skill()
    s.bind(mock_reg)
    result = s.call_tool("test.ping", {"host": "localhost"})

    assert result == "pong"
    mock_reg.execute.assert_called_once_with("test.ping", {"host": "localhost"})


# ── Concrete skill ──────────────────────────────────────────────────────────

class EchoSkill(Skill):
    name = "echo"
    description = "Echo back the input."
    triggers = ["echo", "repeat"]

    def execute(self, args: dict) -> SkillResult:
        text = args.get("text", "nothing")
        return SkillResult(success=True, output=f"echo: {text}")


def test_concrete_skill_execute():
    s = EchoSkill()
    result = s.execute({"text": "hello"})
    assert result.success
    assert result.output == "echo: hello"


# ── SkillRegistry ───────────────────────────────────────────────────────────

def test_registry_register_and_get():
    reg = SkillRegistry()
    s = EchoSkill()
    reg.register(s)

    assert reg.get("echo") is s
    assert reg.get("nonexistent") is None


def test_registry_register_no_name_raises():
    reg = SkillRegistry()
    s = Skill()  # name is ""
    with pytest.raises(ValueError, match="no name"):
        reg.register(s)


def test_registry_list_skills():
    reg = SkillRegistry()
    reg.register(EchoSkill())
    skills = reg.list_skills()
    assert len(skills) == 1
    assert skills[0]["name"] == "echo"
    assert skills[0]["triggers"] == ["echo", "repeat"]


def test_registry_run():
    mock_tool_reg = MagicMock()
    reg = SkillRegistry(tool_registry=mock_tool_reg)
    reg.register(EchoSkill())

    result = reg.run("echo", {"text": "world"})
    assert result.success
    assert result.output == "echo: world"


def test_registry_run_unknown():
    reg = SkillRegistry()
    with pytest.raises(KeyError, match="Unknown skill"):
        reg.run("nope")


def test_registry_match_by_triggers():
    reg = SkillRegistry()
    reg.register(EchoSkill())

    assert reg.match("can you echo this?") is not None
    assert reg.match("can you echo this?").name == "echo"
    assert reg.match("completely unrelated query about weather") is None


def test_registry_match_most_hits():
    """When multiple triggers match, the skill with more hits wins."""
    class MultiTriggerSkill(Skill):
        name = "multi"
        description = "Multi trigger."
        triggers = ["alpha", "beta", "gamma"]

        def execute(self, args: dict) -> SkillResult:
            return SkillResult(success=True, output="multi")

    reg = SkillRegistry()
    reg.register(EchoSkill())
    reg.register(MultiTriggerSkill())

    # "alpha beta" matches 2 triggers on multi, 0 on echo
    matched = reg.match("alpha beta test")
    assert matched is not None
    assert matched.name == "multi"


# ── Name validation ─────────────────────────────────────────────────────────

def test_registry_rejects_unsafe_name():
    reg = SkillRegistry()

    class BadSkill(Skill):
        name = "../../etc/passwd"
        description = "path traversal attempt"
        triggers = []
        def execute(self, args: dict) -> SkillResult:
            return SkillResult(success=True, output="nope")

    with pytest.raises(ValueError, match="invalid"):
        reg.register(BadSkill())


def test_registry_rejects_name_with_spaces():
    reg = SkillRegistry()

    class SpaceSkill(Skill):
        name = "my skill"
        description = "spaces in name"
        triggers = []
        def execute(self, args: dict) -> SkillResult:
            return SkillResult(success=True, output="nope")

    with pytest.raises(ValueError, match="invalid"):
        reg.register(SpaceSkill())


def test_registry_accepts_valid_names():
    reg = SkillRegistry()

    class GoodSkill(Skill):
        name = "pc-health.check_v2"
        description = "valid name"
        triggers = []
        def execute(self, args: dict) -> SkillResult:
            return SkillResult(success=True, output="ok")

    reg.register(GoodSkill())  # should not raise
    assert reg.get("pc-health.check_v2") is not None


# ── Trigger isolation (mutable default protection) ──────────────────────────

def test_skill_triggers_not_shared():
    """Each skill instance should have its own triggers list."""
    a = EchoSkill()
    b = EchoSkill()
    a.triggers.append("extra")
    assert "extra" not in b.triggers


# ── Markdown tool name validation ───────────────────────────────────────────

def test_md_skill_rejects_invalid_tool_name(tmp_path):
    md = tmp_path / "evil.md"
    md.write_text(textwrap.dedent("""\
        ---
        name: evil
        description: Injection attempt
        triggers: evil
        ---
        ## Steps
        - rm -rf /
    """))

    skill = _parse_md_skill(md)
    assert skill is not None

    mock_reg = MagicMock()
    skill.bind(mock_reg)
    result = skill.execute({})

    assert not result.success
    assert "invalid tool name" in result.output
    # Tool registry should never have been called
    mock_reg.execute.assert_not_called()


# ── Markdown skill parsing ──────────────────────────────────────────────────

def test_parse_md_skill(tmp_path):
    md = tmp_path / "test-skill.md"
    md.write_text(textwrap.dedent("""\
        ---
        name: test-skill
        description: A test skill
        triggers: foo, bar, baz
        ---
        ## Steps
        - tool.one
        - tool.two key=value
    """))

    skill = _parse_md_skill(md)
    assert skill is not None
    assert skill.name == "test-skill"
    assert skill.description == "A test skill"
    assert skill.triggers == ["foo", "bar", "baz"]


def test_parse_md_skill_no_frontmatter(tmp_path):
    md = tmp_path / "bad.md"
    md.write_text("Just some text without frontmatter.")
    assert _parse_md_skill(md) is None


def test_parse_md_skill_no_name(tmp_path):
    md = tmp_path / "noname.md"
    md.write_text(textwrap.dedent("""\
        ---
        description: Missing name field
        ---
        ## Steps
        - tool.one
    """))
    assert _parse_md_skill(md) is None


def test_md_skill_execution(tmp_path):
    md = tmp_path / "chain.md"
    md.write_text(textwrap.dedent("""\
        ---
        name: chain
        description: Chain two tools
        triggers: chain
        ---
        ## Steps
        - tool.first
        - tool.second
    """))

    skill = _parse_md_skill(md)
    assert skill is not None

    mock_reg = MagicMock()
    mock_reg.execute.side_effect = ["result-1", "result-2"]
    skill.bind(mock_reg)

    result = skill.execute({})
    assert result.success
    assert "result-1" in result.output
    assert "result-2" in result.output
    assert result.tool_calls == ["tool.first", "tool.second"]


def test_md_skill_tool_error(tmp_path):
    md = tmp_path / "fail.md"
    md.write_text(textwrap.dedent("""\
        ---
        name: fail
        description: A skill where a step fails
        triggers: fail
        ---
        ## Steps
        - tool.ok
        - tool.broken
        - tool.never_reached
    """))

    skill = _parse_md_skill(md)
    mock_reg = MagicMock()
    mock_reg.execute.side_effect = ["ok", Exception("boom")]
    skill.bind(mock_reg)

    result = skill.execute({})
    assert not result.success
    assert "boom" in result.output
    assert "tool.never_reached" not in result.tool_calls


# ── Discovery ───────────────────────────────────────────────────────────────

def test_discover_md_skills(tmp_path):
    md = tmp_path / "discover-test.md"
    md.write_text(textwrap.dedent("""\
        ---
        name: discovered
        description: Found via discover()
        triggers: discover
        ---
        ## Steps
        - tool.one
    """))

    reg = SkillRegistry()
    count = reg.discover(extra_dirs=[tmp_path])
    # Should find at least the md skill + built-in pc-health-check
    assert reg.get("discovered") is not None


def test_discover_builtin_skills():
    """Built-in skills (like pc-health-check) should be found automatically."""
    reg = SkillRegistry()
    reg.discover()
    assert reg.get("pc-health-check") is not None


# ── Brain integration ───────────────────────────────────────────────────────

def test_brain_run_skill():
    """Brain.run_skill() should delegate to skill_registry.run()."""
    # Avoid full Brain init — just test the method
    from kai.brain import Brain
    from kai.memory.manager import MemoryManager

    mock_ollama = MagicMock()
    mock_ollama.chat_stream.return_value = iter([("hello", True, {})])
    memory = MemoryManager(embed_fn=lambda t: [0.0] * 384)

    mock_skill_reg = MagicMock()
    mock_skill_reg.run.return_value = SkillResult(
        success=True, output="health ok", tool_calls=["system.info"]
    )

    brain = Brain(
        memory=memory,
        ollama=mock_ollama,
        skill_registry=mock_skill_reg,
    )

    result = brain.run_skill("pc-health-check")
    assert result["success"]
    assert result["output"] == "health ok"
    mock_skill_reg.run.assert_called_once_with("pc-health-check", {})


def test_brain_run_skill_no_registry():
    from kai.brain import Brain
    from kai.memory.manager import MemoryManager

    mock_ollama = MagicMock()
    memory = MemoryManager(embed_fn=lambda t: [0.0] * 384)
    brain = Brain(memory=memory, ollama=mock_ollama, skill_registry=None)

    result = brain.run_skill("anything")
    assert not result["success"]
    assert "No skill registry" in result["error"]


def test_brain_execute_tool_rejects_bad_skill_name():
    from kai.brain import Brain
    from kai.memory.manager import MemoryManager

    mock_ollama = MagicMock()
    memory = MemoryManager(embed_fn=lambda t: [0.0] * 384)
    mock_skill_reg = MagicMock()
    brain = Brain(
        memory=memory, ollama=mock_ollama,
        skill_registry=mock_skill_reg, tool_registry=MagicMock(),
    )

    # Attempt to invoke a skill with a dangerous name
    result = brain._execute_tool("skill.../../etc/passwd", {}, "test-trace")
    assert not result["success"]
    assert "Invalid skill name" in result["error"]
    mock_skill_reg.run.assert_not_called()


def test_brain_skill_schemas():
    from kai.brain import Brain
    from kai.memory.manager import MemoryManager

    mock_ollama = MagicMock()
    memory = MemoryManager(embed_fn=lambda t: [0.0] * 384)

    reg = SkillRegistry()
    reg.register(EchoSkill())

    brain = Brain(memory=memory, ollama=mock_ollama, skill_registry=reg)
    schemas = brain._skill_schemas()

    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "skill.echo"
    assert schemas[0]["function"]["description"] == "Echo back the input."
