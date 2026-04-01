"""
Skill base class — defines what a skill looks like and how it runs.

A skill is a reusable multi-step workflow that chains tools together.
Each skill declares its name, description, trigger keywords, and an
execute() method that orchestrates tool calls via the tool registry.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SkillResult:
    """Outcome of a skill execution."""
    success: bool
    output: str                         # human-readable result
    tool_calls: list[str] = field(default_factory=list)  # tools that were invoked
    data: dict[str, Any] = field(default_factory=dict)   # structured payload (optional)

    def __str__(self) -> str:
        return self.output


class Skill:
    """
    Base class for all skills.

    Subclasses must set the class-level attributes and implement execute().
    The tool_registry is injected at execution time by the SkillRegistry
    so skills never import it directly.

    Example
    -------
    class PingSkill(Skill):
        name = "ping"
        description = "Ping a host and report latency."
        triggers = ["ping", "latency", "connection test"]

        def execute(self, args: dict) -> SkillResult:
            result = self.call_tool("network.ping", {"host": args.get("host", "8.8.8.8")})
            return SkillResult(success=True, output=result)
    """

    name: str = ""
    description: str = ""
    triggers: list[str] = []           # keywords that hint this skill is relevant

    def __init__(self) -> None:
        self._tool_registry: Any | None = None
        # Ensure each instance gets its own triggers list (avoid shared mutable default)
        if type(self).triggers is Skill.triggers:
            self.triggers = []
        else:
            self.triggers = list(type(self).triggers)

    def bind(self, tool_registry: Any) -> None:
        """Inject the tool registry. Called by SkillRegistry before execute()."""
        self._tool_registry = tool_registry

    def call_tool(self, tool_name: str, args: dict | None = None) -> Any:
        """
        Convenience wrapper — runs a tool through the registry and returns
        the raw output. Raises RuntimeError if the tool fails.
        """
        if not self._tool_registry:
            raise RuntimeError("Skill has no tool registry — was bind() called?")
        result = self._tool_registry.execute(tool_name, args or {})
        return result

    def execute(self, args: dict) -> SkillResult:
        """
        Run the skill. Subclasses must override this.

        Parameters
        ----------
        args : dict
            Free-form arguments extracted from the user's request.

        Returns
        -------
        SkillResult
        """
        raise NotImplementedError(f"{type(self).__name__} must implement execute()")
