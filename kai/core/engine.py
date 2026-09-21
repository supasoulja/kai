"""
TurnEngine — the tool-calling mechanism, extracted from Brain (the god-object).

Owns the machinery every turn shares regardless of *who* is driving it: the
tool-round loop, per-tool execution + cerebellum checks, broken/narrated
tool-call recovery, the chat/tool-model primitives, and tool-model resolution.
Kai's generalist Brain and each crew specialist are thin *policy* configs
(persona, tool slice, model, context depth) that drive this one *mechanism*.

Runtime state that the app's public API mutates mid-session (the active chat
brain, the cancel flag, the session id, the temperature, the tool level) lives
on the host Brain; the engine reads/writes it through the proxy properties
below. This mirrors CrewRunner's honest back-reference pattern — the coupling is
explicit and confined to one file, not scattered.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Generator
from datetime import datetime
from typing import TYPE_CHECKING, NamedTuple

import kai.config as cfg
from kai.config import CHAT_MODEL
from kai.core import events
from kai.core import flow as flow_rec
from kai.memory.cerebellum import COMPLETED_ACTION_RE
from kai.memory.privacy import patterns_enabled
from kai.tools.registry import (
    TOOL_LABELS as _TOOL_LABELS,
)
from kai.tools.registry import ToolUnavailable
from kai.tools.registry import (
    confirm_tool_names as _confirm_tool_names,
)
from kai.util import log
from kai.util.ids import short_id
from kai.util.text import strip_thinking as _strip_thinking

if TYPE_CHECKING:
    from kai.core.brain import Brain

# Tools that need user confirmation before execution — derived from the
# registry's "destructive" risk tier (single source of truth).
_CONFIRM_TOOLS = _confirm_tool_names()


MAX_TOOL_ROUNDS = 8  # increased to support multi-step tasks (scan → restore point → fix)


class _ToolBatchOutcome(NamedTuple):
    """Result of executing one model response's batch of tool calls."""

    confirm_intercepted: bool  # a confirm-gated tool paused the chain
    chain_stopped: bool  # cerebellum STOP — end rounds, never escalate
    rounds_done: bool  # duplicate calls piling up — answer with what we have
    tool_error: bool  # a tool raised (hard error) → escalation
    win_error_code: bool  # tool output carried a Windows hex error code
    dup_count: int  # running count of duplicate calls this turn


_KAI_RETRY_SIGNALS = re.compile(
    r"let\s+me\s+(try|check|look|see|investigate|figure|attempt|search|test)|"
    r"i('ll| will)\s+(try|check|look|see|attempt|investigate|search|test)|"
    r"let\s+me.{0,40}(again|another|different|instead)|"
    r"(trying|attempt(ing)?)\s+(a\s+)?(different|another|alternative)",
    re.IGNORECASE,
)

# Zero-tool replies that talk as if work, a check, or an approval already happened. While
# the tools are still on offer the model is sent back once to do the work instead of
# describing it. Once a tool has run, Brain grounds and checks the final answer instead
# (cerebellum.check_claims_backed), so these apply only to turns where nothing ran.
# On 2026-09-10 Kai claimed a folder, a saved file and its path with zero tool calls,
# and listed eight invented startup apps.
_SYSTEM_CLAIM_RE = re.compile(
    r"\byour\s+(?:system|pc|computer|machine|laptop|drives?|disks?|ssd|gpu|cpu|ram|network|"
    r"wi-?fi|startup(?:\s+programs?)?|boot|updates?|packages?|processes|temps?|temperatures?|"
    r"fans?|logs?|files?|folders?|workspace)\b[^.?!\n]{0,60}\b(?:is|are|was|were|has|have|had|"
    r"shows?|looks?|runs?|reads?|includes?|contains?|lists?|reports?|indicates?|uses?|"
    r"currently|running|healthy|stable|at\s+\d)"
    r"|\b(?:no|zero|there (?:are|is|were|was|have been|has been) no)\b[^.?!\n]{0,30}"
    r"\b(?:crash(?:es)?|errors?|updates?|issues?|failures?|resets?)\b[^.?!\n]{0,40}"
    r"\b(?:found|detected|pending|reported|available|logged|recorded|in the (?:last|past))\b"
    r"|\b(?:is|are)\s+(?:currently\s+|fully\s+)?up to date\b"
    r"|\b(?:scan|check|search|diagnostic)\s+(?:is|was|has)\s+(?:been\s+)?"
    r"(?:complete|completed|finished|done)\b",
    re.IGNORECASE,
)
_APPROVAL_ASK_RE = re.compile(
    r"\b(?:shall|should)\s+i\s+(?:proceed|go ahead|run|start|continue)\b"
    r"|\b(?:need|want|require)s?\s+your\s+(?:approval|permission|ok|confirmation|go-?ahead)\b"
    r"|\bdo you (?:approve|want me to (?:proceed|run it|go ahead))\b",
    re.IGNORECASE,
)
# A deliverable promised for later. There is no later: a turn that ends in "I'll put
# together that presentation" ends with nothing made (2026-09-21, design.render offered and
# ranked first). Only artifact nouns count, so "let me write a summary:" followed by the
# summary itself does not trip it.
_PROMISED_WORK_RE = re.compile(
    r"\b(?:i'?ll|i\s+will|i'?m|i\s+am|let\s+me|going\s+to)\s+(?:now\s+|go\s+ahead\s+and\s+)?"
    r"(?:put\w*\s+together|draft\w*|creat\w*|mak\w*|prepar\w*|generat\w*|build\w*|design\w*|"
    r"whip\w*\s+up)\b[^.?!\n]{0,60}?\b(?:flyers?|posters?|slides?|slide\s*deck|presentations?|"
    r"decks?|spreadsheets?|pdfs?|handouts?|brochures?)\b",
    re.IGNORECASE,
)
_ZERO_TOOL_NUDGES = (
    (
        COMPLETED_ACTION_RE,
        "claim",
        "You described work as already done, but no tool ran this turn, so it did not happen. "
        "Call the tool that does it now, or say plainly that you did not do it and why. Do "
        "not state a path you have not read back.",
    ),
    (
        _SYSTEM_CLAIM_RE,
        "system_fact",
        "You stated facts about this machine, but no tool ran this turn. Call the tool that "
        "checks it, or say plainly that you have not checked. Do not answer from memory or "
        "guesswork.",
    ),
    (
        _PROMISED_WORK_RE,
        "promised",
        "You said you would make it, but no tool ran, and nothing happens after this reply. "
        "Make it now with the tool that does it. If you need a detail from the user first, "
        "ask for that plainly instead of promising.",
    ),
    (
        _APPROVAL_ASK_RE,
        "approval_ask",
        "Nothing is waiting for approval: no tool was called. If the user asked for an "
        "action, call its tool now; the system asks the user for approval itself when it is "
        "needed. If you need a clarification, ask it plainly.",
    ),
)
# Left for the final answer when the model still answers that way after one send-back.
NO_TOOL_RAN_NOTE = (
    "No tool ran this turn. Do not present anything about this machine as checked or done. "
    "If the user needs live information or an action, say plainly that you have not checked "
    "or not done it."
)


