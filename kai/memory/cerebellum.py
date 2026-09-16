"""
Cerebellum — execution validation layer for Kai's tool chain.

Runs at two points in every tool chain:
  pre_check()  — before a tool fires: intent drift, scope, loop detection
  post_check() — after a tool returns: output coherence

Results:
  CLEAR (0) — proceed normally
  FLAG  (1) — inject a warning into the message stream; Kai decides whether to continue
  STOP  (2) — abort the chain; Kai explains what happened

All checks use FAST_EMBED (384-dim CPU, ~5ms) — never the main LLM.
Logging is async so checks add <10ms to tool latency.

Human brain analog: the real cerebellum receives an efference copy of a motor
command BEFORE it executes, predicts the expected sensory outcome, and corrects
in real time when reality diverges. This does the same thing for tool calls.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import IntEnum

import kai.config as cfg
from kai.llm.vecmath import cosine_distance as _cosine_distance  # shared cosine math
from kai.util.ids import short_id


class Verdict(IntEnum):
    CLEAR = 0
    FLAG = 1
    STOP = 2


@dataclass
class CerebellarResult:
    verdict: Verdict
    reason: str
    score: float  # cosine distance from intent (0.0 = identical, ~1.0 = orthogonal)


# ── Write-capable tools ────────────────────────────────────────────────────────
# Anything that can modify state on the host or a remote node.
# Pre-check applies tighter drift thresholds for these.
_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "files.write",
        "files.edit",
        "files.append",
        "files.delete",
        "sandbox.propose_move",
        "sandbox.propose_delete",
        "sandbox.propose_rename",
        "sandbox.approve",
        "system.clear_temp_files",
        "system.run_disk_cleanup",
        "system.create_restore_point",
        "system.repair_files",
        "system.disable_startup_program",
        "system.kill_process",
        "pc.deep_scan",
        "self.apply_persona_update",
        "workspace.git_clone",
        "workspace.git_pull",
    }
)

# ── Data-returning tools ───────────────────────────────────────────────────────
# These should produce substantial output. A very short return is suspicious.
_DATA_TOOLS: frozenset[str] = frozenset(
    {
        "system.info",
        "system.temps",
        "system.crashes",
        "pc.network_info",
        "pc.event_logs",
        "pc.startup_programs",
        "files.read",
        "files.list",
        "files.find_large",
        "files.find_old",
        "search.web",
        "docs.search",
        "network.full_diagnostic",
    }
)

# ── Error patterns in tool output ─────────────────────────────────────────────
_ERROR_PATTERNS = re.compile(
    r"(Traceback \(most recent call last\)"
    r"|PermissionError|FileNotFoundError|OSError\b"
    r"|Access is denied|Access denied"
    r"|command not found"
    r"|No such file or directory"
    r"|Connection refused|timed out"
    r"|\bERROR:\s|\bCRITICAL:\s|\bFATAL:\s)",
    re.IGNORECASE,
)

# ── Answer-hedge patterns (post-answer grounding check) ────────────────────────
# The "denies/can't despite tools ran" failure: the turn executed tools, but the
# final answer says it couldn't get the data or asks the user for permission to do
# what it could just do ("I don't have Boise weather… want me to try again?").
# Matched only when tools ran — "I don't have that" is a fine answer on a chat turn.
_HEDGE_DENIAL_RE = re.compile(
    r"\b(?:"
    r"i (?:don'?t|do not) have (?:the|any|specific|access|enough|that|it|current)"
    r"|i (?:couldn'?t|could not|was ?n'?t able to|cannot|can'?t|am unable to|wasn'?t able to)"
    r"\s+(?:find|get|retrieve|access|determine|locate|pull|obtain|complete|confirm)"
    r"|i (?:don'?t|do not) (?:have|see) (?:any )?(?:data|info|information|results?|access)"
    r"|(?:the )?(?:last|previous|earlier) (?:check|result|search|attempt|lookup)"
    r"\s+(?:provided|returned|gave|showed|was for)"
    r"|no (?:data|information|results?|reading) (?:available|found|returned|for)"
    r"|unable to (?:find|get|retrieve|access|determine|complete|confirm)"
    r"|i wasn'?t able to (?:find|get|retrieve|pull|determine)"
    r")",
    re.IGNORECASE,
)

_bg = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cerebellum-log")


# ── Internal helpers ──────────────────────────────────────────────────────────


def _embed_action(tool_name: str, tool_args: dict) -> list[float] | None:
    """Embed a text description of the proposed tool call. Returns None on failure."""
    try:
        from kai.llm.embed import embed as _fast_embed

        arg_summary = " ".join(str(v) for v in tool_args.values() if v)
        text = f"tool {tool_name} {arg_summary}".strip()
        return _fast_embed(text)
    except Exception:
        return None


# ── Public interface ──────────────────────────────────────────────────────────


def call_signature(tool_name: str, tool_args: dict) -> str:
    """Canonical signature for loop detection — same tool + same args.

    The brain records one of these per executed call and passes the list to
    pre_check, so only literally-identical repeats count as a loop. The same
    tool with different args (reading several files) is progress, not a loop.
    """
    try:
        args_part = json.dumps(tool_args, sort_keys=True, default=str)
    except Exception:
        args_part = str(tool_args)
    return f"{tool_name}|{args_part}"


def pre_check(
    tool_name: str,
    tool_args: dict,
    intent_emb: list[float],
    tools_called: list[str],
) -> CerebellarResult:
    """
    Run before a tool fires.

    Checks:
      1. Intent drift — is this tool semantically aligned with what the user asked?
      2. Write scope — is a write tool being called with suspicious drift?
      3. Loop detection — has this exact call (tool + args) repeated with no progress?

    tools_called: call_signature() strings of the calls already executed this
    turn (see Brain._run_tool_rounds).
    """
    if not cfg.CEREBELLUM_ENABLED:
        return CerebellarResult(Verdict.CLEAR, "disabled", 0.0)

    dist = 0.0

    # ── 1. Intent drift ───────────────────────────────────────────────────────
    action_emb = _embed_action(tool_name, tool_args)
    if action_emb:
        dist = _cosine_distance(intent_emb, action_emb)

        is_write = tool_name in _WRITE_TOOLS
        drift_warn = cfg.CEREBELLUM_WRITE_DRIFT_WARN if is_write else cfg.CEREBELLUM_DRIFT_WARN
        drift_stop = cfg.CEREBELLUM_WRITE_DRIFT_STOP if is_write else cfg.CEREBELLUM_DRIFT_STOP

        if dist >= drift_stop:
            return CerebellarResult(
                Verdict.STOP,
                f"Intent drift {dist:.2f} exceeds stop threshold {drift_stop} for {tool_name}",
                dist,
            )
        if dist >= drift_warn:
            return CerebellarResult(
                Verdict.FLAG,
                f"Intent drift {dist:.2f} for {tool_name} — may not match the request",
                dist,
            )

    # ── 2. Loop detection ─────────────────────────────────────────────────────
    call_count = tools_called.count(call_signature(tool_name, tool_args))
    if call_count >= 3:
        return CerebellarResult(
            Verdict.STOP,
            f"Loop detected: {tool_name} called {call_count} times this turn "
            "with identical arguments",
            dist,
        )

    return CerebellarResult(Verdict.CLEAR, "ok", dist)


def post_check(
    tool_name: str,
    result: dict,
    intent_emb: list[float],
) -> CerebellarResult:
    """
    Run after a tool returns.

    Checks:
      1. Hidden errors — tool said success=True but output contains error text
      2. Empty data — a data-returning tool returned nearly nothing
    """
    if not cfg.CEREBELLUM_ENABLED:
        return CerebellarResult(Verdict.CLEAR, "disabled", 0.0)

    output = str(result.get("output", ""))
    success = result.get("success", True)

    # ── 1. Hidden error pattern ───────────────────────────────────────────────
    if success and _ERROR_PATTERNS.search(output):
        return CerebellarResult(
            Verdict.FLAG,
            f"{tool_name} reported success but output contains an error pattern",
            0.0,
        )

    # ── 2. Empty output from a data tool ─────────────────────────────────────
    if tool_name in _DATA_TOOLS and len(output.strip()) < 20:
        return CerebellarResult(
            Verdict.FLAG,
            f"{tool_name} returned suspiciously little data ({len(output.strip())} chars)",
            0.0,
        )

    return CerebellarResult(Verdict.CLEAR, "ok", 0.0)


def verify_answer(answer: str, user_input: str, tools_used: list[str]) -> CerebellarResult:
    """Deterministic post-answer grounding check (no model, no embedding).

    Flags the "hedge/deny despite tools" failure: the turn ran tools, but the
    final answer says it couldn't get the data or asks permission to do what it
    could just do (the Boise → "I don't have that, want me to check?" case). The
    caller nudges once and regenerates on FLAG. Two guards keep it precise:

      • Only tool turns are checked — "I don't have that" is a legitimate answer
        when nothing was supposed to run (a chat turn is always CLEAR).
      • It keys on an explicit denial/inability phrase, NOT a trailing offer — so
        "CPU is 52°C. Want me to check the GPU too?" (a helpful follow-up after a
        real answer) does not trip it; only a leading/standalone "I couldn't get
        it" does.
    """
    if not tools_used or not (answer or "").strip():
        return CerebellarResult(Verdict.CLEAR, "no-tools-or-empty", 0.0)
    if _HEDGE_DENIAL_RE.search(answer):
        return CerebellarResult(
            Verdict.FLAG,
            f"answer denies/can't-complete despite {len(tools_used)} tool(s) run",
            1.0,
        )
    return CerebellarResult(Verdict.CLEAR, "ok", 0.0)


# Numbers Kai may state without a tool having produced them: the ordinals and
# small counts that show up in ordinary prose ("the 3 drives", "step 2"), plus
# percentages of 100. Without this every sentence with "one" or "100%" in it
# would look ungrounded.
_FREE_NUMBERS = {0.0, 1.0, 2.0, 3.0, 100.0}
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")

# How many untraceable numbers before it is fabrication rather than arithmetic.
# Derived, correct answers exist — "312.1 total, 163.7 used" legitimately yields
# "148 GB free", a number appearing in no tool result — so a single stray value
# must not trip this. The real failure looked nothing like that: six invented
# capacities across three invented device names in one reply.
# Two was measured to be enough: a probe answer with two invented values (91.4%,
# 285.3 GB against a tool that said 55.3% / 163.7 GB) passed at three (P1-1b).
_UNGROUNDED_LIMIT = 2


def _numbers(text: str) -> list[float]:
    out = []
    for m in _NUM_RE.finditer(text or ""):
        try:
            out.append(float(m.group()))
        except ValueError:
            pass
    return out


def number_trace(answer: str, evidence: str) -> tuple[int, list[float]]:
    """(numbers checked, those tracing to no evidence number). Same 2% tolerance as
    check_grounded; (0, []) when the evidence carries no numbers."""
    have = _numbers(evidence)
    if not have:
        return 0, []
    checked, stray = 0, []
    for n in _numbers(answer):
        if n in _FREE_NUMBERS:
            continue
        checked += 1
        if not any(abs(n - h) <= max(0.05, abs(h) * 0.02) for h in have):
            stray.append(n)
    return checked, stray


def check_grounded(answer: str, evidence: str) -> CerebellarResult:
    """Deterministic fabrication check: do the answer's numbers trace to evidence?

    The counterpart to verify_answer, which only catches the opposite failure —
    hedging DESPITE evidence. Nothing checked the reverse, so a turn could invent
    values wholesale and pass: asked to find more drives, Kai was handed folder
    sizes and replied with /dev/sda1 32/50 GB, /dev/sdb1 180/250, /dev/sdc1
    40/100. Not one of those numbers appeared in any tool result.

    This is the same test kaibench's check_grounded scores runs with. It existed
    only in the harness; a detector that never runs in production catches nothing.

    Tolerant on purpose — a value within 2% of an evidence number counts as
    traced (rounding, unit conversion), and only _UNGROUNDED_LIMIT untraceable
    numbers FLAG. The caller nudges and regenerates once, so a false positive
    costs one retry, never a wrong answer.
    """
    if not (answer or "").strip() or not (evidence or "").strip():
        return CerebellarResult(Verdict.CLEAR, "no-answer-or-evidence", 0.0)
    if not _numbers(evidence):
        return CerebellarResult(Verdict.CLEAR, "evidence carries no numbers", 0.0)
    _, stray = number_trace(answer, evidence)
    if len(stray) >= _UNGROUNDED_LIMIT:
        return CerebellarResult(
            Verdict.FLAG,
            f"{len(stray)} numbers in the answer trace to no tool result: {stray[:6]}",
            1.0,
        )
    return CerebellarResult(Verdict.CLEAR, f"{len(stray)} untraced (under limit)", 0.0)


# ── Claims of work done (the commitment check) ─────────────────────────────────
# A reply may report an action as done only if a tool that performs it ran this turn
# and its result says it happened. Backing comes from what executed and what it
# returned, not from tool-name substrings: those let sandbox.propose_rename "back" a
# rename that never happened, and flagged a real research.add_to_library as unbacked
# (T1-21).

COMPLETED_ACTION_RE = re.compile(
    r"\bi(?:'ve|\s+have)?\s+(?:just\s+|now\s+|already\s+|successfully\s+)?"
    r"(?:created|saved|written|wrote|made|added|appended|deleted|removed|renamed|moved|"
    r"installed|configured|updated|generated|stored|placed|set\s+up|checked|scanned|"
    r"searched|verified|trashed|filed|applied)\b"
    r"|\b(?:has|have|was|were)\s+been?\s*(?:created|saved|written|added|updated|"
    r"removed|deleted|moved|renamed|stored|trashed|applied)\b"
    r"|\b(?:was|were)\s+(?:created|saved|written|added|updated|removed|deleted|moved|"
    r"renamed|stored|trashed)\b"
    r"|^\s*(?:done|saved|created|added)\b[.!:—-]"
    r"|\b(?:saved|written|created|stored)\s+(?:it\s+)?(?:to|at|in|under)\s+[`'\"/~]",
    re.IGNORECASE | re.MULTILINE,
)

_IN_PROGRESS_RE = re.compile(
    r"\b(?:i am|i'm|i have started|i've started|now)\s+(?:executing|running|deleting|"
    r"removing|moving|renaming|applying|starting|scanning|killing|clearing|cleaning|"
    r"updating|installing)\b"
    r"|\b(?:executing|running|doing)\s+(?:it|that|this|the\s+(?:action|operation|scan|"
    r"deletion))\s+now\b"
    r"|\breceived\s+(?:your\s+|the\s+)?(?:authori[sz]ation|approval|go-?ahead)\b"
    r"|\b(?:is|are)\s+(?:now\s+)?(?:running|in\s+progress|underway)\b",
    re.IGNORECASE,
)

_WRITE_TOOLS = frozenset(
    {
        "files.write",
        "files.append",
        "files.edit",
        "notes.save",
        "notes.commit",
        "tree.save",
        "goals.create",
        "goals.update",
        "research.add_to_library",
        "sandbox.copy_to_workspace",
        "browser.screenshot",
        "memory.reflect",
        "workspace.git_clone",
        "self.apply_persona_update",
    }
)
_DELETE_TOOLS = frozenset({"docs.delete", "goals.abandon"})
# A result that says nothing happened backs nothing, whatever its success flag says.
_NOT_DONE_RE = re.compile(
    r"^\s*(?:this tool is only available|not found|source not found|already gone|"
    r"proposal '[^']*' not found|blocked|rejected|error|could not|failed|"
    r"operation failed|permission denied|unknown)",
    re.IGNORECASE,
)
_CLAIM_KINDS = (
    (
        "rename",
        re.compile(r"\bre-?named\b", re.IGNORECASE),
        lambda name, out: name == "sandbox.approve" and out.startswith("Renamed"),
    ),
    (
        "delete",
        re.compile(r"\b(?:deleted|removed|erased|trashed)\b", re.IGNORECASE),
        lambda name, out: (
            (name == "sandbox.approve" and out.startswith("Moved to trash"))
            or name in _DELETE_TOOLS
        ),
    ),
    (
        "move",
        re.compile(r"\bmoved\b", re.IGNORECASE),
        lambda name, out: name == "sandbox.approve" and out.startswith("Moved"),
    ),
    (
        "write",
        re.compile(
            r"\b(?:created|saved|wrote|written|appended|stored|filed|added)\b", re.IGNORECASE
        ),
        lambda name, out: name in _WRITE_TOOLS,
    ),
)
_GENERIC_DONE_RE = re.compile(
    r"\b(?:done|completed|finished|killed|disabled|cleared|cleaned|repaired|terminated|applied)\b",
    re.IGNORECASE,
)
# A sentence that negates, hedges, asks, or talks about the future is not a claim.
_NOT_A_CLAIM_RE = re.compile(
    r"\b(?:not|never|no|nothing|didn't|did not|haven't|hasn't|wasn't|weren't|won't|can't|"
    r"cannot|couldn't|unable|will|would|can|could|should|once|if|when|to be|ready to|"
    r"going to|about to|proposal|proposed|propose)\b|\?\s*$",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _claim_sentences(text: str) -> list[str]:
    parts = (p.strip() for p in _SENTENCE_SPLIT_RE.split(text or ""))
    return [p for p in parts if p and not _NOT_A_CLAIM_RE.search(p)]


def claims_work(text: str) -> bool:
    """True if a reply says something is running or done. Used where nothing has run."""
    text = text or ""
    if _IN_PROGRESS_RE.search(text) or COMPLETED_ACTION_RE.search(text):
        return True
    return any(pattern.search(s) for s in _claim_sentences(text) for _, pattern, _ in _CLAIM_KINDS)


def check_claims_backed(answer: str, executed: list[tuple[str, str]]) -> CerebellarResult:
    """Deterministic commitment check: work the answer reports as done must be backed by
    a tool that performs it and actually ran this turn.

    `executed` is [(tool name, output)] for calls that ran. An output that says nothing
    happened ("not found", "only available on Windows") backs nothing.
    """
    sentences = _claim_sentences(answer)
    if not sentences:
        return CerebellarResult(Verdict.CLEAR, "no claims", 0.0)
    real = [(n, o) for n, o in executed if not _NOT_DONE_RE.search(o or "")]
    unbacked = [
        kind
        for kind, pattern, backs in _CLAIM_KINDS
        if any(pattern.search(s) for s in sentences) and not any(backs(n, o) for n, o in real)
    ]
    if (
        not unbacked
        and executed
        and not real
        and any(_GENERIC_DONE_RE.search(s) for s in sentences)
    ):
        unbacked = ["work (every tool this turn reported that it did nothing)"]
    if unbacked:
        return CerebellarResult(Verdict.FLAG, ", ".join(unbacked), 1.0)
    return CerebellarResult(Verdict.CLEAR, "claims backed", 0.0)


def log_result(
    tool_name: str,
    phase: str,
    result: CerebellarResult,
    user_id: int = 0,
    tool_args: dict | None = None,
    output_snippet: str = "",
) -> None:
    """Fire-and-forget async log. Never blocks the main thread."""
    _bg.submit(_write_log, tool_name, phase, result, user_id, tool_args or {}, output_snippet)


def _write_log(
    tool_name: str,
    phase: str,
    result: CerebellarResult,
    user_id: int,
    tool_args: dict,
    output_snippet: str,
) -> None:
    try:
        from kai.store.db import get_conn

        conn = get_conn()
        conn.execute(
            "INSERT INTO cerebellum_log "
            "(id, user_id, ts, tool_name, phase, verdict, score, reason, tool_args, output_snippet) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                short_id(16),
                user_id,
                time.time(),
                tool_name,
                phase,
                result.verdict.name.lower(),
                round(result.score, 4),
                result.reason,
                json.dumps(tool_args),
                output_snippet[:500],
            ),
        )
        conn.commit()
    except Exception:
        pass  # logging must never break the main flow
