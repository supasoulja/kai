"""
The Brain — Ollama HTTP client + ReAct conversation loop.

Flow per turn:
  1. Build context block from memory (identity + procedural + semantic + episodic)
  2. Stream final response token by token (feels instant, same as ollama run)
  3. For tool calls: non-streaming round trip, then stream the final answer
  4. Strip <think> tags (log in debug mode)
  5. Commit turn to memory
"""
import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import TYPE_CHECKING, Any

import kai.config as cfg
from kai.config import (
    CHAT_MODEL,
    TEMPERATURE_REASON, HISTORY_CHAR_LIMIT, HISTORY_COMPRESS_KEEP, LEARN_FROM_CONVERSATION,
)
from kai.llm.ollama import OllamaClient
from kai.memory.manager import MemoryManager
from kai.util.text import strip_thinking as _strip_thinking
from kai.core.tool_gate import _TOOL_SIGNALS, _query_needs_thinking, _query_needs_tools
from kai.core import trace as trace_log
from kai.core import flow as flow_rec
from kai.store import sessions
from kai.core import events

if TYPE_CHECKING:
    from kai.tools.registry import ToolRegistry

MAX_TOOL_ROUNDS   = 8   # increased to support multi-step tasks (scan → restore point → fix)
_HISTORY_HARD_CAP = 60  # safety ceiling — compression normally keeps history much smaller
_FACT_EXTRACT_THRESHOLD = 2  # two-phase fact extraction fires when ≥ this many tools were called

# Last-resort placeholders the model emits nothing real for. They must never go
# back into the conversation — otherwise the model sees them in history and
# parrots them, turning one empty turn into a "[no response]" death spiral.
_FAILURE_MARKERS = {"[no response]", "[stopped]"}

# Tools that need user confirmation before execution.
# When the model calls one of these, execution is paused and a confirm button
# is shown to the user. The tool runs only after they click "Go ahead."
#
# The set is derived from the registry's risk tiers (the "destructive" tier) —
# the single source of truth — so classifying a new tool there automatically
# gates it here. confirm_tool_names() reads a static table, so importing it at
# module load is safe regardless of tool-registration order.
from kai.tools.registry import confirm_tool_names as _confirm_tool_names
_CONFIRM_TOOLS = _confirm_tool_names()

_CONFIRM_RE = re.compile(
    r"^(go\s*ahead|ye[spa]h?|yup|ok(ay)?|sure|do\s*it|confirm(ed)?|"
    r"run\s*it|scan|proceed|go\s*for\s*it|let'?s?\s*go|approved?|"
    r"y|bet|send\s*it|mhm|uh\s*huh|absolutely|please|pls|def(initely)?|"
    r"fo\s*sho|for\s*sure|aight|alright|right|dew\s*it|hit\s*it)\s*[.!]?\s*$",
    re.IGNORECASE,
)

# Tool gate (does this turn need tools / thinking) lives in kai/tool_gate.py.
# Imported at the top of this module.

# Detects when Kai herself signals she wants to retry — e.g. "Let me try a different approach"
# When this fires after a failed tool call, the loop gives her one more round automatically.
_KAI_RETRY_SIGNALS = re.compile(
    r"let\s+me\s+(try|check|look|see|investigate|figure|attempt|search|test)|"
    r"i('ll| will)\s+(try|check|look|see|attempt|investigate|search|test)|"
    r"let\s+me.{0,40}(again|another|different|instead)|"
    r"(trying|attempt(ing)?)\s+(a\s+)?(different|another|alternative)",
    re.IGNORECASE,
)

# ── Broken tool-call recovery ────────────────────────────────────────────────
# Small/quantized models sometimes emit tool calls as plain text instead of
# structured JSON that Ollama can parse.  Detect patterns like:
#   {"name": "weather.current", "arguments": {"city": "NYC"}}
#   tool_call: weather.current(city="NYC")
#   <tool_call>weather.current</tool_call>
# and try to salvage the call rather than losing the round.
_BROKEN_TOOL_CALL_RE = re.compile(
    r'"name"\s*:\s*"([a-z][a-z0-9_.]+)".*?"arguments"\s*:\s*(\{[^}]*\})',
    re.DOTALL,
)

# Action verbs that signal the model intended to CALL the tool it just named.
# Used by narrated-call recovery so "use system.info" fires but a passing
# mention ("system.info showed nothing earlier") does not.
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