def _zero_tool_nudge(text: str) -> tuple[str, str] | None:
    """(kind, corrective) for a reply that claims work, checks or approval with nothing run."""
    for pattern, kind, corrective in _ZERO_TOOL_NUDGES:
        if pattern.search(text):
            return kind, corrective
    return None


_BROKEN_TOOL_CALL_RE = re.compile(
    r'"name"\s*:\s*"([a-z][a-z0-9_.]+)".*?"arguments"\s*:\s*(\{[^}]*\})',
    re.DOTALL,
)

_NARRATED_VERB_RE = re.compile(
    r"\b(run(?:ning)?|use|using|call(?:ing)?|execut\w*|invok\w*|"
    r"try(?:ing)?|start(?:ing)?|launch(?:ing)?|perform(?:ing)?|"
    r"fir(?:e|ing)|check(?:ing)?|read(?:ing)?|open(?:ing)?|"
    r"look(?:ing)?(?:\s+at)?|list(?:ing)?|fetch(?:ing)?|grab(?:bing)?|"
    r"cat|scan(?:ning)?|"
    # intent markers — not verbs, but they announce a call just as clearly:
    # "let me files.read that", "I'll system.info real quick"
    r"let\s+me|i'?ll|i\s+will|going\s+to|gonna|about\s+to)\b",
    re.IGNORECASE,
)

_NARRATED_INTENTS: list[tuple[re.Pattern, str, str]] = [
    # "searching the web for the latest gemma release", "let me look that up — for X"
    (
        re.compile(
            r"\b(?:search\w*|look\w*\s*up|googl\w*)\b[^.?!]*?\bfor\s+[`'\"]?(.+?)[`'\"]?\s*[.?!]*\s*$",
            re.IGNORECASE,
        ),
        "search.web",
        "query",
    ),
]


_SECRET_ARG_RE = re.compile(r"pass|secret|token|api[_-]?key|auth|credential", re.I)


def tool_receipt(name: str, args: dict, result: dict, duration_ms: int) -> dict:
    """What the UI shows for a call that ran. Built from the call and its result, never from a
    status label, so an answer can only show tools that executed (T2-23, T2-28)."""
    shown = {}
    for key, value in (args or {}).items():
        if _SECRET_ARG_RE.search(str(key)):
            value = "•••"
        elif not isinstance(value, (int, float, bool)):
            value = str(value)
            if len(value) > 120:
                value = value[:117] + "…"
        shown[key] = value
    ok = bool(result.get("success"))
    receipt = {
        "name": name,
        "args": shown,
        "ok": ok,
        "error": "" if ok else str(result.get("error") or "")[:300],
        "kind": result.get("kind", ""),
        "duration_ms": duration_ms,
        "output": str(result.get("output") or "")[:600] if ok else "",
    }
    # Files a tool made (design.*) become previews and links under the receipt. Only our own
    # route's URLs pass, so no tool can put an arbitrary address into the page.
    output = result.get("output")
    if ok and isinstance(output, dict) and isinstance(output.get("files"), list):
        files = [
            {"name": str(f.get("name", "")), "url": f["url"], "preview": bool(f.get("preview"))}
            for f in output["files"]
            if isinstance(f, dict) and str(f.get("url", "")).startswith("/api/designs/")
        ]
        if files:
            receipt["files"] = files
    return receipt


def _proposal_problem(tool_args: dict) -> str | None:
    """Why a sandbox.approve call would approve nothing, or None if its proposal is live."""
    try:
        from kai.tools.compute.sandbox import proposal_problem
    except Exception:
        return None
    return proposal_problem((tool_args or {}).get("proposal_id"))


def _proposal_label(tool_args: dict) -> str | None:
    """What approving this proposal does ("Delete notes.txt"), or None if it isn't live."""
    try:
        from kai.tools.compute.sandbox import proposal_label
    except Exception:
        return None
    return proposal_label((tool_args or {}).get("proposal_id"))


def _match_narrated_intent(clean: str, known_tools: set[str]) -> dict | None:
    """Match a natural-language promised action and build a call WITH its arg.

    Returns a synthetic tool_call dict, or None. Skips any intent whose target
    tool isn't registered (so it never fires a call the system can't route).
    """
    for pattern, tool_name, arg_name in _NARRATED_INTENTS:
        if tool_name not in known_tools:
            continue
        m = pattern.search(clean)
        if m:
            value = m.group(1).rstrip(".,;:!?")
            log.debug(f"[recover] narrated intent: {tool_name}({arg_name}={value!r})")
            return {"function": {"name": tool_name, "arguments": {arg_name: value}}}
    return None


