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
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import IntEnum

import kai.config as cfg
from kai.llm.vecmath import cosine_distance as _cosine_distance  # shared cosine math


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
_WRITE_TOOLS: frozenset[str] = frozenset({
    "files.write", "files.edit", "files.append", "files.delete",
    "sandbox.propose_move", "sandbox.propose_delete", "sandbox.propose_rename",
    "sandbox.approve",
    "system.clear_temp_files", "system.run_disk_cleanup",
    "system.create_restore_point", "system.repair_files",
    "system.disable_startup_program", "system.kill_process",
    "pc.deep_scan",
    "self.apply_persona_update",
    "workspace.git_clone", "workspace.git_pull",
})

# ── Data-returning tools ───────────────────────────────────────────────────────
# These should produce substantial output. A very short return is suspicious.
_DATA_TOOLS: frozenset[str] = frozenset({
    "system.info", "system.temps", "system.crashes",
    "pc.network_info", "pc.event_logs", "pc.startup_programs",
    "cluster.node_scan", "cluster.broadcast_scan", "cluster.node_status",
    "files.read", "files.list", "files.find_large", "files.find_old",
    "search.web", "docs.search",
    "network.full_diagnostic",
})

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
                secrets.token_hex(8),
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
