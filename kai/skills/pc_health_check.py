"""
Built-in skill: PC Health Check

Runs system info + temps + crash logs in sequence and returns
a combined health summary. Demonstrates the Skill pattern.
"""
from kai.skills.base import Skill, SkillResult


class PcHealthCheck(Skill):
    name = "pc-health-check"
    description = "Run a quick PC health check: system stats, temperatures, and recent crash logs."
    triggers = ["health check", "pc health", "is my pc okay", "system check", "quick scan"]

    def execute(self, args: dict) -> SkillResult:
        tools_called: list[str] = []
        sections: list[str] = []

        steps = [
            ("system.info", {}, "System Info"),
            ("system.temps", {}, "Temperatures"),
            ("system.crashes", {}, "Recent Crashes"),
        ]

        for tool_name, tool_args, label in steps:
            tools_called.append(tool_name)
            try:
                result = self.call_tool(tool_name, tool_args)
                sections.append(f"## {label}\n{result}")
            except Exception as exc:
                sections.append(f"## {label}\nError: {exc}")

        return SkillResult(
            success=True,
            output="\n\n".join(sections),
            tool_calls=tools_called,
        )