def _try_recover_tool_call(
    content: str, known_tools: set[str], needs_args: set[str] | frozenset[str] = frozenset()
) -> dict | None:
    """
    Attempt to extract a tool call from plain-text content the model emitted
    when Ollama failed to parse it as a structured tool_call.

    Three recovery strategies:
      1. Broken JSON — the model emitted {"name": "...", "arguments": {...}} as text
      2. Narrated intent — natural phrasing ("searching the web for X"),
         matched against a deterministic map that also extracts the argument
      3. Narrated call — the model wrote "Running `pc.deep_scan` now" instead of
         calling it (tool named verbatim, fired with empty args, so only for tools in
         known_tools that aren't in needs_args)

    Returns a synthetic tool_call dict matching Ollama's format, or None.
    """
    # Strategy 1: broken JSON
    m = _BROKEN_TOOL_CALL_RE.search(content)
    if m:
        name = m.group(1)
        if name in known_tools:
            try:
                args = json.loads(m.group(2))
            except json.JSONDecodeError:
                raw = m.group(2).replace("'", '"')
                raw = re.sub(r",\s*}", "}", raw)
                try:
                    args = json.loads(raw)
                except json.JSONDecodeError:
                    args = None
            if args is not None:
                return {"function": {"name": name, "arguments": args}}

    # Strategy 2: narrated intent — natural language that promises an action,
    # matched against the deterministic phrase→tool map (extracts the argument).
    _, clean_for_intent = _strip_thinking(content)
    if clean_for_intent and len(clean_for_intent) <= 500:
        intent = _match_narrated_intent(clean_for_intent, known_tools)
        if intent:
            return intent

    # Strategy 3: narrated tool call — model mentions a tool by name in prose
    # e.g. "Running `pc.deep_scan` now" or "I'll use system.info to check"
    # Guards (all must pass — a bare mention is NOT a call):
    #   • < 500 chars — longer text is an explanation, not a failed tool call
    #   • an action verb shortly before the name — "Running pc.deep_scan" is
    #     intent; "pc.deep_scan found nothing last time" is just prose
    #   • no question mark after the name — "Want me to run pc.deep_scan?" is
    #     the model asking permission, and firing the tool would answer for
    #     the user
    _, clean = _strip_thinking(content)
    if not clean or len(clean) > 500:
        return None
    for tool_name in known_tools:
        if tool_name in needs_args:
            continue  # fired with {}, it could only fail, then escalate to every tool (T1-14)
        idx = clean.find(tool_name)
        if idx == -1:
            continue
        # Question guard: only skip when the "?" is in the SAME sentence as
        # the tool name. "Want me to run X?" is a question to the user;
        # "Running X now. Anything else?" is a narrated call we should fire.
        tail = clean[idx + len(tool_name) :]
        q_pos = tail.find("?")
        if q_pos != -1:
            sentence_end = re.search(r"[.!\n]", tail)
            if sentence_end is None or q_pos < sentence_end.start():
                continue
        if not _NARRATED_VERB_RE.search(clean[max(0, idx - 40) : idx]):
            continue
        log.debug(f"[recover] narrated tool call: {tool_name}")
        return {"function": {"name": tool_name, "arguments": {}}}
    return None


