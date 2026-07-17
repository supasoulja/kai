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
from typing import TYPE_CHECKING, NamedTuple

import kai.config as cfg
from kai.config import CHAT_MODEL
from kai.core import events
from kai.core import flow as flow_rec
from kai.memory.privacy import patterns_enabled
from kai.tools.registry import (
    TOOL_LABELS as _TOOL_LABELS,
    confirm_tool_names as _confirm_tool_names,
)
from kai.util.text import strip_thinking as _strip_thinking

if TYPE_CHECKING:
    from kai.core.brain import Brain

# Tools that need user confirmation before execution — derived from the
# registry's "destructive" risk tier (single source of truth).
_CONFIRM_TOOLS = _confirm_tool_names()


MAX_TOOL_ROUNDS   = 8   # increased to support multi-step tasks (scan → restore point → fix)

class _ToolBatchOutcome(NamedTuple):
    """Result of executing one model response's batch of tool calls."""
    confirm_intercepted: bool  # a confirm-gated tool paused the chain
    chain_stopped: bool        # cerebellum STOP — end rounds, never escalate
    rounds_done: bool          # duplicate calls piling up — answer with what we have
    tool_error: bool           # a tool raised (hard error) → escalation
    win_error_code: bool       # tool output carried a Windows hex error code
    dup_count: int             # running count of duplicate calls this turn

_KAI_RETRY_SIGNALS = re.compile(
    r"let\s+me\s+(try|check|look|see|investigate|figure|attempt|search|test)|"
    r"i('ll| will)\s+(try|check|look|see|attempt|investigate|search|test)|"
    r"let\s+me.{0,40}(again|another|different|instead)|"
    r"(trying|attempt(ing)?)\s+(a\s+)?(different|another|alternative)",
    re.IGNORECASE,
)

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
    # "creating a container named Kytest3", "spinning up a VM called web1"
    (re.compile(
        r"\b(?:creat\w*|launch\w*|spin\w*\s*up|mak\w*)\b[^.?!]*?"
        r"\b(?:container|ct|vm|instance)\b[^.?!]*?"
        r"\b(?:named|called)\s+[`'\"]?([A-Za-z0-9][\w.-]{0,62})",
        re.IGNORECASE), "lxc.create", "name"),
    # "searching the web for the latest gemma release", "let me look that up — for X"
    (re.compile(
        r"\b(?:search\w*|look\w*\s*up|googl\w*)\b[^.?!]*?\bfor\s+[`'\"]?(.+?)[`'\"]?\s*[.?!]*\s*$",
        re.IGNORECASE), "search.web", "query"),
]

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
            if cfg.DEBUG:
                print(f"[recover] narrated intent: {tool_name}({arg_name}={value!r})")
            return {"function": {"name": tool_name, "arguments": {arg_name: value}}}
    return None

