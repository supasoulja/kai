"""
Crew triage — the routing tree that decides how a turn is handled.

This is Part C of docs/AGENT_CREW_AND_SETTINGS_PLAN.md: a tools-first, MODEL-FREE
decision tree that replaces the flat HandoffRouter mode + keyword tool-gate as the
turn's decision surface, and doubles as the crew's triage front-end.

Design rule: every decision is a cheap classifier (the caller supplies the signals
— embedding cosine, keyword, heuristic), never a model call. This module is the
PURE decision logic + the crew→category map. It does not run specialists, touch the
DB, or call the LLM — so it is fully unit-testable in isolation. Wiring it into
Brain.run_stream (replacing _run_tool_rounds) and splitting handoff_patterns into
tool-/think-pattern tables happens at cutover, separately.

The six execution profiles the tree resolves to:
    CHAT        — no tools, no think          (greeting, recall, opinion)
    REASON      — no tools, think             (analysis, advice)
    FAST        — one specialist, no think    (quick single-domain task)
    FAST_THINK  — one specialist, think       (hard single-domain task)
    BOSS        — Otto orchestrates many      (multi-domain / vague / low-confidence)
    BACKGROUND  — one long-running tool        (fire-and-forget)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from kai.config import ROOT_DIR

# ── The crew → category map (from plan Part A.2) ─────────────────────────────────
# Each of the 17 tool categories in kai/tools/registry.py is owned by exactly one
# specialist. Otto (boss) and Envoy (MCP, none yet) own no categories here.
CREW_CATEGORIES: dict[str, list[str]] = {
    "Gus":   ["system_health", "system_control", "startup_and_updates", "network"],
    "Dewey": ["file_operations", "disk_analysis", "workspace_and_code"],
    "Scout": ["search_and_info", "web_content", "study_library", "media_understanding"],
    "Remy":  ["notes_and_memory", "goals_and_tasks", "self_inspection", "docs_rag"],
    "Cargo": ["containers", "remote_cluster"],
}

# Reverse lookup: category → specialist. Built once at import.
CATEGORY_TO_SPECIALIST: dict[str, str] = {
    cat: specialist
    for specialist, cats in CREW_CATEGORIES.items()
    for cat in cats
}

# Tools that take long enough to warrant the BACKGROUND lane (fire-and-forget).
LONG_RUNNING_TOOLS: frozenset[str] = frozenset({
    "pc.deep_scan", "network.full_diagnostic", "cluster.broadcast_scan",
})

# All worker specialists (Otto is the boss, not a worker). Envoy is MCP-only and
# dormant until a server is connected (its slice is empty), but its prompt exists.
SPECIALISTS: tuple[str, ...] = ("Gus", "Dewey", "Scout", "Remy", "Cargo", "Envoy")

# The finalized lean prompts (one file per agent). docs/crew_prompts is canonical —
# see docs/AGENT_CREW_AND_SETTINGS_PLAN.md Part A.2.
CREW_PROMPTS_DIR: Path = ROOT_DIR / "docs" / "crew_prompts"

# needs:<domain> → specialist, the six fixed escalation tokens (plan Part A.2).
NEEDS_TO_SPECIALIST: dict[str, str] = {
    "machine": "Gus", "files": "Dewey", "web": "Scout",
    "memory": "Remy", "infra": "Cargo", "external": "Envoy",
}
_NEEDS_RE = re.compile(r"^\s*needs:\s*(machine|files|web|memory|infra|external)\b", re.I)
_BLOCKED_RE = re.compile(r"^\s*blocked:\s*(.+)", re.I | re.S)

# Otto's dispatch loop bound — caps latency + GPU swaps (plan Part A.2).
MAX_DISPATCHES = 4
_DISPATCH_RE = re.compile(r"DISPATCH\s+(\w+)\s*:\s*(.+)", re.I)
_FINISH_RE = re.compile(r"\bFINISH\b\s*:?\s*(.*)", re.I)


# ── Tuning knobs (cheap heuristics — no model) ───────────────────────────────────
# A category must clear this cosine score to count as "matched". Calibrated to the
# LIVE triage embedder — kai.llm.embed / bge-small-en-v1.5 (the CPU model that
# embeds the query and the category index at runtime), NOT the shutdown-only
# qwen3-embedding. On bge-small, small talk tops out ~0.51 and a query's own domain
# scores ~0.61–0.70, so a low floor let almost every query match 2–3 domains and
# fall through to BOSS (Otto orchestration) even for a plain single-domain request.
# At 0.60 a clean single-domain query resolves to ONE specialist (the FAST lane)
# while a genuine multi-domain request keeps ≥2. The margin is thin, though —
# runner-up categories sit right around 0.60, so some single-domain phrasings still
# pull a 2nd specialist and over-route to BOSS. Retune if the embedding model
# changes; tests/test_crew_calibration.py is the drift tripwire that fails loudly
# (with the new distribution printed) when this floor stops separating the lanes.
CATEGORY_FLOOR = 0.60
# Secondary guard: even a single matched specialist needs the top category this
# confident to take FAST. With CATEGORY_FLOOR at 0.60 a match already clears this,
# so it's a subordinate floor kept as an explicit minimum.
FAST_CONFIDENCE = 0.30


class Profile(StrEnum):
    CHAT = "chat"
    REASON = "reason"
    FAST = "fast"
    FAST_THINK = "fast_think"
    BOSS = "boss"
    BACKGROUND = "background"


@dataclass(frozen=True)
class TriageResult:
    """The decision for one turn."""
    profile: Profile
    specialist: str | None  # the FAST/FAST_THINK/BACKGROUND worker; None for CHAT/REASON/BOSS
    think: bool             # whether chain-of-thought is on
    tools: bool             # whether any tools run this turn
    # Distinct specialists whose domains cleared the floor, in score order. For a
    # BOSS turn this is the coverage set: every one of these has real work to do,
    # so run_crew force-dispatches any Otto skips before it's allowed to FINISH.
    matched: tuple[str, ...] = ()

    @property
    def lane(self) -> str:
        """Coarse lane label for logging/flow trace."""
        if self.profile in (Profile.CHAT, Profile.REASON):
            return "chat"
        if self.profile in (Profile.FAST, Profile.FAST_THINK):
            return "fast"
        if self.profile is Profile.BOSS:
            return "boss"
        return "background"


def _specialists_for(category_scores: list[tuple[str, float]]) -> list[str]:
    """Distinct specialists owning the matched (above-floor) top categories,
    in score order. Categories with no owner (e.g. a future MCP-only one) are
    skipped."""
    seen: list[str] = []
    for cat, score in category_scores:
        if score < CATEGORY_FLOOR:
            continue
        spec = CATEGORY_TO_SPECIALIST.get(cat)
        if spec and spec not in seen:
            seen.append(spec)
    return seen


def triage(
    *,
    tools_open: bool,
    needs_think: bool,
    category_scores: list[tuple[str, float]],
    long_running: bool = False,
    think_capped: bool = False,
    keyword_gated: bool = True,
) -> TriageResult:
    """Resolve a turn to one of the six execution profiles.

    All inputs are cheap signals the caller already has:
      tools_open      — tools were opened for this turn (keyword gate ∪ the fuzzy
                        semantic tool axis)
      needs_think     — the think classifier (heuristic ∪ learned think-patterns)
      category_scores — registry.select_tools_by_category ranking, (category, cosine)
                        sorted DESC; pass the top few (e.g. top-3)
      long_running    — a long-running op was detected (see is_long_running_query)
      think_capped    — the active preset forbids thinking (a no-think preset always
                        wins; see config.GEN_PRESETS). When True, think is forced off.
      keyword_gated   — tools were opened by a TRUSTED gate (keyword ∪ handoff-
                        semantic), not just the fuzzy tool axis. Distinguishes a
                        genuine-but-vague tool request from a chat turn the fuzzy
                        axis mis-read (greetings/small-talk score ~0.5 on it).

    Ordering is tools-first; ambiguity routes UP to BOSS (its low-confidence default).
    """
    think = needs_think and not think_capped

    # Q0 · BACKGROUND? — a long-running tool beats lane selection.
    if tools_open and long_running:
        specialists = _specialists_for(category_scores)
        return TriageResult(
            profile=Profile.BACKGROUND,
            specialist=specialists[0] if specialists else None,
            think=False,
            tools=True,
        )

    # Q1 · TOOLS? — no tools → the chat branch.
    if not tools_open:
        return TriageResult(
            profile=Profile.REASON if think else Profile.CHAT,
            specialist=None,
            think=think,
            tools=False,
        )

    # Tools branch. Q3 · DOMAIN SPREAD — single confident specialist → FAST, else BOSS.
    specialists = _specialists_for(category_scores)

    # No domain cleared CATEGORY_FLOOR. Three sub-cases:
    #   • keyword-gated → a genuine but vague tool request → let Otto (BOSS) work it.
    #   • no category scores at all (tool index unavailable) → we can't judge the
    #     domain; be safe and let Otto handle it rather than silently drop tools.
    #   • scores existed but none cleared the floor → the fuzzy tool axis mis-read a
    #     chat turn (greetings/small-talk score ~0.5 on every domain) → chat, so we
    #     don't pay full Otto orchestration for "hey there".
    if not specialists:
        if keyword_gated or not category_scores:
            return TriageResult(
                profile=Profile.BOSS, specialist=None,
                think=not think_capped, tools=True,
            )
        return TriageResult(
            profile=Profile.REASON if think else Profile.CHAT,
            specialist=None, think=think, tools=False,
        )

    top_score = category_scores[0][1] if category_scores else 0.0
    single_confident = (
        len(specialists) == 1 and top_score >= FAST_CONFIDENCE
    )

    if single_confident:
        return TriageResult(
            profile=Profile.FAST_THINK if think else Profile.FAST,
            specialist=specialists[0],
            think=think,
            tools=True,
        )

    # ≥2 specialists → Otto orchestrates. Pass the top-2 distinct domains as the
    # coverage set so run_crew won't let Otto FINISH before every one is dispatched
    # (granite likes to stop after the first, dropping the rest of a compound ask).
    # BOSS always reasons (the leaf table fixes think=on for orchestration).
    return TriageResult(
        profile=Profile.BOSS,
        specialist=None,
        think=not think_capped,
        tools=True,
        matched=tuple(specialists[:2]),
    )


# ── Long-running detection (cheap keyword pass) ──────────────────────────────────
# Approximate at triage time: the exact tool is the specialist's pick, but these
# phrasings reliably mean a long job. Refine against real usage later.
_LONG_RUNNING_HINTS = (
    "deep scan", "deep-scan", "full diagnostic", "full scan", "scan everything",
    "scan all", "broadcast", "scan every node", "scan the cluster", "scan all nodes",
)


def is_long_running_query(text: str) -> bool:
    t = text.lower()
    return any(h in t for h in _LONG_RUNNING_HINTS)


def tools_for_specialist(name: str, category_tools: dict[str, list[str]]) -> list[str]:
    """The full tool slice a specialist owns, derived from its categories.

    `category_tools` maps category → tool names (from registry._TOOL_CATEGORIES);
    passed in so this module stays decoupled from the registry. Used by the
    execution layer (not triage) to narrow a specialist's tool schema.

    Gus also carries search.web for inline diagnostic lookups (plan Part A.2);
    Scout owns it via its categories already.
    """
    tools: list[str] = []
    for cat in CREW_CATEGORIES.get(name, []):
        for tool in category_tools.get(cat, []):
            if tool not in tools:
                tools.append(tool)
    if name == "Gus" and "search.web" not in tools:
        tools.append("search.web")
    return tools


# ── Specialist prompts ───────────────────────────────────────────────────────────

_PROMPT_CACHE: dict[str, str] = {}
# Extracts the first fenced code block — the actual prompt — from a crew_prompts/*.md.
_FENCE_RE = re.compile(r"```(?:\w+)?\n(.*?)```", re.S)


def load_specialist_prompt(name: str, *, prompts_dir: Path | None = None) -> str:
    """Return a specialist's (or Otto's) system prompt from docs/crew_prompts/<name>.md.

    The .md wraps the prompt in a fenced block; this returns the fenced content
    only (no title/notes). Cached after first read. Raises FileNotFoundError /
    ValueError loudly — a missing or malformed crew prompt is a build error, not
    something to paper over with a fallback.
    """
    key = name.lower()
    if prompts_dir is None and key in _PROMPT_CACHE:
        return _PROMPT_CACHE[key]

    base = prompts_dir or CREW_PROMPTS_DIR
    path = base / f"{key}.md"
    if not path.exists():
        raise FileNotFoundError(f"crew prompt not found: {path}")
    m = _FENCE_RE.search(path.read_text(encoding="utf-8"))
    if not m:
        raise ValueError(f"crew prompt {path} has no fenced prompt block")
    prompt = m.group(1).strip()
    if prompts_dir is None:
        _PROMPT_CACHE[key] = prompt
    return prompt


# ── Result contract (specialist → Otto/Kai) ──────────────────────────────────────

@dataclass
class SpecialistResult:
    """What a specialist hands back. `findings` is the evidence Kai synthesizes;
    `status` is what the orchestrator branches on (plan Part A.2)."""
    status: str                          # "done" | "needs:<domain>" | "blocked:<reason>"
    findings: str                        # distilled tool outputs / what was done
    tools: list[str] = field(default_factory=list)  # tool names actually called
    for_: str = ""                       # residual subtask for the next worker (optional)

    @property
    def needs(self) -> str | None:
        """The escalation target specialist, if this result is a needs: handback."""
        if self.status.startswith("needs:"):
            return NEEDS_TO_SPECIALIST.get(self.status.split(":", 1)[1].strip())
        return None

    @property
    def blocked(self) -> bool:
        return self.status.startswith("blocked:")


def parse_specialist_status(text: str) -> tuple[str, str]:
    """Read a specialist's final message → (status, residual_subtask).

    `^needs:<one of six tokens>` → ("needs:<token>", <for: line or "">).
    `^blocked:<reason>` → ("blocked:<reason>", "").
    Anything else → ("done", "").  Detection mirrors _classify_tool_result's home.
    """
    stripped = text.strip()
    m = _NEEDS_RE.match(stripped)
    if m:
        token = m.group(1).lower()
        for_m = re.search(r"^\s*for:\s*(.+)", stripped, re.I | re.M)
        return f"needs:{token}", (for_m.group(1).strip() if for_m else "")
    m = _BLOCKED_RE.match(stripped)
    if m:
        return f"blocked:{m.group(1).strip()}", ""
    return "done", ""


def parse_otto_decision(text: str) -> tuple[str, str, str] | None:
    """Read Otto's one-line decision.

    Returns ("dispatch", specialist, subtask) — specialist validated against
    SPECIALISTS — or ("finish", summary, "") on FINISH, or None if neither
    parses (caller should then finish). DISPATCH is checked before FINISH so a
    stray "finish" word inside a subtask doesn't end the loop early.
    """
    for line in text.splitlines():
        m = _DISPATCH_RE.search(line)
        if m:
            name = m.group(1).strip().capitalize()
            if name in SPECIALISTS:
                return ("dispatch", name, m.group(2).strip())
        f = _FINISH_RE.search(line)
        if f:
            return ("finish", f.group(1).strip(), "")
    return None