class TurnEngine:
    """The shared tool-calling mechanism. One per Brain; the crew drives the
    same instance for its specialists."""

    def __init__(self, host: Brain) -> None:
        self._host = host

    # ── Live runtime state, read from the host Brain ─────────────────────────
    # Proxied (not copied) so a mid-session change on the Brain — active brain
    # swap, Stop, new session id, temperature slider — is seen immediately here.
    @property
    def ollama(self):
        return self._host.ollama

    @property
    def tool_registry(self):
        return self._host.tool_registry

    @property
    def skill_registry(self):
        return self._host.skill_registry

    @property
    def user_id(self) -> int:
        return self._host.user_id

    @property
    def disabled_tools(self) -> set[str]:
        return self._host.disabled_tools

    @property
    def session_id(self):
        return self._host.session_id

    @property
    def model(self) -> str:
        return self._host.model

    @property
    def _cancel(self):
        return self._host._cancel

    @property
    def _chat_client(self):
        return self._host._chat_client

    @property
    def _chat_model(self) -> str:
        return self._host._chat_model

    @property
    def _final_temp(self) -> float:
        return self._host._final_temp

    @property
    def _tool_level(self) -> str:
        return self._host._tool_level

    @property
    def run_skill(self):
        return self._host.run_skill

    @property
    def _fallback_text(self):
        return self._host._fallback_text

    # writable runtime cache — kept on the host so its public API stays the source
    @property
    def _pending_confirm(self):
        return self._host._pending_confirm

    @_pending_confirm.setter
    def _pending_confirm(self, v):
        self._host._pending_confirm = v

    @property
    def _tool_model(self):
        return self._host._tool_model

    @_tool_model.setter
    def _tool_model(self, v):
        self._host._tool_model = v

    @property
    def _tool_model_resolved(self) -> bool:
        return self._host._tool_model_resolved

    @_tool_model_resolved.setter
    def _tool_model_resolved(self, v):
        self._host._tool_model_resolved = v

    # ── Mechanism (moved verbatim from Brain) ────────────────────────────────

    def _resolve_tool_model(self) -> tuple[str | None, bool]:
        """Resolve the active tool level to an installed model (cached).

        Returns (model, available). model=None means: run the rounds on the
        chat model (the "off" level / fallback behavior) — no separate tool
        model, no thinking.
        """
        if self._tool_model_resolved:
            return self._tool_model, self._tool_model is not None
        wanted = cfg.TOOL_MODEL_LEVELS.get(self._tool_level, {}).get("model")
        resolved: str | None = None
        if wanted:
            try:
                if wanted not in self.ollama.installed_models():
                    log.debug(
                        f"[tool model] {wanted} not installed — rounds fall back to {self.model}"
                    )
                else:
                    from kai.llm.models import fits_beside

                    fit = fits_beside(self.ollama, wanted, self.model)
                    if fit is None or fit[0]:
                        resolved = wanted
                    else:
                        log.warn(f"[tool model] {fit[1]} — rounds run on {self.model}")
            except Exception:
                pass  # Ollama unreachable — fall back; apply_tool_level re-checks
        self._tool_model = resolved
        self._tool_model_resolved = True
        return resolved, resolved is not None

    def _chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        think: bool = False,
        temperature: float | None = None,
    ) -> dict:
        """Non-streaming chat on the active brain, with fail→local fallback.

        If a cloud brain raises (dead key, offline, rate-limited), retry once on
        the local chat model so a turn never bricks. Local failures propagate.
        """
        temp = self._final_temp if temperature is None else temperature
        client = self._chat_client
        try:
            return client.chat(
                messages, tools=tools, model=self._chat_model, think=think, temperature=temp
            )
        except Exception as e:
            if client is self.ollama:
                raise
            log.debug(f"[brain] cloud chat failed → local fallback: {e}")
            return self.ollama.chat(
                messages, tools=tools, model=CHAT_MODEL, think=True, temperature=temp
            )

    def _chat_stream(self, messages: list[dict], think: bool, temperature: float):
        """Stream the active brain's reply. A cloud brain that fails BEFORE any
        token falls back to the local chat model; a mid-stream failure keeps what
        was produced and stops cleanly. Yields (token, done, meta) like Ollama."""
        client = self._chat_client
        if client is self.ollama:
            yield from self.ollama.chat_stream(
                messages, tools=None, model=self._chat_model, think=think, temperature=temperature
            )
            return
        emitted = False
        try:
            for chunk in client.chat_stream(
                messages, tools=None, model=self._chat_model, think=think, temperature=temperature
            ):
                emitted = emitted or bool(chunk[0])
                yield chunk
        except Exception as e:
            if emitted:
                log.debug(f"[brain] cloud stream broke mid-answer: {e}")
                return
            log.debug(f"[brain] cloud stream failed pre-token → local: {e}")
            yield from self.ollama.chat_stream(
                messages, tools=None, model=CHAT_MODEL, think=True, temperature=temperature
            )

    def _run_fast_path(
        self,
        tool_name: str,
        messages: list[dict],
        tools_used: list[str],
        *,
        args: dict | None = None,
        query_emb: list[float] | None,
        user_input: str,
        trace_id: str,
        on_status: Callable[[str], None] | None,
    ) -> Generator[tuple[str, bool, dict], None, None]:
        """Execute a deterministically-matched tool WITHOUT a tool-round model
        call (the pre-LLM fast-path — see _match_fast_path).

        Most fast-path tools are no-arg; `args` carries any deterministically
        extracted arguments (e.g. weather.current's location) so the fast-path is
        correct, not just fast — a no-arg weather call silently returns the
        IP-geolocated city instead of the one the user named.

        Synthesizes the tool_call and runs it through the normal execution path
        (_execute_tool_calls: dedup/confirm/cerebellum/result-append) so the tool
        result lands in `messages` exactly as a real round would. run_stream then
        streams the grounded answer. Saves a whole LLM round on common commands.
        """
        from kai.memory.cerebellum import call_signature as _sig_of

        tool_calls = [{"function": {"name": tool_name, "arguments": args or {}}}]
        flow_rec.record(trace_id, "fast_path", name=tool_name, input=user_input)
        messages.append({"role": "assistant", "content": "", "tool_calls": tool_calls})
        # Reuse the batch executor; safe no-arg tools never hit the confirm gate,
        # but yield-through keeps the contract identical if one ever does.
        yield from self._execute_tool_calls(
            tool_calls,
            messages,
            tools_used,
            [],
            0,
            query_emb=query_emb,
            user_input=user_input,
            trace_id=trace_id,
            on_status=on_status,
            sig_of=_sig_of,
        )

    def _run_tool_rounds(
        self,
        messages: list[dict],
        tools_schema: list[dict],
        tools_used: list[str],
        *,
        query_emb: list[float] | None,
        user_input: str,
        trace_id: str,
        on_status: Callable[[str], None] | None,
        keep_prose: bool = False,
        model_override: str | None = None,
    ) -> Generator[tuple[str, bool, dict], None, tuple[str, str] | None]:
        """Run up to MAX_TOOL_ROUNDS of non-streaming tool calls.

        Mutates `messages` in place (assistant tool_calls, tool results,
        corrective prompts) and appends every called tool to `tools_used`.

        keep_prose: normally the tool model's prose is discarded (Kai's voice
        writes the real reply). For a crew specialist the tool model IS the
        worker and its prose is the findings — set True to return that content
        instead of dropping it.

        model_override: force a specific tool model (the crew passes each member's
        roles.json model here). Falls back to the level-resolved model if the
        override isn't installed.

        Rounds run on the chat model by default (one resident model, no thinking
        tax) or on an opt-in secondary tool model. Neither thinks: a dedicated tool
        model emits structured calls natively, and the chat model's occasional "narrate
        instead of call" slip is caught by the pre-LLM fast-paths and the
        narrated-intent recovery net (_match_narrated_intent /
        _try_recover_tool_call) — so we keep tool-calling reliable without paying
        the per-round reasoning latency that used to band-aid the 06-09 regression.

        Returns (raw_content, clean_text) when the model answers in prose instead of
        calling a tool. Brain never ships that prose as the reply (it grounds, checks
        and streams its own); crew specialists keep it as their findings. Returns None
        when the rounds end any other way (the confirm gate fired, a safety stop hit,
        duplicates piled up, or rounds were exhausted).
        """
        from kai.memory.cerebellum import call_signature as _sig_of

        _escalated = False  # True after first tool error → full schema injected
        if model_override:
            # Crew per-agent model. Honour it only if installed, else fall back to
            # the level-resolved model (keeps a bad roles.json from bricking turns).
            try:
                installed = model_override in self.ollama.installed_models()
            except Exception:
                installed = True  # can't check (cloud/offline) — trust the config
            tool_model = model_override if installed else self._resolve_tool_model()[0]
        else:
            tool_model, _ = self._resolve_tool_model()
        rounds_model = tool_model or self._chat_model
        # Tool rounds never think. Thinking adds a multi-thousand-token trace per
        # round (the latency killer) and pulls the chat model off the fast path.
        # Reliability without it comes from the fast-paths + narrated recovery.
        rounds_think = False
        _call_sigs: list[str] = []  # executed (tool, args) signatures for dedup + loop detection
        _dup_count = 0  # repeats of an already-executed identical call
        _corrective_used = 0  # zero-tool replies sent back for claiming work, checks or approval
        for round_num in range(MAX_TOOL_ROUNDS):
            if not tools_schema:
                break  # no tools → skip to streaming final answer
            if self._cancel.is_set():
                break  # user hit Stop — don't fire any more tool calls

            # A secondary tool model stays local; "off" runs rounds on the
            # active chat brain (possibly cloud) via _chat, with fail→local.
            if tool_model is None:
                resp = self._chat(
                    messages,
                    tools=tools_schema,
                    think=rounds_think,
                    temperature=cfg.TEMPERATURE_TOOL,
                )
            else:
                # The opt-in tool model is secondary — unload it right after
                # so it never holds a second runner beside the warm chat model.
                try:
                    resp = self.ollama.chat(
                        messages,
                        tools=tools_schema,
                        model=tool_model,
                        think=rounds_think,
                        keep_alive=0,
                    )
                except Exception as exc:
                    # A tool model that errors or times out mustn't end the turn (T2-30): finish on
                    # the chat model, and don't try the tool model again until the level changes.
                    log.warn(
                        f"[tool model] {tool_model} failed ({exc}); rounds run on the chat model"
                    )
                    flow_rec.record(trace_id, "tool_model_failed", model=tool_model, error=str(exc))
                    self._tool_model, self._tool_model_resolved = None, True
                    tool_model, rounds_model = None, self._chat_model
                    resp = self._chat(
                        messages,
                        tools=tools_schema,
                        think=rounds_think,
                        temperature=cfg.TEMPERATURE_TOOL,
                    )
            msg = resp.get("message", {})
            try:
                _tc_names = [
                    tc.get("function", {}).get("name", "?") for tc in msg.get("tool_calls") or []
                ]
            except Exception:
                _tc_names = ["?"]
            flow_rec.record(
                trace_id,
                "round",
                n=round_num,
                model=rounds_model,
                think=rounds_think,
                messages=len(messages),
                content=msg.get("content", ""),
                thinking=msg.get("thinking", ""),
                tool_calls=", ".join(_tc_names) or "none",
            )

            # Emit tool-round thinking as a step to be shown inline before the
            # tool label in the activity log — not as a floating reasoning dropdown.
            tool_round_thinking = msg.get("thinking", "")
            if tool_round_thinking:
                if self.session_id:
                    events.emit(
                        events.EVENT_THINK,
                        self.session_id,
                        text=tool_round_thinking,
                        round=round_num,
                    )
                yield "", False, {"think_step": True, "text": tool_round_thinking}

            log.debug(
                f"\n[{trace_id}] tool round={round_num} tool_calls={bool(msg.get('tool_calls'))}"
            )

            if not msg.get("tool_calls"):
                content = msg.get("content", "")

                # ── JSON repair: recover broken tool calls from plain text ────
                # Small models sometimes emit tool calls as text instead of
                # structured JSON.  Try to salvage before giving up on the round.
                if self.tool_registry and content:
                    known = set(self.tool_registry.list_tools())
                    needs = {n for n in known if self.tool_registry.required_params(n)}
                    recovered = _try_recover_tool_call(content, known, needs)
                    if recovered:
                        log.debug(
                            f"[{trace_id}] recovered broken tool call: "
                            f"{recovered['function']['name']}"
                        )
                        flow_rec.record(
                            trace_id, "recovered_call", name=recovered["function"]["name"]
                        )
                        msg["tool_calls"] = [recovered]
                        # Fall through to normal tool execution below
                    else:
                        # Recovery failed — continue with normal no-tool path
                        pass

                # If recovery injected tool_calls, fall through to tool execution
                if not msg.get("tool_calls"):
                    if tool_model is not None and not keep_prose:
                        # The tool model's only job is picking tools. Prose
                        # from it is not Kai's voice — discard it and let the
                        # chat model write the real reply in the streamed
                        # final answer. (Crew specialists pass keep_prose=True:
                        # their prose IS the findings, so fall through instead.)
                        flow_rec.record(trace_id, "discarded_prose", text=content)
                        break
                    _, clean = _strip_thinking(content)
                    if not clean:
                        clean = self._fallback_text(tools_used)
                    final = clean

                    # Search raw content (includes <think> blocks) so retry signals
                    # inside thinking are still detected.
                    if tools_used and _KAI_RETRY_SIGNALS.search(content):
                        messages.append({"role": "assistant", "content": final})
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Go ahead — use a different tool or call search.web "
                                    "to find the information another way."
                                ),
                            }
                        )
                        continue

                    # ── Commitment gate (nothing has run yet) ────────────
                    # A reply that reports work, a check, or a pending approval when no
                    # tool ran is a false statement about the system. Sent back once while
                    # the tools are still on offer; past that the model won't be argued
                    # into it, so a note steers the final answer instead.
                    if not tools_used:
                        caught = _zero_tool_nudge(clean)
                        if caught and _corrective_used < 1:
                            _corrective_used += 1
                            flow_rec.record(
                                trace_id, "unbacked_claim", check=caught[0], text=clean[:400]
                            )
                            log.debug(f"[{trace_id}] commitment gate ({caught[0]}): nothing ran")
                            messages.append({"role": "assistant", "content": final})
                            messages.append({"role": "user", "content": caught[1]})
                            continue
                        if caught:
                            messages.append({"role": "system", "content": NO_TOOL_RAN_NOTE})

                    # Prose instead of a tool call. Brain discards it and streams a
                    # grounded, checked reply; crew specialists keep it as findings.
                    return content, final

            # Execute tool calls
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content", ""),
                    "tool_calls": msg["tool_calls"],
                }
            )
            outcome = yield from self._execute_tool_calls(
                msg["tool_calls"],
                messages,
                tools_used,
                _call_sigs,
                _dup_count,
                query_emb=query_emb,
                user_input=user_input,
                trace_id=trace_id,
                on_status=on_status,
                sig_of=_sig_of,
            )
            _dup_count = outcome.dup_count

            # Safety stop — end the rounds entirely. Must come before error
            # escalation: a stop that re-arms the full tool set with "do not
            # give up" isn't a stop (that was the bug this replaced).
            if outcome.chain_stopped:
                break
            # The model keeps repeating an already-executed call — it has the
            # data; move on to the final answer.
            if outcome.rounds_done:
                break
            # Confirm-gated tool was intercepted — skip to final answer so the
            # model can ask the user for confirmation.
            if outcome.confirm_intercepted:
                tools_schema = None
                break

            new_schema, _escalated = self._apply_error_escalation(
                messages,
                tools_used,
                trace_id,
                any_tool_error=outcome.tool_error,
                any_win_error_code=outcome.win_error_code,
                escalated=_escalated,
            )
            if new_schema is not None:
                tools_schema = new_schema

        # Rounds exhausted (or confirm gate / Stop broke out) — continue to
        # the streamed final answer.
        return None

    def _apply_error_escalation(
        self,
        messages: list[dict],
        tools_used: list[str],
        trace_id: str,
        *,
        any_tool_error: bool,
        any_win_error_code: bool,
        escalated: bool,
    ) -> tuple[list[dict] | None, bool]:
        """Append the corrective prompt after a round that errored, escalating the
        recovery (paper "Less is More", Tier 2 fallback):

        - First hard failure → widen to the full tool set so the model has every
          alternative. Returns that schema (the caller swaps it in) + escalated=True.
        - Later failures → push hard, let it exit gracefully if truly stuck.
        - No hard error but a Windows error code (and search.web unused) → point
          it at search.web to look the code up.

        Returns (new_tools_schema_or_None, escalated). A None schema means keep the
        current one.
        """
        if any_tool_error:
            if not escalated and self.tool_registry:
                flow_rec.record(trace_id, "escalation", full_schema=True)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "One or more tools failed. All available tools are now provided — "
                            "pick a different one to complete the task. Do not give up."
                        ),
                    }
                )
                return self.tool_registry.get_schema(), True
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Tools continue to fail. If no suitable tool exists, "
                        "answer from what you know and explain what blocked you."
                    ),
                }
            )
            return None, escalated

        if any_win_error_code and "search.web" not in tools_used:
            # A tool ran but returned a Windows error code (e.g. 0x80240032).
            # search.web is already in the tool schema — direct the model to use it.
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "A tool returned a Windows error code. "
                        "Call search.web to look up the exact error code and find the cause and fix."
                    ),
                }
            )
        return None, escalated

    def _execute_tool_calls(
        self,
        tool_calls: list[dict],
        messages: list[dict],
        tools_used: list[str],
        call_sigs: list[str],
        dup_count: int,
        *,
        query_emb: list[float] | None,
        user_input: str,
        trace_id: str,
        on_status: Callable[[str], None] | None,
        sig_of,
    ) -> Generator[tuple[str, bool, dict], None, _ToolBatchOutcome]:
        """Execute one model response's batch of tool calls.

        Yields a confirm event if a gated tool is hit (the caller breaks to let
        the model ask the user). Mutates `messages`, `tools_used`, `call_sigs` in
        place and returns a _ToolBatchOutcome with the control flags + the running
        duplicate count. The per-tool flow is: dedup guard → confirm gate →
        cerebellum pre → execute + pattern-log + cerebellum post → classify.
        """
        confirm_intercepted = chain_stopped = rounds_done = False
        tool_error = win_error_code = False
        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_name = fn.get("name") or ""
            tool_args = fn.get("arguments", {})

            # Duplicate-call guard: small tool models sometimes re-issue the exact
            # same call instead of writing a final answer. The result is already
            # in the conversation — don't run it again, and after a second repeat
            # stop the rounds so the final answer uses the data already gathered.
            sig = sig_of(tool_name, tool_args)
            if sig in call_sigs:
                dup_count += 1
                flow_rec.record(trace_id, "duplicate_call", name=tool_name)
                messages.append(
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "output": (
                                    "This exact call already ran this turn — "
                                    "its result is above. Answer from it."
                                ),
                                "success": True,
                            }
                        ),
                    }
                )
                if dup_count >= 2:
                    rounds_done = True
                    break
                continue

            # ── Confirm gate: pause and ask the user before running ──────────
            confirm_label = self._needs_confirm(tool_name)
            if confirm_label and tool_name == "sandbox.approve":
                # Say what approving does, not "Executing approved operation".
                confirm_label = _proposal_label(tool_args) or confirm_label
            # A parked approval must point at a proposal that exists. The model invented
            # the id twice, and the user's "go ahead" then approved nothing (T1-15).
            bad_proposal = _proposal_problem(tool_args) if tool_name == "sandbox.approve" else None
            if bad_proposal:
                flow_rec.record(trace_id, "rejected_call", name=tool_name, reason=bad_proposal)
                messages.append(
                    {
                        "role": "tool",
                        "content": json.dumps({"success": False, "error": bad_proposal}),
                    }
                )
                continue
            if confirm_label:
                log.info(f"[confirm] intercepted {tool_name} — waiting for user OK")
                self._pending_confirm = {
                    "name": tool_name,
                    "args": tool_args,
                    "trace_id": trace_id,
                    "label": confirm_label,
                    "call_id": short_id(8),  # the approval card names this call
                }
                result = {
                    "output": (
                        f"⏸ {tool_name} requires your OK before running. "
                        "Describe what you want to do and ask the user to confirm."
                    ),
                    "success": True,
                    "pending": True,  # not evidence: see brain._evidence_from
                }
                messages.append({"role": "tool", "content": json.dumps(result)})
                confirm_event = {
                    "confirm_tool": True,
                    "name": tool_name,
                    "label": self._pending_confirm["label"],
                    "args": tool_args,
                    "call_id": self._pending_confirm["call_id"],
                }
                # Self-edits to persona.md carry the exact diff so the UI can
                # render a reviewable before/after modal — the user approves
                # precisely what will be written.
                if tool_name == "self.apply_persona_update":
                    try:
                        from kai.tools.system.self_inspect import persona_update_diff

                        diff = persona_update_diff(
                            tool_args.get("section", ""),
                            tool_args.get("content", ""),
                        )
                        if diff:
                            confirm_event["diff"] = diff
                    except Exception:
                        pass  # diff is a nicety — never block the confirm flow
                yield "", False, confirm_event
                confirm_intercepted = True
                break  # exit tool loop — wait for user confirmation

            # ── Cerebellum pre-check (may stop the chain) ────────────────────
            if self._cerebellum_pre(tool_name, tool_args, query_emb, call_sigs, messages, trace_id):
                chain_stopped = True
                break

            # Only calls that execute count as used. A parked, duplicate or safety-stopped
            # call must not back a claim, feed grounding, or reach the trace log that
            # self.trace reads back as "what you actually ran".
            tools_used.append(tool_name)
            if on_status:
                on_status(_TOOL_LABELS.get(tool_name, tool_name))
            started = time.monotonic()
            result = self._execute_tool_traced(tool_name, tool_args, trace_id)
            ran_ms = int((time.monotonic() - started) * 1000)
            yield "", False, {"tool_result": tool_receipt(tool_name, tool_args, result, ran_ms)}
            call_sigs.append(sig)

            # ── Pattern tracking ──────────────────────────────────────────────
            if patterns_enabled(self.user_id):
                from kai.memory.patterns import log_tool_call as _log_pattern

                _log_pattern(tool_name, user_id=self.user_id, topic=user_input[:80])

            # ── Cerebellum post-check ─────────────────────────────────────────
            self._cerebellum_post(tool_name, result, query_emb, messages)
            messages.append({"role": "tool", "content": json.dumps(result)})

            hard, win_code = self._classify_tool_result(result)
            tool_error = tool_error or hard
            win_error_code = win_error_code or win_code

        return _ToolBatchOutcome(
            confirm_intercepted,
            chain_stopped,
            rounds_done,
            tool_error,
            win_error_code,
            dup_count,
        )

    def _cerebellum_pre(
        self,
        tool_name: str,
        tool_args: dict,
        query_emb: list[float] | None,
        call_sigs: list[str],
        messages: list[dict],
        trace_id: str,
    ) -> bool:
        """Cerebellum pre-flight check for one tool call.

        Appends any warning (FLAG) or stop (STOP) messages to `messages` in
        place. Returns True when the chain must stop (STOP verdict).
        """
        if not (cfg.CEREBELLUM_ENABLED and query_emb):
            return False
        from kai.memory import cerebellum as _cb

        pre = _cb.pre_check(tool_name, tool_args, query_emb, call_sigs)
        _cb.log_result(tool_name, "pre", pre, self.user_id, tool_args)
        if pre.verdict == _cb.Verdict.STOP:
            messages.append(
                {
                    "role": "tool",
                    "content": json.dumps(
                        {
                            "output": f"[Cerebellum] Chain stopped: {pre.reason}",
                            "success": False,
                        }
                    ),
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"A safety check stopped the chain: {pre.reason}. "
                        "Explain what happened and what you would need to continue."
                    ),
                }
            )
            flow_rec.record(trace_id, "safety_stop", reason=pre.reason)
            return True
        if pre.verdict == _cb.Verdict.FLAG:
            messages.append(
                {
                    "role": "system",
                    "content": f"[Cerebellum] Pre-check warning: {pre.reason}. Proceed carefully.",
                }
            )
        return False

    def _cerebellum_post(
        self, tool_name: str, result: dict, query_emb: list[float] | None, messages: list[dict]
    ) -> None:
        """Cerebellum post-check for one tool result. Appends a FLAG warning in place."""
        if not (cfg.CEREBELLUM_ENABLED and query_emb):
            return
        from kai.memory import cerebellum as _cb

        post = _cb.post_check(tool_name, result, query_emb)
        _cb.log_result(
            tool_name,
            "post",
            post,
            self.user_id,
            output_snippet=str(result.get("output", ""))[:500],
        )
        if post.verdict == _cb.Verdict.FLAG:
            messages.append(
                {
                    "role": "system",
                    "content": f"[Cerebellum] Post-check warning: {post.reason}",
                }
            )

    @staticmethod
    def _classify_tool_result(result: dict) -> tuple[bool, bool]:
        """Return (hard_error, win_error_code) for one tool result.

        hard_error: the tool itself crashed (Python exception / no registry).
        win_error_code: the tool ran but its output carries a Windows hex error
        code (the 0x… pattern — unambiguous, no false positives). This is
        intentionally Windows-only: Linux exit codes are small ints that appear
        in legitimate output, so there is no equally false-positive-free signal
        to key a cross-platform escalation off of.
        """
        if not result.get("success", True):
            # Can't act here, or wrong argument names: the model hears it failed, but a wider
            # tool set won't fix either (D3a).
            if result.get("kind") in ("unavailable", "bad_args"):
                return False, False
            return True, False
        if re.search(r"\b0x[0-9a-fA-F]{4,}\b", str(result.get("output", ""))):
            return False, True
        return False, False

    def _needs_confirm(self, tool_name: str) -> str | None:
        """UI label for a call that must pause for approval, or None.

        Destructive tools pause, and so does a recipe with a destructive step. A recipe
        runs its steps directly, so without this it ran docs.delete unasked (T1-24).
        """
        if tool_name in _CONFIRM_TOOLS:
            return _TOOL_LABELS.get(tool_name, tool_name)
        if tool_name.startswith("skill.") and self.skill_registry:
            recipe = tool_name.removeprefix("skill.")
            risky = [t for t in self.skill_registry.step_tools(recipe) if t in _CONFIRM_TOOLS]
            if risky:
                return f"Recipe {recipe} (runs {', '.join(dict.fromkeys(risky))})"
        return None

    def _execute_tool_traced(
        self, tool_name: str, tool_args: dict, trace_id: str, confirmed: bool = False
    ) -> dict:
        """Execute one tool wrapped in TOOL_START/TOOL_END events + debug log."""
        if self.session_id:
            events.emit(events.EVENT_TOOL_START, self.session_id, name=tool_name, args=tool_args)
        _tool_t0 = time.monotonic()
        result = self._execute_tool(tool_name, tool_args, trace_id, confirmed=confirmed)
        _tool_dur = round(time.monotonic() - _tool_t0, 3)
        if self.session_id:
            # Truncate output for the event bus (keeps SQLite lean)
            _evt_output = str(result.get("output", ""))[:2000]
            events.emit(
                events.EVENT_TOOL_END,
                self.session_id,
                name=tool_name,
                duration=_tool_dur,
                success=result.get("success", False),
                error=result.get("error"),
                output=_evt_output,
                args=tool_args,
            )
        if cfg.DEBUG:
            # Truncate tool output in debug logs to avoid leaking large
            # data blobs (file contents, search results) to the terminal
            _dbg = str(result)
            if len(_dbg) > 300:
                _dbg = _dbg[:300] + "… [truncated]"
            log.debug(f"[{trace_id}] TOOL: {tool_name} → {_dbg}")
        flow_rec.record(
            trace_id,
            "tool",
            name=tool_name,
            args=json.dumps(tool_args),
            success=result.get("success"),
            error=result.get("error"),
            output=str(result.get("output", "")),
        )
        # Provenance rides with the result so the model can cite WHERE a fact came
        # from, not just what it was. Without it a stale third-party feed is
        # indistinguishable from a fabrication: on 2026-09-09 weather.current
        # relayed wttr.in faithfully, the user's own source disagreed, and with no
        # way to say "this is wttr.in as of 13:21" the reply read as invented — and
        # was then confessed to as invented. A tool that cannot name its source
        # cannot defend a correct answer.
        if isinstance(result, dict) and "_source" not in result:
            try:
                shown = json.dumps(tool_args, default=str)
                if len(shown) > 200:
                    shown = shown[:200] + "…"
                result["_source"] = (
                    f"{tool_name} · run {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                    f"· args {shown}"
                )
            except Exception:  # never let provenance break a working tool call
                pass
        return result

    def _execute_tool(self, name: str, args: dict, trace_id: str, confirmed: bool = False) -> dict:
        # Skill delegation — tool names starting with "skill." route to the skill registry
        if name.startswith("skill.") and self.skill_registry:
            skill_name = name.removeprefix("skill.")
            # Validate skill name — reject anything that isn't a safe identifier
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", skill_name):
                return {"success": False, "error": f"Invalid skill name: {skill_name!r}"}
            skill_name = self.skill_registry.resolve(skill_name)
            # A recipe runs its steps directly, so the per-user tool switch is checked here
            # too: turned-off tools used to run through a recipe (T1-23, T2-32).
            disabled = self.disabled_tools or set()
            off = [
                t
                for t in self.skill_registry.step_tools(skill_name)
                if (self.tool_registry.resolve_name(t) if self.tool_registry else t) in disabled
            ]
            if off:
                return {
                    "success": False,
                    "error": (
                        f"Recipe '{skill_name}' uses {', '.join(off)}, which is turned off in "
                        "Settings. Nothing ran."
                    ),
                }
            log.debug(f"[brain] skill delegation: {name!r} → skill {skill_name!r}")
            return self.run_skill(skill_name, args, allow_destructive=confirmed)

        if not self.tool_registry:
            return {"success": False, "error": f"No tool registry — cannot run '{name}'"}
        # ── Enablement gate ──────────────────────────────────────────────────
        # A tool the user turned off in Settings must never run — even if the
        # model names it directly, through a learned alias, or via a crew
        # specialist whose slice still listed it. The schema filter hides
        # disabled tools from the model; this is the authoritative block.
        disabled = self.disabled_tools
        if disabled and self.tool_registry.resolve_name(name) in disabled:
            return {
                "success": False,
                "error": f"'{name}' is turned off in Settings and was not run.",
            }
        # Set thread-local user_id (per-user DB scoping) and session_id (so
        # memory tools can exclude the live conversation from "past sessions").
        from kai.core._app_state import set_current_session_id, set_current_user_id

        set_current_user_id(self.user_id)
        set_current_session_id(self.session_id)
        try:
            output = self.tool_registry.execute(name, args)
            return {"success": True, "output": output}
        except KeyError:
            # Unknown tool name — try alias learning (model may have hallucinated
            # a name). Args are passed so a close-but-wrong tool whose schema
            # can't accept them is rejected instead of called with garbage.
            target = self.tool_registry.learn_alias(name, args=args)
            if target:
                if disabled and self.tool_registry.resolve_name(target) in disabled:
                    return {
                        "success": False,
                        "error": f"'{target}' is turned off in Settings and was not run.",
                    }
                log.debug(f"[brain] alias redirect: {name!r} → {target!r}")
                try:
                    output = self.tool_registry.execute(target, args)
                    return {"success": True, "output": output}
                except Exception as e:
                    return self._failed_call(target, e)
            return {"success": False, "error": f"Unknown tool: {name!r} — no similar tool found"}
        except Exception as e:
            return self._failed_call(name, e)

    def _failed_call(self, name: str, exc: Exception) -> dict:
        """Result for a tool call that raised. Two kinds of failure don't widen the tool set:
        the tool can't act here (T1-06), or the call used argument names the tool doesn't take
        (T1-34), where the real ones are handed back."""
        if isinstance(exc, ToolUnavailable):
            return {"success": False, "error": str(exc), "kind": "unavailable"}
        if isinstance(exc, TypeError) and self.tool_registry.is_arg_error(name, exc):
            hint = self.tool_registry.args_hint(name)
            return {"success": False, "error": f"{exc}. {name} takes: {hint}", "kind": "bad_args"}
        return {"success": False, "error": str(exc)}