# Deterministic narrated-intent map (the user's "phrase → tool" fast-path).
# Generic narrated recovery (Strategy 2 below) only fires when the tool's dotted
# NAME appears verbatim in the prose — but the model usually narrates in plain
# language ("I'm creating a container named Kytest3 now"), where the name
# "lxc.create" never appears, and even a match would fire with empty args while
# the tool requires one. These patterns recognise the natural phrasing AND pull
# the argument out of the same sentence, so the promised action actually runs.
#
# Scoped to NON-destructive intents on purpose: auto-firing a delete/stop off a
# loose prose match could tear down the wrong instance. Destructive actions stay
# behind the confirm gate. Each entry: (regex with one capture group, tool, arg).
_NARRATED_INTENTS: list[tuple[re.Pattern, str, str]] = [
    # "creating a container named Kytest3", "spinning up a VM called web1"
    (re.compile(
        r"\b(?:creat\w*|launch\w*|spin\w*\s*up|mak\w*)\b[^.?!]*?"
        r"\b(?:container|ct|vm|instance)\b[^.?!]*?"
        r"\b(?:named|called)\s+[`'\"]?([A-Za-z0-9][\w.-]{0,62})",
        re.IGNORECASE), "lxc.create", "name"),
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


# Shared compression prompt — used by _maybe_compress_history, flush_history_snapshot,
# and web.py _archive_pending_turns. Defined once to avoid drift.
COMPRESS_PROMPT = (
    "Compress this conversation into a single concise paragraph. "
    "Preserve: facts shared, decisions made, topics discussed, preferences stated. "
    "Write in past tense. Be specific. No filler."
)

LEARN_PROMPT = (
    "Review this conversation exchange. Extract any NEW KNOWLEDGE you learned — "
    "corrections, cultural references, facts, personal details, inside jokes, or anything "
    "worth remembering for future conversations.\n"
    "Each fact on its own line, concise and specific.\n"
    "If nothing new was learned, respond with exactly: NONE"
)

# Always-present grounding calibration — appended to every turn's context so
# the rule sits at the decision boundary where the model generates. Defined
# once; shared by the normal turn path and the confirmed-tool path.
GROUNDING_RULE = (
    "\n\n[CRITICAL GROUNDING RULE]\n"
    "Before stating ANY fact about this system, ask: did a tool result "
    "or [SEMANTIC] entry provide this data in THIS conversation? "
    "If not — call a tool first, or say \"I'd need to check that.\" "
    "Never fabricate numbers, outputs, or success messages."
)

# Citation fence injected after tool calls — keeps the final answer pinned to
# what the tools actually returned. Callers append the evidence text to it.
EVIDENCE_PROMPT = (
    "GROUNDING: Respond using ONLY data from tool results above. "
    "Quote exact values. If you lack data the user needs, say so.\n\n"
    "KEY EVIDENCE:\n"
)


# Friendly labels for tool status messages shown in the web UI now live with the
# tools, in the registry — the single source of truth (see kai/tools/registry.py).
from kai.tools.registry import TOOL_LABELS as _TOOL_LABELS


# OllamaClient (the Ollama HTTP wrapper) now lives in kai/ollama.py — imported
# at the top of this module.


# ── Helpers ────────────────────────────────────────────────────────────────────

def _build_compress_messages(raw_text: str) -> list[dict]:
    """Build the messages list for a compression call. Single source of truth."""
    return [{"role": "user", "content": f"{COMPRESS_PROMPT}\n\n{raw_text}\n\nSummary:"}]


# ── Brain ──────────────────────────────────────────────────────────────────────

class Brain:
    def __init__(
        self,
        memory: MemoryManager,
        tool_registry: "ToolRegistry | None" = None,
        model: str = CHAT_MODEL,
        ollama: OllamaClient | None = None,
        think: bool = False,
        user_id: int = 0,
        skill_registry: "Any | None" = None,
    ):
        self.memory = memory
        self.tool_registry = tool_registry
        self.model = model
        self.ollama = ollama or OllamaClient()
        self._think = think
        self._final_temp: float = cfg.TEMPERATURE_FINAL  # per-session final-answer temp
        self._cancel = threading.Event()  # set by request_stop() to abort the current turn
        self.user_id = user_id
        self.skill_registry = skill_registry          # kai.skills.SkillRegistry (optional)
        self._session_history: list[dict] = []  # rolling conversation turns for this session
        self._history_lock = threading.Lock()    # protects _session_history mutations
        self.session_id: str | None = None       # current persisted session UUID
        self._turn_order: int = 0                # monotonic counter for message ordering
        self._tool_index: dict[str, list[float]] = {}  # name → embedding vector, built lazily
        self._tool_index_ready: bool = False
        self._memory_router_ready: bool = False       # memory domain index built lazily
        self._handoff_router_ready: bool = False      # handoff pattern index seeded lazily
        from kai.memory.knowledge import HandoffRouter
        self._handoff_router = HandoffRouter()
        self._compressing: bool = False               # prevents concurrent history compressions
        self._turn_count: int = 0                     # monotonic counter for learn-rate gating
        self._pending_confirm: dict | None = None     # tool call awaiting user confirmation
        self._tool_level: str = cfg.DEFAULT_TOOL_LEVEL  # which model runs tool rounds
        self._tool_model: str | None = None             # resolved lazily, availability-checked
        self._tool_model_resolved: bool = False
        self._bg_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="kai-bg")

        # Active chat brain — defaults to local Ollama + CHAT_MODEL. set_active_brain()
        # can point the chat role at a connected cloud brain; _chat/_chat_stream wrap
        # the call with a fail→local fallback. Tool-round granite always stays local.
        self._chat_client = self.ollama
        self._chat_model = self.model

        # Sync this user's tool-doc tree nodes (tools/<namespace>/<tool_name>) once
        # per process — idempotent upsert keeps the docs current with the registry.
        # render_tool_index() reads these every turn via context.build() to emit the
        # [TOOLS] block. Failures are swallowed inside; never blocks construction.
        if self.tool_registry:
            from kai.memory.tool_docs import ensure_tool_docs_synced
            ensure_tool_docs_synced(self.user_id)

        # Re-apply a persisted cloud brain selection (best-effort; no-op if local).
        self._restore_active_brain()

    def apply_preset(self, key: str, custom_temps: dict[str, float] | None = None) -> dict:
        """Apply a generation preset — sets think mode + final-answer temperature.

        `custom_temps` optionally overrides a preset's default temperature with the
        user's saved Advanced value (keyed by preset name). Returns the resolved
        {key, label, think, temp}.
        """
        preset = cfg.GEN_PRESETS.get(key)
        if not preset:
            raise ValueError(f"Unknown preset: {key!r}")
        temp = preset["temp"]
        if custom_temps and key in custom_temps:
            temp = custom_temps[key]
        self._think = bool(preset["think"])
        self._final_temp = max(cfg.TEMP_MIN, min(cfg.TEMP_MAX, float(temp)))
        return {"key": key, "label": preset["label"],
                "think": self._think, "temp": self._final_temp}

    def set_temperature(self, temp: float) -> float:
        """Override the final-answer temperature for this session (slider). Clamped."""
        self._final_temp = max(cfg.TEMP_MIN, min(cfg.TEMP_MAX, float(temp)))
        return self._final_temp

    def apply_tool_level(self, key: str) -> dict:
        """Select which model runs tool-call rounds. Mirrors apply_preset.

        An unavailable model (not pulled in Ollama) falls back to running the
        rounds on the chat model with thinking forced on — same as the "off"
        level — so selecting any level never breaks anything.
        Returns the resolved {key, label, model, available}.
        """
        level = cfg.TOOL_MODEL_LEVELS.get(key)
        if not level:
            raise ValueError(f"Unknown tool level: {key!r}")
        self._tool_level = key
        self._tool_model_resolved = False  # re-check availability on next use
        model, available = self._resolve_tool_model()
        return {"key": key, "label": level["label"],
                "model": level["model"], "available": available}

    def _resolve_tool_model(self) -> tuple[str | None, bool]:
        """Resolve the active tool level to an installed model (cached).

        Returns (model, available). model=None means: run the rounds on the
        chat model with thinking on (the "off" level / fallback behavior).
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
                          f"rounds fall back to {self.model} with thinking on")
            except Exception:
                pass  # Ollama unreachable — fall back; apply_tool_level re-checks
        self._tool_model = resolved
        self._tool_model_resolved = True
        return resolved, resolved is not None

    # ── Active chat brain (local Ollama or a connected cloud brain) ───────────

    def set_active_brain(self, entry: dict, persist: bool = True) -> dict:
        """Point the chat role at a model-registry entry — local or cloud.

        A cloud entry resolves its client + API key via the registry/keystore;
        a missing key raises LLMKeyMissing (the caller decides what to do).
        Tool-round granite is unaffected — only the chat role moves.
        """
        provider = entry.get("provider", "ollama")
        model_id = entry.get("ollama_id") or self.model
        if provider == "ollama":
            self._chat_client = self.ollama
        else:
            from kai.llm.resolve import resolve_client
            self._chat_client = resolve_client(entry, self.user_id)
        self._chat_model = model_id
        self.model = model_id
        if "think" in entry:
            self._think = bool(entry["think"])
        if persist:
            try:
                self.memory.set_fact("active_brain", entry.get("name", ""), source="user_setting")
            except Exception:
                pass
        return {"name": entry.get("name", ""), "provider": provider, "model": model_id}

    def _restore_active_brain(self) -> None:
        """Re-apply the persisted active brain on construction (best-effort).

        Only cloud entries need restoring — local is already the default. A
        missing key or any failure leaves the chat role on local Ollama.
        """
        try:
            name = self.memory.get_fact("active_brain")
            if not name:
                return
            from kai.llm import models as _models
            entry = _models.get_model(name)
            if entry and entry.get("provider", "ollama") != "ollama":
                self.set_active_brain(entry, persist=False)
        except Exception:
            if cfg.DEBUG:
                import traceback
                traceback.print_exc()

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

    def request_stop(self) -> None:
        """Signal the in-flight turn to stop — breaks tool-round and streaming loops.
        Whatever has been generated so far is kept and finalized."""
        self._cancel.set()

    def generate_greeting(self, fresh: bool = False) -> Generator[tuple[str, bool, dict], None, None]:
        """Stream Kai's own opening line so she starts the conversation.

        fresh=False (cold open): may use her welcome-back note + memory to pick up
            where things left off; consumes the welcome-back note.
        fresh=True (new chat): a brief clean-start greeting that ignores the note.

        Yields (token, done, meta) like run_stream. The greeting is kept in the
        in-session history (so the model has continuity) but is not persisted as a
        standalone DB turn — the session is created normally on the user's first reply.
        """
        context = self.memory.render_context(
            query="", include_welcome_back=not fresh,
        )
        if fresh:
            directive = (
                "[The user just started a fresh chat. Open the conversation yourself "
                "with a short, warm greeting in your own voice — a clean slate. One "
                "sentence. Don't reference past sessions, don't list options, and don't "
                "ask 'how can I help'.]"
            )
        else:
            directive = (
                "[The user just opened Kai. Start the conversation yourself with a short, "
                "natural greeting in your own voice. If you left yourself a welcome-back "
                "note, use it to pick up where things left off — work it in naturally, "
                "don't quote it. One or two sentences. Don't list options or ask "
                "'how can I help'.]"
            )
        messages = [
            {"role": "system", "content": context},
            {"role": "user",   "content": directive},
        ]
        # A Stop left over from a previous turn must not kill the greeting —
        # this is a fresh generation, so clear the flag like run_stream does.
        self._cancel.clear()
        full_text, _ = yield from self._stream_answer(
            messages, think=False, forward_thinking=False,
        )
        _, clean = _strip_thinking(full_text)
        clean = clean.strip()
        if clean:
            with self._history_lock:
                self._session_history.append({"role": "assistant", "content": clean})
        # A cold-open greeting is how the welcome-back note gets delivered — clear it.
        if not fresh:
            self._mark_session_notes_delivered()
        yield "", True, {}

    def drain(self) -> None:
        """Wait for all queued background work to finish, then close the pool.

        Called by the graceful-shutdown path: every pending _post_turn /
        archive_history job must complete so the entries they produce exist
        before the end-of-session HQ re-embed runs. Unlike shutdown(), this
        does NOT cancel — it blocks until the pool drains.
        """
        self._bg_pool.shutdown(wait=True)

    def shutdown(self) -> None:
        """Hard-stop the background thread pool, cancelling pending work.

        The emergency / abrupt-exit path. Prefer drain() for clean shutdowns —
        it lets in-flight memory and embedding work finish first.
        """
        self._bg_pool.shutdown(wait=False, cancel_futures=True)

    def _skill_schemas(self) -> list[dict]:
        """Build Ollama-compatible tool schemas for registered skills."""
        if not self.skill_registry:
            return []
        schemas = []
        for info in self.skill_registry.list_skills():
            schemas.append({
                "type": "function",
                "function": {
                    "name": f"skill.{info['name']}",
                    "description": info["description"],
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            })
        return schemas

    def run_skill(self, name: str, args: dict | None = None) -> dict:
        """
        Execute a skill by name. Returns a tool-result-shaped dict
        so it can slot into tool-call rounds seamlessly.
        """
        if not self.skill_registry:
            return {"success": False, "error": "No skill registry configured"}
        try:
            result = self.skill_registry.run(name, args or {})
            return {
                "success": result.success,
                "output": result.output,
                "tool_calls": result.tool_calls,
            }
        except KeyError:
            return {"success": False, "error": f"Unknown skill: {name!r}"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def clear_history(self) -> None:
        """Clear in-memory conversation history (call on /clear)."""
        with self._history_lock:
            self._session_history.clear()
        self.session_id  = None
        self._turn_order = 0
        self._turn_count = 0

    def snapshot_history(self) -> list[dict]:
        """Thread-safe snapshot of current history for archiving."""
        with self._history_lock:
            return list(self._session_history)

    def load_session(self, session_id: str, messages: list[dict]) -> int:
        """Replace in-memory history with a saved session. Returns message count."""
        with self._history_lock:
            self._session_history = [
                {"role": m["role"], "content": m["content"]} for m in messages
            ]
        self.session_id  = session_id
        self._turn_order = len(messages)
        return len(messages)

    def run(self, user_input: str, trace_id: str | None = None) -> str:
        """
        Non-streaming turn. Used by tests.
        Returns the complete response string.
        """
        _tokens: list[str] = []
        for token, done, _ in self.run_stream(user_input, trace_id):
            if not done:
                _tokens.append(token)
        full_text = "".join(_tokens)
        _, clean = _strip_thinking(full_text)
        return clean

    def run_stream(
        self,
        user_input: str,
        trace_id: str | None = None,
        on_status: "Callable[[str], None] | None" = None,
    ) -> Generator[tuple[str, bool, dict], None, None]:
        """
        Streaming turn. Yields (token, done, {}) until done=True.
        The CLI iterates this and prints tokens as they arrive.
        Tool calls are handled internally (non-streaming) before the
        final answer is streamed.
        """
        trace_id  = trace_id or str(uuid.uuid4())[:8]
        turn_start = time.monotonic()
        tools_used: list[str] = []
        self._cancel.clear()   # fresh turn — clear any prior stop request

        # Bind this turn's user/session to the thread up front so flow records
        # (route, rounds, etc.) are user-scoped — not just calls made after the
        # first tool dispatch, which re-sets these in _execute_tool.
        from kai.core._app_state import set_current_user_id, set_current_session_id
        set_current_user_id(self.user_id)
        set_current_session_id(self.session_id)

        # ── Pending tool confirmation ────────────────────────────────────────
        # If a confirm-gated tool is waiting and the user just said "go ahead",
        # execute the tool now and stream the model's presentation of results.
        if self._pending_confirm and _CONFIRM_RE.match(user_input.strip()):
            print(f"[confirm] user confirmed — executing {self._pending_confirm['name']}")
            yield from self._run_confirmed_tool(
                user_input, trace_id, turn_start, on_status,
            )
            return

        # If the user said something other than confirm, clear the pending tool
        if self._pending_confirm:
            print(f"[confirm] user said '{user_input}' — clearing pending {self._pending_confirm['name']}")
            self._pending_confirm = None

        # Thinking is gated by the active preset first (self._think), then
        # auto-think skips it for trivial prompts (greetings, acks, small
        # talk). Previously this was hardcoded True, which made the preset
        # toggle a no-op and spent reasoning time on every tool round.
        use_think = self._think and _query_needs_thinking(user_input)

        self._emit_status("Thinking...", on_status)

        # One fast CPU embed (~5ms) shared by the memory router, tool router,
        # and handoff classifier — then classify the turn.
        query_emb, handoff_mode = self._route_turn(user_input)

        # Reasoning-routed prompts re-enable thinking even when auto-think
        # called them trivial. The preset stays authoritative though: a
        # no-think preset never thinks.
        if handoff_mode == "reasoning" and self._think:
            use_think = True

        context = self._build_turn_context(user_input, query_emb, trace_id)
        with self._history_lock:
            history = list(self._session_history[-_HISTORY_HARD_CAP:])
        # Never replay failure placeholders — otherwise the model mimics them.
        # (Also scrubs any "[no response]" already stored from earlier sessions.)
        history = [m for m in history
                   if not (m.get("role") == "assistant"
                           and m.get("content", "").strip() in _FAILURE_MARKERS)]
        messages: list[dict] = [
            {"role": "system", "content": context},
            *history,
            {"role": "user",   "content": user_input},
        ]
        tools_schema = self._select_tool_schema(user_input, history, query_emb, handoff_mode)

        try:
            _offered = ([t["function"]["name"] for t in tools_schema]
                        if isinstance(tools_schema, list) else [])
        except Exception:
            _offered = ["?"]
        flow_rec.record(trace_id, "route", input=user_input, handoff=handoff_mode,
                        think=use_think, tool_level=self._tool_level,
                        context_chars=len(context),
                        tools_offered=", ".join(_offered) or "none")

        # ── Tool-call rounds (non-streaming) ──────────────────────────────────
        # If the model answers directly in a tool round, the rounds return that
        # answer here and the turn finalizes immediately (single exit path).
        direct = None
        if tools_schema:
            direct = yield from self._run_tool_rounds(
                messages, tools_schema, tools_used,
                query_emb=query_emb, user_input=user_input,
                trace_id=trace_id, on_status=on_status,
            )
        if direct is not None:
            raw_text, clean = direct
            msg_id = self._finalize_turn(
                user_input=user_input, clean_text=clean, raw_text=raw_text,
                trace_id=trace_id, context=context, tools_used=tools_used,
                turn_start=turn_start,
            )
            yield clean, False, {}
            yield "", True, {"message_id": msg_id} if msg_id else {}
            return

        # ── Ground the response in tool evidence ─────────────────────────────
        self._ground_evidence(messages, tools_used, on_status, trace_id=trace_id)

        # ── Stream the final answer ───────────────────────────────────────────
        if tools_used:
            self._emit_status("Responding...", on_status)

        # After tool rounds, disable thinking — the model already reasoned during
        # the tool phase.  Thinking here risks the entire final answer being
        # swallowed into <think> tags, producing "[no response]".
        final_think = use_think and not tools_used

        full_text, had_think = yield from self._stream_answer(
            messages, think=final_think, forward_thinking=True,
        )
        flow_rec.record(trace_id, "final_answer", think=final_think,
                        had_think=had_think, text=full_text)
        _, clean_text = _strip_thinking(full_text)
        if not clean_text and not self._cancel.is_set():
            # Model produced no visible output — thinking swallowed the answer, a
            # tool round left nothing, or the tool model returned no call and the
            # chat model went silent. Retry once with an explicit nudge so the
            # model writes a real reply instead of nothing. (Skipped on Stop.)
            messages.append({"role": "system", "content": (
                "Reply to the user now, directly and in plain text. "
                "Do not call any tools."
            )})
            retry_text, _ = yield from self._stream_answer(
                messages, think=False, forward_thinking=False,
            )
            flow_rec.record(trace_id, "retry_answer", text=retry_text)
            _, clean_text = _strip_thinking(retry_text)
        if not clean_text:
            clean_text = self._fallback_text(tools_used)
            flow_rec.record(trace_id, "fallback", text=clean_text)
            # The fallback was never streamed — without this yield the web UI
            # shows an empty bubble while the DB quietly stores a reply.
            if self.session_id:
                events.emit(events.EVENT_STREAM_TOKEN, self.session_id, token=clean_text)
            yield clean_text, False, {}

        msg_id = self._finalize_turn(
            user_input=user_input, clean_text=clean_text, raw_text=full_text,
            trace_id=trace_id, context=context, tools_used=tools_used,
            turn_start=turn_start,
        )
        yield "", True, {"message_id": msg_id} if msg_id else {}

    # ── Turn phases ──────────────────────────────────────────────────────────
    # run_stream is the orchestrator; each method below is one phase with one
    # job. Phases that stream UI chunks are generators and must be consumed
    # with `yield from`; they hand results back via their return value.

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
    ) -> Generator[tuple[str, bool, dict], None, "tuple[str, str] | None"]:
        """Run up to MAX_TOOL_ROUNDS of non-streaming tool calls.

        Mutates `messages` in place (assistant tool_calls, tool results,
        corrective prompts) and appends every called tool to `tools_used`.

        Rounds run on the selected tool model (granite — emits structured
        calls without reasoning) or fall back to the chat model with thinking
        forced ON: gemma without thinking narrates the call instead of making
        it, then fabricates results (the 06-09 regression).

        Returns (raw_content, clean_text) when the chat model answers
        directly without calling tools — the caller finalizes the turn with
        that text. Returns None when the turn should continue to the streamed
        final answer (tools ran, the confirm gate fired, a safety stop hit,
        or rounds were exhausted).
        """
        from kai.memory.cerebellum import call_signature as _sig_of
        _escalated = False  # True after first tool error → full schema injected
        tool_model, _ = self._resolve_tool_model()
        rounds_model = tool_model or self._chat_model
        rounds_think = tool_model is None  # granite: no think; chat fallback: always think
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
                resp = self.ollama.chat(
                    messages, tools=tools_schema, model=tool_model, think=rounds_think
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
                    if tool_model is not None:
                        # The tool model's only job is picking tools. Prose
                        # from it is not Kai's voice — discard it and let the
                        # chat model write the real reply in the streamed
                        # final answer.
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
            _confirm_intercepted = False
            _chain_stopped = False   # Cerebellum STOP — end the rounds, never escalate
            _rounds_done = False     # duplicate calls piling up — answer with what we have
            any_tool_error = False   # Python exception — tool completely failed
            any_soft_error = False   # Tool ran but output contains a Windows error code
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                tool_name = fn.get("name") or ""
                tool_args = fn.get("arguments", {})
                tools_used.append(tool_name)

                # Duplicate-call guard: small tool models sometimes re-issue
                # the exact same call instead of writing a final answer. The
                # result is already in the conversation — don't run it again,
                # and after a second repeat stop the rounds so the final
                # answer presents the data already gathered.
                _sig = _sig_of(tool_name, tool_args)
                if _sig in _call_sigs:
                    _dup_count += 1
                    flow_rec.record(trace_id, "duplicate_call", name=tool_name)
                    messages.append({"role": "tool", "content": json.dumps({
                        "output": ("This exact call already ran this turn — "
                                   "its result is above. Answer from it."),
                        "success": True,
                    })})
                    if _dup_count >= 2:
                        _rounds_done = True
                        break
                    continue

                # ── Confirm gate: pause and ask the user before running ──────
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
                    _confirm_intercepted = True
                    break  # exit tool loop — wait for user confirmation

                # ── Cerebellum pre-check (may stop the chain) ─────────────────
                if self._cerebellum_pre(tool_name, tool_args, query_emb,
                                        _call_sigs, messages, trace_id):
                    _chain_stopped = True
                    break

                if on_status:
                    on_status(_TOOL_LABELS.get(tool_name, tool_name))
                result = self._execute_tool_traced(tool_name, tool_args, trace_id)
                _call_sigs.append(_sig)

                # ── Pattern tracking ──────────────────────────────────────────
                if cfg.PATTERN_ENABLED:
                    from kai.memory.patterns import log_tool_call as _log_pattern
                    _log_pattern(tool_name, user_id=self.user_id, topic=user_input[:80])

                # ── Cerebellum post-check ─────────────────────────────────────
                self._cerebellum_post(tool_name, result, query_emb, messages)
                messages.append({"role": "tool", "content": json.dumps(result)})

                _hard, _soft = self._classify_tool_result(result)
                any_tool_error = any_tool_error or _hard
                any_soft_error = any_soft_error or _soft

            # Safety stop — end the rounds entirely. Must come before error
            # escalation: a stop that re-arms the full tool set with "do not
            # give up" isn't a stop (that was the bug this replaced).
            if _chain_stopped:
                break

            # The model keeps repeating an already-executed call — it has the
            # data; move on to the final answer.
            if _rounds_done:
                break

            # Confirm-gated tool was intercepted — skip to final answer so the
            # model can ask the user for confirmation.
            if _confirm_intercepted:
                tools_schema = None
                break

            # Error escalation (paper "Less is More", Tier 2 fallback):
            # First failure → give the model the full tool set so it has every alternative.
            # Subsequent failures → push hard and let it exit gracefully if truly stuck.
            if any_tool_error:
                if not _escalated and self.tool_registry:
                    tools_schema = self.tool_registry.get_schema()
                    _escalated = True
                    flow_rec.record(trace_id, "escalation", full_schema=True)
                    messages.append({
                        "role": "user",
                        "content": (
                            "One or more tools failed. All available tools are now provided — "
                            "pick a different one to complete the task. Do not give up."
                        ),
                    })
                else:
                    messages.append({
                        "role": "user",
                        "content": (
                            "Tools continue to fail. If no suitable tool exists, "
                            "answer from what you know and explain what blocked you."
                        ),
                    })
            elif any_soft_error and "search.web" not in tools_used:
                # A tool ran but returned a Windows error code (e.g. 0x80240032).
                # search.web is already in the tool schema — direct the model to use it.
                messages.append({
                    "role": "user",
                    "content": (
                        "A tool returned a Windows error code. "
                        "Call search.web to look up the exact error code and find the cause and fix."
                    ),
                })

        # Rounds exhausted (or confirm gate / Stop broke out) — continue to
        # the streamed final answer.
        return None

    # ── Tool-round helpers ─────────────────────────────────────────────────────

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
        """Return (hard_error, soft_error) for one tool result.

        hard_error: the tool itself crashed (Python exception / no registry).
        soft_error: the tool ran but the system returned a Windows error code
        (the 0x hex pattern — unambiguous, no false positives).
        """
        if not result.get("success", True):
            return True, False
        if re.search(r"\b0x[0-9a-fA-F]{4,}\b", result.get("output", "")):
            return False, True
        return False, False

    def _route_turn(self, user_input: str) -> tuple[list[float] | None, str]:
        """Embed the query once and classify the turn.

        Returns (query_emb, handoff_mode). One fast CPU embed call (~5ms)
        replaces the old Ollama GPU call that caused model swaps and 5-15s
        latency on 8 GB cards. query_emb is None when embedding fails —
        every consumer then falls back to injecting everything.
        """
        self._ensure_memory_router()
        self._ensure_tool_index()
        query_emb: list[float] | None = None
        try:
            from kai.llm.embed import embed as _fast_embed
            query_emb = _fast_embed(user_input)
        except Exception:
            pass

        # Handoff routing: classify as chat / reasoning / tool / researcher.
        # The precomputed vector is passed in so the router doesn't embed the
        # same text a second time.
        handoff_mode = "chat"
        if query_emb:
            try:
                from kai.llm.embed import embed as _fast_embed
                self._ensure_handoff_router()
                handoff_mode, handoff_conf = self._handoff_router.route(
                    user_input, _fast_embed, query_emb=query_emb,
                )
                if cfg.DEBUG:
                    print(f"[handoff] mode={handoff_mode} conf={handoff_conf:.3f}")
            except Exception:
                pass
        return query_emb, handoff_mode

    def _build_turn_context(
        self, user_input: str, query_emb: list[float] | None,
        trace_id: str = "",
    ) -> str:
        """Render the memory context block + learned knowledge + grounding rule."""
        context = self.memory.render_context(
            query=user_input, query_embedding=query_emb
        )
        # Tree memory: [MEMORY CONTEXT] ranked by the Version C equation.
        # Prepended so relationship state + ranked facts sit at the very top.
        tree_block = self._render_tree_context(query_emb, trace_id)
        if tree_block:
            context = tree_block + "\n\n" + context
        # Inject learned knowledge if the store has relevant entries for this query.
        # Runs for all modes — learned knowledge is always useful when it matches.
        try:
            if self.memory.knowledge_count() > 0:
                hits = self.memory.search_knowledge(user_input)
                if hits:
                    lines = [
                        f"[{h['topic'] or 'general'}] {h['content']}"
                        for h in hits
                    ]
                    knowledge_block = (
                        "[LEARNED KNOWLEDGE — from past research]\n"
                        + "\n---\n".join(lines)
                    )
                    context = knowledge_block + "\n\n" + context
        except Exception:
            pass
        return context + GROUNDING_RULE

    def _render_tree_context(self, query_emb: list[float] | None, trace_id: str = "") -> str:
        """One pass of the memory model loop (gather → rank → flag → render).

        Returns the [MEMORY CONTEXT] block: relationship/state summary, tree
        nodes ranked by the Version C scoring equation, and intuition flags.
        Empty string when the tree holds no real facts yet — an empty scaffold
        would just burn context tokens. Failures are silent: the turn must
        never depend on the memory loop working.
        """
        try:
            from kai.memory import tree as _mtree
            uid = str(self.user_id)
            if _mtree.count_facts(uid) == 0:
                return ""
            import numpy as _np
            from kai.memory import loop as _memloop
            q = (_np.asarray(query_emb, dtype=_np.float32)
                 if query_emb else None)
            block, flags = _memloop.gather_context(uid, q)
            flow_rec.record(trace_id, "memory_tree", block=block,
                            flags=len(flags))
            return block
        except Exception:
            if cfg.DEBUG:
                import traceback
                traceback.print_exc()
            return ""

    def _select_tool_schema(
        self,
        user_input: str,
        history: list[dict],
        query_emb: list[float] | None,
        handoff_mode: str,
    ) -> list[dict] | None:
        """Decide whether this turn gets tools, and which ones.

        Selects semantically relevant tools rather than injecting all 40 every
        round. Paper "Less is More": filtering to top-K tools improves selection
        accuracy 30-70% for small/quantized models and halves context size.
        """
        if not self.tool_registry:
            return None
        gate = _query_needs_tools(user_input, history)
        # Optional semantic gate: the handoff router classified this turn from
        # its embedding — when it says tool/researcher, open the gate even if
        # the keyword regex missed. New tools then work without new keywords.
        if not gate and cfg.SEMANTIC_TOOL_GATE and handoff_mode in ("tool", "researcher"):
            if cfg.DEBUG:
                print(f"[tool gate] keyword miss, semantic hit ({handoff_mode})")
            gate = "direct"
        if not gate:
            return None

        # Follow-up turns ("yes", "run that again") carry no meaning of their
        # own — selecting categories from their embedding picks arbitrary
        # tools (this is how "yes" once produced a hallucinated shell tool).
        # Select with the embedding of the message that carried the intent.
        # Prefer USER messages: assistant replies are full of keyword noise
        # (every temps report says "CPU"/"GPU") and embed off-category.
        selection_emb = query_emb
        if gate == "follow_up":
            recent = history[-4:]
            candidates = [m for m in reversed(recent)
                          if m.get("role") == "user"
                          and _TOOL_SIGNALS.search(m.get("content", ""))]
            if not candidates:
                candidates = [m for m in reversed(recent)
                              if _TOOL_SIGNALS.search(m.get("content", ""))]
            if candidates:
                try:
                    from kai.llm.embed import embed as _fast_embed
                    selection_emb = _fast_embed(candidates[0]["content"][:500])
                except Exception:
                    pass

        if self._tool_index and selection_emb:
            try:
                tools_schema = self.tool_registry.select_tools_by_category(
                    selection_emb, self._tool_index, top_k=2
                )
            except Exception:
                tools_schema = self.tool_registry.get_schema()
        else:
            tools_schema = self.tool_registry.get_schema()

        # Inject skill schemas so the model can call skills as tools (skill.name)
        if tools_schema and self.skill_registry:
            tools_schema = list(tools_schema) + self._skill_schemas()
        return tools_schema

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

    def _ground_evidence(
        self,
        messages: list[dict],
        tools_used: list[str],
        on_status: "Callable[[str], None] | None" = None,
        trace_id: str = "",
    ) -> None:
        """Pin the final answer to what the tools actually returned.

        One tool → light grounding (citation fence + evidence excerpt).
        Multiple tools → two-phase: extract facts first, then write the
        response. Separates reasoning (what did tools return?) from reporting
        (how to tell the user) — reduces confabulation.
        """
        if not tools_used:
            return
        evidence_lines = []
        for m in messages:
            if m["role"] == "tool":
                try:
                    data = json.loads(m["content"])
                    if data.get("success"):
                        out = str(data.get("output", ""))[:500]
                        if out:
                            evidence_lines.append(out)
                except (json.JSONDecodeError, TypeError):
                    pass
        if not evidence_lines:
            return
        evidence_text = "\n---\n".join(evidence_lines)

        if len(set(tools_used)) >= _FACT_EXTRACT_THRESHOLD:
            if on_status:
                on_status("Analyzing results...")
            messages.append({"role": "system", "content": (
                "Summarize the factual findings from the tool results above. "
                "One bullet per finding. Only include data the tools returned. "
                "No interpretation, no speculation, no training-data fill-in."
            )})
            fact_resp = self._chat(messages, think=False, temperature=TEMPERATURE_REASON)
            facts = fact_resp.get("message", {}).get("content", "").strip()
            _, facts = _strip_thinking(facts)
            flow_rec.record(trace_id, "grounding", mode="two-phase", facts=facts)
            if facts:
                messages.append({"role": "assistant", "content": facts})
                messages.append({"role": "system", "content": (
                    "Good. Now write your response to the user based ONLY on "
                    "the verified facts above. Use your natural voice. "
                    "Do not add data beyond what was extracted. "
                    "If the user needs information you don't have, say so."
                )})
        else:
            flow_rec.record(trace_id, "grounding", mode="light",
                            evidence_chars=len(evidence_text))
            messages.append({"role": "system",
                             "content": EVIDENCE_PROMPT + evidence_text})

    def _stream_answer(
        self,
        messages: list[dict],
        think: bool,
        forward_thinking: bool,
    ) -> Generator[tuple[str, bool, dict], None, tuple[str, bool]]:
        """Stream one model response, yielding (token, False, meta) chunks.

        The single home for the consume loop that the final answer, the
        empty-response retry, greetings, and confirmed-tool replies all share.
        Returns (full_text, had_think) — grab it with
        ``text, had_think = yield from self._stream_answer(...)``.
        forward_thinking=True passes think tokens/blocks through to the UI;
        False swallows them. Honors the Stop button: a set cancel flag ends
        the stream early and keeps whatever was generated.
        """
        _tokens: list[str] = []
        had_think = False
        for token, done, meta in self._chat_stream(
            messages, think=think, temperature=self._final_temp,
        ):
            if done:
                break
            if self._cancel.is_set():
                break  # user hit Stop — keep what's generated
            # Live reasoning — stream each thinking chunk to the UI as it arrives.
            if meta.get("think_token") is not None:
                if forward_thinking:
                    yield "", False, {"think_token": True, "text": meta["think_token"]}
                continue
            # Think block — the complete reasoning, emitted once thinking ends.
            if meta.get("think_block") is not None:
                had_think = True
                if forward_thinking:
                    block_text = meta["think_block"]
                    if meta.get("think_runaway"):
                        block_text += "\n\n[cut off — reasoning was looping; answering directly]"
                    if self.session_id:
                        events.emit(events.EVENT_THINK, self.session_id, text=block_text)
                    yield "", False, {"think": True, "text": block_text}
                continue
            _tokens.append(token)
            if self.session_id:
                events.emit(events.EVENT_STREAM_TOKEN, self.session_id, token=token)
            yield token, False, {}
        return "".join(_tokens), had_think

    def _fallback_text(self, tools_used: list[str]) -> str:
        """Last-resort response text when the model produced nothing visible."""
        if self._cancel.is_set():
            return "[stopped]"
        if tools_used:
            return f"Done — used {', '.join(dict.fromkeys(tools_used))}."
        return "[no response]"

    def _finalize_turn(
        self,
        *,
        user_input: str,
        clean_text: str,
        raw_text: str,
        trace_id: str,
        context: str,
        tools_used: list[str],
        turn_start: float,
    ) -> int | None:
        """Commit a finished turn — the single end-of-turn path for every exit.

        The trace keeps the raw text (with <think> tags) for debugging;
        history and the sessions DB get the clean version. Background
        post-turn work is submitted here, BEFORE the caller's final yield,
        so a consumer that stops iterating at done=True can't skip it.
        Returns the assistant message id (None if persistence failed).
        """
        self._record_trace(trace_id, user_input, context, tools_used, raw_text, turn_start)
        with self._history_lock:
            self._session_history.append({"role": "user", "content": user_input})
            # Don't keep a pure failure placeholder in replayed history — it would
            # be mimicked next turn. The user turn stays so context isn't lost.
            if clean_text.strip() not in _FAILURE_MARKERS:
                self._session_history.append({"role": "assistant", "content": clean_text})
        msg_id = self._persist_turn(user_input, clean_text)
        # The first reply of a session delivers the welcome-back note /
        # briefing — mark them consumed so they aren't re-injected next time.
        if self._turn_count <= 1:
            self._mark_session_notes_delivered()
        if self.session_id:
            events.emit(events.EVENT_STREAM_END, self.session_id,
                        tools_used=tools_used,
                        duration=round(time.monotonic() - turn_start, 3))
        # commit + learn + compression: runs off the hot path
        self._bg_pool.submit(self._post_turn, user_input, clean_text)
        return msg_id

    def _emit_status(self, label: str, on_status: "Callable[[str], None] | None" = None) -> None:
        """Send a status label to both UIs: CLI callback + web event bus."""
        if on_status:
            on_status(label)
        if self.session_id:
            events.emit(events.EVENT_STATUS, self.session_id, label=label)

    def _mark_session_notes_delivered(self) -> None:
        """One-shot session notes (welcome-back, watchdog events, briefing)
        are consumed on delivery — mark them so the next session starts clean."""
        try:
            from kai.memory.context import (
                mark_welcome_back_delivered, mark_watchdog_events_delivered,
                mark_briefing_delivered,
            )
            mark_welcome_back_delivered()
            mark_watchdog_events_delivered()
            mark_briefing_delivered(user_id=self.user_id)
        except Exception:
            pass

    def _run_confirmed_tool(
        self,
        user_input: str,
        trace_id: str,
        turn_start: float,
        on_status: "Callable[[str], None] | None" = None,
    ) -> Generator[tuple[str, bool, dict], None, None]:
        """Execute a confirm-gated tool after the user approved it."""
        pending = self._pending_confirm
        self._pending_confirm = None
        tool_name = pending["name"]
        tool_args = pending["args"]
        tools_used = [tool_name]

        self._emit_status(pending["label"], on_status)
        result = self._execute_tool_traced(tool_name, tool_args, trace_id)

        # Build messages with the tool result injected
        context = self.memory.render_context(
            query=user_input,
        ) + GROUNDING_RULE
        with self._history_lock:
            history = list(self._session_history[-_HISTORY_HARD_CAP:])

        evidence = str(result.get("output", ""))[:3000]
        messages: list[dict] = [
            {"role": "system", "content": context},
            *history,
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": tool_name, "arguments": tool_args}}]},
            {"role": "tool", "content": json.dumps(result)},
            {"role": "system", "content": EVIDENCE_PROMPT + evidence},
        ]

        self._emit_status("Responding...", on_status)
        full_text, _ = yield from self._stream_answer(
            messages, think=False, forward_thinking=False,
        )
        flow_rec.record(trace_id, "final_answer", confirmed_tool=tool_name,
                        text=full_text)
        _, clean_text = _strip_thinking(full_text)
        if not clean_text:
            clean_text = self._fallback_text(tools_used)
            flow_rec.record(trace_id, "fallback", text=clean_text)
            # Never leave the UI bubble empty — stream the recovered text.
            if self.session_id:
                events.emit(events.EVENT_STREAM_TOKEN, self.session_id, token=clean_text)
            yield clean_text, False, {}

        msg_id = self._finalize_turn(
            user_input=user_input, clean_text=clean_text, raw_text=full_text,
            trace_id=trace_id, context=context, tools_used=tools_used,
            turn_start=turn_start,
        )
        yield "", True, {"message_id": msg_id} if msg_id else {}

    def _persist_turn(self, user_input: str, response: str) -> int | None:
        """Persist user+assistant messages to the sessions DB. Returns assistant message id."""
        try:
            if not self.session_id:
                self.session_id = sessions.new_session(user_input, user_id=self.user_id)
                # New session → bump the relationship state's session counter
                # (feeds the Version C context modifier).
                try:
                    from kai.memory import state as _mstate
                    _mstate.record_session_start(str(self.user_id))
                except Exception:
                    pass
            sessions.append_message(self.session_id, "user",      user_input, self._turn_order, user_id=self.user_id)
            msg_id = sessions.append_message(self.session_id, "assistant", response, self._turn_order + 1, user_id=self.user_id)
            self._turn_order += 2
            return msg_id
        except Exception:
            if cfg.DEBUG:
                import traceback
                traceback.print_exc()
            return None  # session persistence failure never breaks a conversation

    def _record_trace(
        self,
        trace_id: str,
        user_input: str,
        context: str,
        tools_used: list[str],
        response: str,
        start_time: float,
    ) -> None:
        try:
            trace_log.record(trace_log.TraceEntry(
                trace_id     = trace_id,
                timestamp    = datetime.now().isoformat(),
                user_input   = user_input,
                model        = self.model,
                context_len  = len(context),
                tool_calls   = tools_used,
                elapsed_ms   = int((time.monotonic() - start_time) * 1000),
                response_len = len(response),
                user_id      = self.user_id,
            ))
        except Exception:
            if cfg.DEBUG:
                import traceback
                traceback.print_exc()

    def _ensure_tool_index(self) -> None:
        """
        Build the category-level embedding index in one batch call. No-op after first run.
        Embeds category descriptions (not individual tool schemas) — fast and coherent.
        Failures leave _tool_index empty — brain falls back to the full schema.
        """
        if self._tool_index_ready or not self.tool_registry:
            return
        try:
            from kai.llm.embed import embed_batch as _fast_embed_batch
            self._tool_index = self.tool_registry.build_category_index(_fast_embed_batch)
            if cfg.DEBUG:
                print(f"[tool index] {len(self._tool_index)} categories indexed")
        except Exception as exc:
            if cfg.DEBUG:
                print(f"[tool index] build failed (will use full schema): {exc}")
        finally:
            self._tool_index_ready = True

    def _ensure_memory_router(self) -> None:
        """
        Build the memory domain embedding index in one batch call. No-op after first run.
        Embeds 7 domain descriptions — fast (~30ms). Failures leave domain_index empty,
        which makes context.build() fall back to injecting everything.
        """
        if self._memory_router_ready:
            return
        try:
            from kai.llm.embed import embed_batch as _fast_embed_batch
            self.memory.init_router(_fast_embed_batch)
            if cfg.DEBUG:
                print(f"[memory router] {len(self.memory._domain_index)} domains indexed")
        except Exception as exc:
            if cfg.DEBUG:
                print(f"[memory router] build failed (will inject everything): {exc}")
        finally:
            self._memory_router_ready = True

    def _ensure_handoff_router(self) -> None:
        """
        Seed the handoff routing table on first use. No-op after first run.
        Embeds seed patterns into handoff_vec (~8 patterns, ~40ms).
        Failures are silent — routing falls back to 'chat' mode.
        """
        if self._handoff_router_ready:
            return
        try:
            from kai.llm.embed import embed as _fast_embed
            self._handoff_router.init(_fast_embed)
            if cfg.DEBUG:
                print("[handoff router] seeded and ready")
        except Exception as exc:
            if cfg.DEBUG:
                print(f"[handoff router] init failed: {exc}")
        finally:
            self._handoff_router_ready = True

    # ── Conversational learning ──────────────────────────────────────────────

    def _post_turn(self, user_input: str, assistant_text: str) -> None:
        """
        Background post-turn processing: persist turn + compress history +
        extract knowledge. Runs in the background pool — never blocks the user.
        """
        self.memory.commit_turn(user_input, assistant_text)
        # Compress between turns instead of at turn start, so the user never
        # waits on the summarization LLM call. Worst case the next turn runs
        # with slightly-too-long history — the same window the old in-turn
        # compression already tolerated while its LLM call was in flight.
        try:
            self._maybe_compress_history()
        except Exception:
            if cfg.DEBUG:
                import traceback
                traceback.print_exc()
        with self._history_lock:
            self._turn_count += 1
            count = self._turn_count
        # Rate-limit: only extract knowledge every 3rd turn to reduce Ollama
        # queue pressure. The background LLM call delays the next turn's embed
        # + chat because Ollama serializes GPU work.
        if LEARN_FROM_CONVERSATION and count % 3 == 0:
            try:
                self._extract_knowledge(user_input, assistant_text)
            except Exception:
                if cfg.DEBUG:
                    import traceback
                    traceback.print_exc()

    def _extract_knowledge(self, user_text: str, assistant_text: str) -> None:
        """
        Ask the model what it learned from this exchange.
        Saves each extracted fact as an episodic entry (entry_type='learned')
        with an embedding — permanently searchable by cosine similarity.

        Pre-filtered: skips trivial exchanges (greetings, one-word responses)
        to avoid unnecessary Ollama calls that would queue and delay the next turn.
        """
        # Pre-filter: skip short/trivial exchanges
        user_stripped = user_text.strip()
        if len(user_stripped) < 15 and len(assistant_text.strip()) < 50:
            return

        exchange = f"User: {user_text}\nKai: {assistant_text}"
        try:
            resp = self._chat(
                [{"role": "user", "content": f"{LEARN_PROMPT}\n\n{exchange}"}],
                think=False, temperature=TEMPERATURE_REASON,
            )
        except Exception:
            return  # Ollama down or model unloaded — skip silently

        result = resp.get("message", {}).get("content", "").strip()
        _, result = _strip_thinking(result)

        if not result or result.upper() == "NONE":
            return

        saved = 0
        for line in result.splitlines():
            line = line.strip().lstrip("-•* 0123456789.)")
            if line and len(line) > 10:
                self.memory.add_episode(
                    content=line,
                    entry_type="learned",
                )
                saved += 1

        if saved and self.session_id:
            events.emit(events.EVENT_MEMORY_WRITE, self.session_id,
                        entries=saved, source="knowledge_extraction")
        if cfg.DEBUG and saved:
            print(f"[learn] saved {saved} knowledge entries")

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

    def get_embed_fn(self):
        from kai.llm.embed import embed as _fast_embed
        return _fast_embed

    def _summarize_messages(self, msgs: list[dict]) -> str:
        """LLM-compress a list of turns into one past-tense paragraph.

        Shared by live history compression and clear-time archiving so the
        formatting (and the 800-char-per-message cap) can't drift apart.
        Returns "" on any failure — callers treat that as "skip, keep history".
        """
        raw = "\n\n".join(
            f"[{m['role']}]: {m.get('content', '')[:800]}" for m in msgs
        )
        try:
            resp = self._chat(_build_compress_messages(raw), think=False,
                              temperature=TEMPERATURE_REASON)
            summary = resp.get("message", {}).get("content", "").strip()
            _, summary = _strip_thinking(summary)
            return summary
        except Exception:
            if cfg.DEBUG:
                import traceback
                traceback.print_exc()
            return ""

    def _maybe_compress_history(self) -> None:
        """
        Compress _session_history when it grows too large for the context window.

        Runs in the background after each turn (from _post_turn) so the user
        never waits on it. Fires only when total chars exceed
        HISTORY_CHAR_LIMIT (~3k tokens at 4 chars/token). Older turns are
        replaced with a single summary system message so the model keeps the
        thread without blowing the token budget.

        Archives the compressed content to episodic memory so nothing is lost.
        Swap the char estimator for tiktoken later for exact token counts.

        Race-safety: old messages are NOT removed until the summary is ready.
        During the LLM call the full history remains visible to concurrent readers.
        A ``_compressing`` flag prevents overlapping compressions.
        """
        with self._history_lock:
            if self._compressing:
                return  # another thread is already compressing

            total_chars = sum(len(m.get("content") or "") for m in self._session_history)
            if total_chars <= HISTORY_CHAR_LIMIT:
                return

            keep_n = HISTORY_COMPRESS_KEEP * 2  # user + assistant = 2 messages per exchange
            hist_len = len(self._session_history)
            if hist_len <= keep_n:
                return  # not enough history to split

            self._compressing = True

            # Snapshot the old portion — do NOT trim yet so concurrent readers
            # still see the full history during the (slow) LLM call.
            split_idx = hist_len - keep_n
            to_compress = [
                m for m in self._session_history[:split_idx]
                if m.get("role") != "system"
            ]

        if not to_compress:
            with self._history_lock:
                self._compressing = False
            return

        try:
            summary = self._summarize_messages(to_compress)
            if not summary:
                return  # compression failed — history is still intact (never trimmed)

            # Atomic swap: drop the compressed messages and inject the summary.
            # split_idx still points at the right boundary because we only APPEND
            # during the window (new messages go to the end, old ones stayed put).
            with self._history_lock:
                # Safety: if history was cleared during compression, bail out
                if len(self._session_history) < split_idx:
                    return
                self._session_history = self._session_history[split_idx:]
                self._session_history.insert(0, {
                    "role":    "system",
                    "content": f"[Earlier in this conversation: {summary}]",
                })

            # Archive to episodic DB off the hot path — nothing is lost.
            self._bg_pool.submit(self.memory.archive_history, summary)
        finally:
            with self._history_lock:
                self._compressing = False

    def flush_history_snapshot(self, snapshot: list[dict]) -> None:
        """
        Compress and archive a history snapshot taken at clear-time.
        Runs in a background thread — snapshot is already captured before the clear.
        """
        messages = [m for m in snapshot if m.get("role") != "system"]
        if not messages:
            return
        summary = self._summarize_messages(messages)
        if summary:
            try:
                self.memory.archive_history(summary)
            except Exception:
                if cfg.DEBUG:
                    import traceback
                    traceback.print_exc()
