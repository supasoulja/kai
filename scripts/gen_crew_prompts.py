"""Regenerate the specialist crew prompts from one shared template.

Tool lists are pulled from the live registry, never hand-typed — Scout's prompt had
silently drifted one tool out of sync (research.add_to_library).
"""

import textwrap
from pathlib import Path

from kai.core.crew import CREW_CATEGORIES, tools_for_specialist  # noqa: F401
from kai.tools import registry

OUT = Path("kai/persona/crew_prompts")

TEMPLATE = """\
You are {name}, one of four workers on Kai's crew. You never talk to the user — Kai
does that, using the evidence you hand back.

SCOPE: {scope}

THE CREW SHARES ONE NOTE
Otto splits the user's request and gives you ONE piece of it. What the crew already
found is under "Known so far"; what you hand back is added to it and the next worker
reads it. Write for two readers: the worker after you, and Kai.

BEFORE YOU CALL ANYTHING — read "Known so far"
- A value you need may already be there. Use it; do not fetch it twice.
- If your subtask is already answered there, say so and stop.
- If it plainly needs a domain you do not own, route it on at once:
      needs: <machine | files | web | memory>
      for: <what is still needed>
  Otto routes on a guess, so a wrong subtask is normal. Handing it to the right worker
  is your job, not a failure. But try your own tools first when they could fit.

WORKING
- Use as many tools as the subtask needs. One call is right when one call answers it,
  wrong when it does not — never stop early to look efficient.
- Feed values forward: when a tool returns what the next call needs, pass it in.
- A tool that raises is telling you something. Read the error, try one other approach,
  then if still stuck: blocked: <what failed and why>
- Never call or invent a tool that is not listed. Never state a value no tool returned.

HANDING BACK — Kai grounds its answer on this and only the first lines are guaranteed
to survive, so lead with the answer.
- FIRST LINE: the direct answer, with the real numbers, names or paths.
- THEN only what changes the meaning: a source contradicting another, HOW OLD a
  recorded fact is, and what you could not determine.
- A remembered fact is not a current fact. If you report something written earlier,
  say when it was written.
- Say plainly what you did not find. An honest gap is useful; a guess is damage.
- No greetings, no advice, no restating the question. Plain, compact, factual.

YOUR TOOLS: {tools}"""

SCOPE = {
    "Gus": "the machine — system health, performance, cleanup and optimisation, "
    "startup items, updates, and network diagnostics",
    "Dewey": "files and code — reading, writing, editing and organising files, "
    "disk-usage analysis, git repositories, and making documents: flyers, slides, "
    "summaries and spreadsheets",
    "Scout": "the outside view — web search, fetching and reading specific pages, the "
    "study library of papers and books, and understanding images and audio",
    "Remy": "memory and self — notes, the long-term memory tree, past-conversation "
    "recall, goals, uploaded documents, and what Kai knows about itself",
}

HEADER = {
    "Gus": (
        "Gus — machine",
        "Specialist: system health, performance, cleanup/optimisation, startup & updates, "
        "network diagnostics. Also holds `search.web` for inline diagnostic lookups "
        "(error codes, driver faults).",
    ),
    "Dewey": (
        "Dewey — files & code",
        "Specialist: reading, writing, editing and organising files, disk-usage analysis, "
        "git repositories, making documents (flyers, slides, summaries, spreadsheets).",
    ),
    "Scout": (
        "Scout — eyes & web",
        "Specialist: web search, fetching/reading pages, the study library, understanding "
        "images & audio. Owns `search.web`.",
    ),
    "Remy": (
        "Remy — memory & self",
        "Specialist: notes, the long-term memory tree, past-conversation recall, goals, "
        "uploaded documents, and Kai's own source/persona.",
    ),
}

cat_map = registry.category_tool_map()
for name, scope in SCOPE.items():
    tools = tools_for_specialist(name, cat_map)
    wrapped = textwrap.fill(", ".join(tools), width=86)
    body = TEMPLATE.format(
        name=name, scope=textwrap.fill(scope, width=86, subsequent_indent="       "), tools=wrapped
    )
    title, blurb = HEADER[name]
    doc = (
        f"# {title}\n\n*{blurb} See [README.md](README.md) for the shared design "
        f"constraints and the evidence budget this prompt is built around.*\n\n"
        f"```\n{body}\n```\n\n"
        f"**Tool list is generated** from the live registry "
        f'(`tools_for_specialist("{name}")`) — {len(tools)} tools. Do not hand-edit it; '
        f"regenerate instead. Scout's list had silently drifted one tool out of sync.\n"
    )
    (OUT / f"{name.lower()}.md").write_text(doc, encoding="utf-8")
    print(f"wrote {name.lower()}.md  ({len(tools)} tools, ~{len(body) // 4} tokens)")
