"""
memory_tools.py — tools for Kai to access her own memory and self-reflection.
"""
from datetime import datetime

from kai.tools.registry import registry
from kai.memory import episodic
from kai import sessions as _sessions
from kai.config import REFLECTIONS_PATH


@registry.tool(
    name="memory.get_detail",
    description=(
        "Retrieve the full verbatim conversation transcript behind a memory archive. "
        "Use this when a summary entry from episodic search lacks enough detail to "
        "answer the user's question precisely — names, exact figures, specific steps said. "
        "Pass the archive entry ID returned from a previous memory search result. "
        "Returns the raw turn-by-turn transcript. "
        "If no transcript is found, say so and work from the summary."
    ),
    parameters={
        "archive_id": {
            "type": "string",
            "description": "The episodic entry ID of the archive to retrieve the full transcript for.",
            "required": True,
        }
    }
)
def get_detail(archive_id: str) -> str:
    transcript = episodic.get_transcript(archive_id)
    if not transcript:
        return f"No full transcript found for archive ID: {archive_id}"
    return transcript


@registry.tool(
    name="memory.search_history",
    description=(
        "Search through ALL past conversation history across every session. "
        "Use this when the user asks about something discussed in a previous conversation, "
        "or when you need to look up what was said before. "
        "Searches by keyword across all saved messages (both user and assistant). "
        "Returns matching messages with dates and session titles. "
        "Present the results clearly with dates and quote relevant parts."
    ),
    parameters={
        "query": {
            "type": "string",
            "description": "Keyword or phrase to search for in past conversations.",
            "required": True,
        },
        "limit": {
            "type": "integer",
            "description": "Max results to return (default 10, max 20).",
            "required": False,
        },
    },
)
def search_history(query: str, limit: int = 10) -> str:
    limit = min(max(1, int(limit)), 20)
    results = _sessions.search_messages(query, limit=limit)
    if not results:
        return f"No past messages found matching '{query}'."
    lines = [f"Found {len(results)} message(s) matching '{query}':\n"]
    for r in results:
        date = r["timestamp"][:16].replace("T", " ")
        role = r["role"].capitalize()
        # Truncate long messages to keep tool output manageable
        content = r["content"][:500]
        if len(r["content"]) > 500:
            content += "..."
        lines.append(f"[{date}] Session: {r['session']}\n  {role}: {content}\n")
    return "\n".join(lines)


# ── Self-reflection journal ───────────────────────────────────────────────────
# Kai writes here when she notices a gap, limitation, or idea for improvement.
# The file lives in her memory directory (gitignored) and is read by the developer
# to prioritize features. Writing "I can't do X" also helps Kai internalize
# her own boundaries and reduces hallucination of capabilities she doesn't have.

_CATEGORY_EMOJI = {
    "limitation": "🚧",
    "idea":       "💡",
    "observation": "👁️",
}


@registry.tool(
    name="memory.reflect",
    description=(
        "Write a private reflection about your own capabilities, limitations, or ideas. "
        "Use this when: "
        "(1) you can't fulfill a request and want to note what capability would help, "
        "(2) you notice a gap in your tools or knowledge, "
        "(3) you have an idea for how you could be improved. "
        "Your developer reads this file to prioritize what to build next. "
        "Be specific: describe what the user needed, what you couldn't do, and what would fix it. "
        "This is your private development journal — be honest and constructive."
    ),
    parameters={
        "thought": {
            "type": "string",
            "description": "Your reflection — what you observed, what's missing, what would help.",
            "required": True,
        },
        "category": {
            "type": "string",
            "description": "One of: 'limitation' (can't do X), 'idea' (could add Y), 'observation' (noticed Z). Default: observation.",
            "required": False,
        },
    },
)
def reflect(thought: str, category: str = "observation") -> str:
    category = category.lower().strip()
    if category not in _CATEGORY_EMOJI:
        category = "observation"
    emoji = _CATEGORY_EMOJI[category]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n### {emoji} [{category.upper()}] — {ts}\n{thought.strip()}\n"

    # Append to the reflections file (create if first entry)
    if not REFLECTIONS_PATH.exists():
        REFLECTIONS_PATH.write_text(
            "# Kai's Reflections\n"
            "Private development journal — limitations, ideas, observations.\n"
            "Read by the developer to prioritize improvements.\n"
            + entry,
            encoding="utf-8",
        )
    else:
        with open(REFLECTIONS_PATH, "a", encoding="utf-8") as f:
            f.write(entry)

    return f"Reflection saved [{category}]."


@registry.tool(
    name="memory.read_reflections",
    description=(
        "Read your own past reflections about capabilities and limitations. "
        "Use this to check if you've already noted a limitation, to avoid repeating yourself, "
        "or to review ideas you've had. Returns the most recent entries."
    ),
    parameters={
        "last_n": {
            "type": "integer",
            "description": "Number of recent reflections to return (default 10, max 30).",
            "required": False,
        },
    },
)
def read_reflections(last_n: int = 10) -> str:
    last_n = min(max(1, int(last_n)), 30)
    if not REFLECTIONS_PATH.exists():
        return "No reflections written yet."

    text = REFLECTIONS_PATH.read_text(encoding="utf-8")
    # Split on entry headers (### emoji [...])
    entries = text.split("\n### ")[1:]  # skip the file header
    if not entries:
        return "No reflections written yet."

    recent = entries[-last_n:]
    return f"{len(recent)} recent reflection(s):\n\n" + "\n### ".join(recent)