def _try_recover_tool_call(content: str, known_tools: set[str]) -> dict | None:
    """
    Attempt to extract a tool call from plain-text content the model emitted
    when Ollama failed to parse it as a structured tool_call.

    Three recovery strategies:
      1. Broken JSON — the model emitted {"name": "...", "arguments": {...}} as text
      2. Narrated intent — natural phrasing ("creating a container named X"),
         matched against a deterministic map that also extracts the argument
      3. Narrated call — the model wrote "Running `pc.deep_scan` now" instead of
         calling it (tool named verbatim, fired with empty args)

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
        idx = clean.find(tool_name)
        if idx == -1:
            continue
        # Question guard: only skip when the "?" is in the SAME sentence as
        # the tool name. "Want me to run X?" is a question to the user;
        # "Running X now. Anything else?" is a narrated call we should fire.
        tail = clean[idx + len(tool_name):]
        q_pos = tail.find("?")
        if q_pos != -1:
            sentence_end = re.search(r"[.!\n]", tail)
            if sentence_end is None or q_pos < sentence_end.start():
                continue
        if not _NARRATED_VERB_RE.search(clean[max(0, idx - 40):idx]):
            continue
        if cfg.DEBUG:
            print(f"[recover] narrated tool call: {tool_name}")
        return {"function": {"name": tool_name, "arguments": {}}}
    return None


class TurnEngine:
    """The shared tool-calling mechanism. One per Brain; the crew drives the
    same instance for its specialists."""

    def __init__(self, host: "Brain") -> None:
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
                if wanted in self.ollama.installed_models():
                    resolved = wanted
                elif cfg.DEBUG:
                    print(f"[tool model] {wanted} not installed — "
                          f"rounds fall back to {self.model}")
            except Exception:
                pass  # Ollama unreachable — fall back; apply_tool_level re-checks
        self._tool_model = resolved
        self._tool_model_resolved = True
        return resolved, resolved is not None

    def _chat(self, messages: list[dict], tools: list[dict] | None = None,
              think: bool = False, temperature: float | None = None) -> dict:
        """Non-streaming chat on the active brain, with fail→local fallback.

        If a cloud brain raises (dead key, offline, rate-limited), retry once on
        the local chat model so a turn never bricks. Local failures propagate.
        """
        temp = self._final_temp if temperature is None else temperature
        client = self._chat_client
        try:
            return client.chat(messages, tools=tools, model=self._chat_model,
                               think=think, temperature=temp)
        except Exception as e:
            if client is self.ollama:
                raise
            if cfg.DEBUG:
                print(f"[brain] cloud chat failed → local fallback: {e}")
            return self.ollama.chat(messages, tools=tools, model=CHAT_MODEL,
                                    think=True, temperature=temp)

    def _chat_stream(self, messages: list[dict], think: bool, temperature: float):
        """Stream the active brain's reply. A cloud brain that fails BEFORE any
        token falls back to the local chat model; a mid-stream failure keeps what
        was produced and stops cleanly. Yields (token, done, meta) like Ollama."""
        client = self._chat_client
        if client is self.ollama:
            yield from self.ollama.chat_stream(messages, tools=None, model=self._chat_model,
                                               think=think, temperature=temperature)
            return
        emitted = False
        try:
            for chunk in client.chat_stream(messages, tools=None, model=self._chat_model,
                                            think=think, temperature=temperature):
                emitted = emitted or bool(chunk[0])
                yield chunk
        except Exception as e:
            if emitted:
                if cfg.DEBUG:
                    print(f"[brain] cloud stream broke mid-answer: {e}")
                return
            if cfg.DEBUG:
                print(f"[brain] cloud stream failed pre-token → local: {e}")
            yield from self.ollama.chat_stream(messages, tools=None, model=CHAT_MODEL,
                                               think=True, temperature=temperature)

    def _run_fast_path(
        self, tool_name: str, messages: list[dict], tools_used: list[str], *,
        args: dict | None = None,
        query_emb: list[float] | None, user_input: str, trace_id: str,
        on_status: "Callable[[str], None] | None",
    ) -> "Generator[tuple[str, bool, dict], None, None]":
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
            tool_calls, messages, tools_used, [], 0,
            query_emb=query_emb, user_input=user_input, trace_id=trace_id,
            on_status=on_status, sig_of=_sig_of,
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
        on_status: "Callable[[str], None] | None",
        keep_prose: bool = False,
        model_override: str | None = None,
    ) -> Generator[tuple[str, bool, dict], None, "tuple[str, str] | None"]:
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
        tax) or on an opt-in granite tool model. Neither thinks: granite emits
        structured calls natively, and the chat model's occasional "narrate
        instead of call" slip is caught by the pre-LLM fast-paths and the
        narrated-intent recovery net (_match_narrated_intent /
        _try_recover_tool_call) — so we keep tool-calling reliable without paying
        the per-round reasoning latency that used to band-aid the 06-09 regression.

        Returns (raw_content, clean_text) when the chat model answers
        directly without calling tools — the caller finalizes the turn with
        that text. Returns None when the turn should continue to the streamed
        final answer (tools ran, the confirm gate fired, a safety stop hit,
        or rounds were exhausted).
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
        _dup_count = 0              # repeats of an already-executed identical call
        for round_num in range(MAX_TOOL_ROUNDS):
            if not tools_schema:
                break  # no tools → skip to streaming final answer
            if self._cancel.is_set():
                break  # user hit Stop — don't fire any more tool calls

            # Granite tool model stays local; the "off" level runs rounds on the
            # active chat brain (possibly cloud) via _chat, with fail→local.
            if tool_model is None:
                resp = self._chat(messages, tools=tools_schema,
                                  think=rounds_think, temperature=cfg.TEMPERATURE_TOOL)
            else:
                # Opt-in granite tool model is secondary — unload it right after
                # so it never holds a second runner beside the warm chat model.
                resp = self.ollama.chat(
                    messages, tools=tools_schema, model=tool_model,
                    think=rounds_think, keep_alive=0,
                )
            msg = resp.get("message", {})
            try:
                _tc_names = [tc.get("function", {}).get("name", "?")
                             for tc in msg.get("tool_calls") or []]
            except Exception:
                _tc_names = ["?"]
            flow_rec.record(trace_id, "round", n=round_num, model=rounds_model,
                            think=rounds_think, messages=len(messages),
                            content=msg.get("content", ""),
                            thinking=msg.get("thinking", ""),
                            tool_calls=", ".join(_tc_names) or "none")

            # Emit tool-round thinking as a step to be shown inline before the
            # tool label in the activity log — not as a floating reasoning dropdown.
            tool_round_thinking = msg.get("thinking", "")
            if tool_round_thinking:
                if self.session_id:
                    events.emit(events.EVENT_THINK, self.session_id,
                                text=tool_round_thinking, round=round_num)
                yield "", False, {"think_step": True, "text": tool_round_thinking}

            if cfg.DEBUG:
                print(f"\n[{trace_id}] tool round={round_num} "
                      f"tool_calls={bool(msg.get('tool_calls'))}")

            if not msg.get("tool_calls"):
                content = msg.get("content", "")

                # ── JSON repair: recover broken tool calls from plain text ────
                # Small models sometimes emit tool calls as text instead of
                # structured JSON.  Try to salvage before giving up on the round.
                if self.tool_registry and content:
                    known = set(self.tool_registry.list_tools())
                    recovered = _try_recover_tool_call(content, known)
                    if recovered:
                        if cfg.DEBUG:
                            print(f"[{trace_id}] recovered broken tool call: "
                                  f"{recovered['function']['name']}")
                        flow_rec.record(trace_id, "recovered_call",
                                        name=recovered["function"]["name"])
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
                        messages.append({
                            "role": "user",
                            "content": (
                                "Go ahead — use a different tool or call search.web "
                                "to find the information another way."
                            ),
                        })
                        continue

                    # Model chose not to use tools — hand the answer back so
                    # run_stream finalizes it (single end-of-turn path).
                    return content, final

            # Execute tool calls
            messages.append({
                "role": "assistant",
                "content": msg.get("content", ""),
                "tool_calls": msg["tool_calls"],
            })
            outcome = yield from self._execute_tool_calls(
                msg["tool_calls"], messages, tools_used, _call_sigs, _dup_count,
                query_emb=query_emb, user_input=user_input, trace_id=trace_id,
                on_status=on_status, sig_of=_sig_of,
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
                messages, tools_used, trace_id,
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
        self, messages: list[dict], tools_used: list[str], trace_id: str, *,
        any_tool_error: bool, any_win_error_code: bool, escalated: bool,
    ) -> "tuple[list[dict] | None, bool]":
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
                messages.append({
                    "role": "user",
                    "content": (
                        "One or more tools failed. All available tools are now provided — "
                        "pick a different one to complete the task. Do not give up."
                    ),
                })
                return self.tool_registry.get_schema(), True
            messages.append({
                "role": "user",
                "content": (
                    "Tools continue to fail. If no suitable tool exists, "
                    "answer from what you know and explain what blocked you."
                ),
            })
            return None, escalated

        if any_win_error_code and "search.web" not in tools_used:
            # A tool ran but returned a Windows error code (e.g. 0x80240032).
            # search.web is already in the tool schema — direct the model to use it.
            messages.append({
                "role": "user",
                "content": (
                    "A tool returned a Windows error code. "
                    "Call search.web to look up the exact error code and find the cause and fix."
                ),
            })
        return None, escalated

    def _execute_tool_calls(
        self, tool_calls: list[dict], messages: list[dict], tools_used: list[str],
        call_sigs: list[str], dup_count: int, *,
        query_emb: list[float] | None, user_input: str, trace_id: str,
        on_status: "Callable[[str], None] | None", sig_of,
    ) -> "Generator[tuple[str, bool, dict], None, _ToolBatchOutcome]":
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
            tools_used.append(tool_name)

            # Duplicate-call guard: small tool models sometimes re-issue the exact
            # same call instead of writing a final answer. The result is already
            # in the conversation — don't run it again, and after a second repeat
            # stop the rounds so the final answer uses the data already gathered.
            sig = sig_of(tool_name, tool_args)
            if sig in call_sigs:
                dup_count += 1
                flow_rec.record(trace_id, "duplicate_call", name=tool_name)
                messages.append({"role": "tool", "content": json.dumps({
                    "output": ("This exact call already ran this turn — "
                               "its result is above. Answer from it."),
                    "success": True,
                })})
                if dup_count >= 2:
                    rounds_done = True
                    break
                continue

            # ── Confirm gate: pause and ask the user before running ──────────
            if tool_name in _CONFIRM_TOOLS:
                print(f"[confirm] intercepted {tool_name} — waiting for user OK")
                self._pending_confirm = {
                    "name": tool_name,
                    "args": tool_args,
                    "trace_id": trace_id,
                    "label": _TOOL_LABELS.get(tool_name, tool_name),
                }
                result = {
                    "output": (
                        f"⏸ {tool_name} requires your OK before running. "
                        "Describe what you want to do and ask the user to confirm."
                    ),
                    "success": True,
                }
                messages.append({"role": "tool", "content": json.dumps(result)})
                confirm_event = {
                    "confirm_tool": True,
                    "name": tool_name,
                    "label": self._pending_confirm["label"],
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
            if self._cerebellum_pre(tool_name, tool_args, query_emb,
                                    call_sigs, messages, trace_id):
                chain_stopped = True
                break

            if on_status:
                on_status(_TOOL_LABELS.get(tool_name, tool_name))
            result = self._execute_tool_traced(tool_name, tool_args, trace_id)
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
            confirm_intercepted, chain_stopped, rounds_done,
            tool_error, win_error_code, dup_count,
        )

    def _cerebellum_pre(self, tool_name: str, tool_args: dict,
                        query_emb: list[float] | None, call_sigs: list[str],
                        messages: list[dict], trace_id: str) -> bool:
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
            messages.append({"role": "tool", "content": json.dumps({
                "output": f"[Cerebellum] Chain stopped: {pre.reason}",
                "success": False,
            })})
            messages.append({
                "role": "user",
                "content": (
                    f"A safety check stopped the chain: {pre.reason}. "
                    "Explain what happened and what you would need to continue."
                ),
            })
            flow_rec.record(trace_id, "safety_stop", reason=pre.reason)
            return True
        if pre.verdict == _cb.Verdict.FLAG:
            messages.append({
                "role": "system",
                "content": f"[Cerebellum] Pre-check warning: {pre.reason}. Proceed carefully.",
            })
        return False

    def _cerebellum_post(self, tool_name: str, result: dict,
                         query_emb: list[float] | None, messages: list[dict]) -> None:
        """Cerebellum post-check for one tool result. Appends a FLAG warning in place."""
        if not (cfg.CEREBELLUM_ENABLED and query_emb):
            return
        from kai.memory import cerebellum as _cb
        post = _cb.post_check(tool_name, result, query_emb)
        _cb.log_result(tool_name, "post", post, self.user_id,
                       output_snippet=str(result.get("output", ""))[:500])
        if post.verdict == _cb.Verdict.FLAG:
            messages.append({
                "role": "system",
                "content": f"[Cerebellum] Post-check warning: {post.reason}",
            })

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
            return True, False
        if re.search(r"\b0x[0-9a-fA-F]{4,}\b", result.get("output", "")):
            return False, True
        return False, False

    def _execute_tool_traced(self, tool_name: str, tool_args: dict, trace_id: str) -> dict:
        """Execute one tool wrapped in TOOL_START/TOOL_END events + debug log."""
        if self.session_id:
            events.emit(events.EVENT_TOOL_START, self.session_id,
                        name=tool_name, args=tool_args)
        _tool_t0 = time.monotonic()
        result = self._execute_tool(tool_name, tool_args, trace_id)
        _tool_dur = round(time.monotonic() - _tool_t0, 3)
        if self.session_id:
            # Truncate output for the event bus (keeps SQLite lean)
            _evt_output = str(result.get("output", ""))[:2000]
            events.emit(events.EVENT_TOOL_END, self.session_id,
                        name=tool_name, duration=_tool_dur,
                        success=result.get("success", False),
                        error=result.get("error"),
                        output=_evt_output,
                        args=tool_args)
        if cfg.DEBUG:
            # Truncate tool output in debug logs to avoid leaking large
            # data blobs (file contents, search results) to the terminal
            _dbg = str(result)
            if len(_dbg) > 300:
                _dbg = _dbg[:300] + "… [truncated]"
            print(f"[{trace_id}] TOOL: {tool_name} → {_dbg}")
        flow_rec.record(trace_id, "tool", name=tool_name, args=json.dumps(tool_args),
                        success=result.get("success"), error=result.get("error"),
                        output=str(result.get("output", "")))
        return result

    def _execute_tool(self, name: str, args: dict, trace_id: str) -> dict:
        # Skill delegation — tool names starting with "skill." route to the skill registry
        if name.startswith("skill.") and self.skill_registry:
            skill_name = name.removeprefix("skill.")
            # Validate skill name — reject anything that isn't a safe identifier
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", skill_name):
                return {"success": False, "error": f"Invalid skill name: {skill_name!r}"}
            if cfg.DEBUG:
                print(f"[brain] skill delegation: {name!r} → skill {skill_name!r}")
            return self.run_skill(skill_name, args)

        if not self.tool_registry:
            return {"success": False, "error": f"No tool registry — cannot run '{name}'"}
        # ── Enablement gate ──────────────────────────────────────────────────
        # A tool the user turned off in Settings must never run — even if the
        # model names it directly, through a learned alias, or via a crew
        # specialist whose slice still listed it. The schema filter hides
        # disabled tools from the model; this is the authoritative block.
        disabled = self.disabled_tools
        if disabled and self.tool_registry.resolve_name(name) in disabled:
            return {"success": False,
                    "error": f"'{name}' is turned off in Settings and was not run."}
        # Set thread-local user_id (per-user DB scoping) and session_id (so
        # memory tools can exclude the live conversation from "past sessions").
        from kai.core._app_state import set_current_user_id, set_current_session_id
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
                    return {"success": False,
                            "error": f"'{target}' is turned off in Settings and was not run."}
                if cfg.DEBUG:
                    print(f"[brain] alias redirect: {name!r} → {target!r}")
                try:
                    output = self.tool_registry.execute(target, args)
                    return {"success": True, "output": output}
                except Exception as e:
                    return {"success": False, "error": str(e)}
            return {"success": False, "error": f"Unknown tool: {name!r} — no similar tool found"}
        except Exception as e:
            return {"success": False, "error": str(e)}
