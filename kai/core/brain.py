"""
The Brain — Ollama HTTP client + ReAct conversation loop.

Flow per turn:
  1. Build context block from memory (identity + procedural + semantic + episodic)
  2. Stream final response token by token (feels instant, same as ollama run)
  3. For tool calls: non-streaming round trip, then stream the final answer
  4. Strip <think> tags (log in debug mode)
  5. Commit turn to memory

Chat is not necessarily local: TurnEngine routes it to whatever the active brain
is, which may be a cloud provider (see engine._chat and kai/llm/providers/).
Background work — memory formation, history compression — deliberately does not
follow it and pins to Ollama via _chat_local.
"""

import json
import re
import threading
import time
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import TYPE_CHECKING, Any

import kai.config as cfg
from kai.config import (
    CHAT_MODEL,
    HISTORY_CHAR_LIMIT,
    HISTORY_COMPRESS_KEEP,
    TEMPERATURE_REASON,
)
from kai.core import events
from kai.core import flow as flow_rec
from kai.core import trace as trace_log
from kai.core.crew_runner import CrewRunner
from kai.core.engine import TurnEngine, tool_receipt
from kai.core.history import HistoryManager, is_summary
from kai.core.tool_gate import _TOOL_SIGNALS, _query_needs_thinking, _query_needs_tools
from kai.llm.ollama import OllamaClient
from kai.memory import pending
from kai.memory.manager import MemoryManager
from kai.memory.privacy import learning_enabled
from kai.store import sessions
from kai.util import log
from kai.util.ids import short_id
from kai.util.text import strip_thinking as _strip_thinking

if TYPE_CHECKING:
    from kai.tools.registry import ToolRegistry

_HISTORY_HARD_CAP = 60  # safety ceiling — compression normally keeps history much smaller
_FACT_EXTRACT_THRESHOLD = 1  # two-phase fact extraction fires when ≥ this many tools were called
# Was 2, which switched the strongest anti-fabrication guard off for every
# single-tool turn — including the one where Kai was handed folder sizes and
# answered with three invented drives. Making the model list what the tools
# actually returned BEFORE writing prose is exactly the fix for that failure,
# so it now runs on any tool turn. Costs one extra model call on single-tool
# turns; a fabricated answer costs more.

# Last-resort placeholders the model emits nothing real for. They must never go
# back into the conversation — otherwise the model sees them in history and
# parrots them, turning one empty turn into a "[no response]" death spiral.
_FAILURE_MARKERS = {"[no response]", "[stopped]"}
# Prefix of the tools-ran-but-no-answer fallback. It names the tools, so it can't be
# an exact marker — but it must stay out of replayed history all the same.
_NO_REPLY_PREFIX = "⚠ No reply:"


def _is_failure_marker(text: str) -> bool:
    text = text.strip()
    return text in _FAILURE_MARKERS or text.startswith(_NO_REPLY_PREFIX)


# Explicit consent to run a parked confirm-gated tool (see _run_confirmed_tool).
# Deliberately narrow: "ok", "sure", "right" and "please" used to count, and a
# destructive call parked in an old chat ran on a new chat's "ok". Any other reply
# clears the parked call. The web UI's approve button sends "go ahead".
_CONFIRM_RE = re.compile(
    r"^(?:yes(?:,?\s+(?:please|go\s*ahead|do\s*it))?|go\s*ahead(?:\s+please)?|do\s*it|"
    r"confirm(?:ed)?|approved?|run\s*it)\s*[.!]?\s*$",
    re.IGNORECASE,
)

# Tool gate (does this turn need tools / thinking) lives in kai/tool_gate.py.
# Imported at the top of this module.

# Tool-call recovery (broken-JSON + narrated-intent), the retry-signal and
# narrated-verb patterns, and MAX_TOOL_ROUNDS moved to kai/core/engine.py with
# the tool loop that uses them.


# ── Pre-LLM intent fast-paths ────────────────────────────────────────────────
# When the WHOLE user message is one of these common, unambiguous commands, run
# the tool directly and skip the tool-round model call entirely (see
# Brain._run_fast_path). Every target is a NO-ARGUMENT tool, so there's nothing
# to misparse — the result is identical to what the model would produce, minus a
# whole LLM round. Anchored ^…$ so we only fire on an exact command, never on a
# passing mention inside a larger request. Order doesn't matter (patterns are
# mutually exclusive by construction).
_FAST_PATHS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(
            r"^\s*(?:what(?:'?s| is)?\s+(?:the\s+)?)?time(?:\s+is\s+it)?(?:\s+(?:now|today))?\s*[?.!]*\s*$",
            re.I,
        ),
        "time.now",
    ),
    (
        re.compile(
            r"^\s*(?:what(?:'?s| is)?\s+)?(?:the\s+|today'?s\s+)?date(?:\s+(?:today|now))?\s*[?.!]*\s*$",
            re.I,
        ),
        "time.now",
    ),
    (
        re.compile(
            r"^\s*(?:check|get|show|what(?:'?s| is)?)\s*(?:the\s+|my\s+)?weather\b[^?.!]*[?.!]*\s*$",
            re.I,
        ),
        "weather.current",
    ),
    (
        re.compile(
            r"^\s*(?:check|show|get|what(?:'?s| are)?)\s*(?:my\s+|the\s+)?(?:cpu\s+|gpu\s+|system\s+)?temp(?:erature)?s?\b[^?.!]*[?.!]*\s*$",
            re.I,
        ),
        "system.temps",
    ),
    (
        re.compile(
            r"^\s*(?:check|show|get|what(?:'?s| is)?)\s*(?:my\s+|the\s+)?disk\s+(?:usage|space|health)\b[^?.!]*[?.!]*\s*$",
            re.I,
        ),
        "files.disk_usage",
    ),
]

# A clause joiner ("... and ...", commas) means the message carries more than one
# ask — the single-no-arg fast-path would drop everything after the first clause.
_COMPOUND_REQUEST_RE = re.compile(r"\b(?:and|then|also|plus|as well as)\b|[,;]", re.I)
# Weather's compound guard excludes the comma: a place legitimately contains one
# ("weather in Austin, TX"), but a real joiner still means more than one ask.
_WEATHER_COMPOUND_RE = re.compile(r"\b(?:and|then|also|plus|as well as)\b|[;]", re.I)

# ── Weather location extraction ──────────────────────────────────────────────
# A weather request usually names a place ("weather in Boise"). The fast-path
# MUST pass that as `location` — calling weather.current with no args silently
# returns the IP-geolocated city instead (the Boise→Denver bug). Extract it
# here so the deterministic path is correct, not just fast.
_WEATHER_LOC_RE = re.compile(
    r"\bweather\b[^?.!]*?\b(?:in|for|at|near|around|of)\s+(?P<loc>.+?)\s*[?.!]*$", re.I
)
# Phrases that follow "in/for/at" but name a TIME, not a place — never a location.
_NOT_A_PLACE = re.compile(
    r"^(?:the\s+)?(?:today|tomorrow|tonight|now|right\s+now|later|this\s+\w+|next\s+\w+|"
    r"the\s+(?:morning|afternoon|evening|weekend|week|day)|a\s+(?:bit|while|moment)|"
    r"noon|midnight|lunch)\b",
    re.I,
)
_TRAILING_TIME_RE = re.compile(
    r"\s+(?:today|tomorrow|tonight|right\s+now|now|later|"
    r"this\s+(?:morning|afternoon|evening|week|weekend)|next\s+week)\s*$",
    re.I,
)


def _weather_location(text: str) -> str:
    """Extract the place from a weather request, or '' for local/none.

    Conservative on purpose: a time phrase ('for tomorrow', 'in the morning') is
    NOT a place, and an over-long capture is rejected. When '' is returned the
    tool uses IP geolocation (correct for a plain 'what's the weather')."""
    m = _WEATHER_LOC_RE.search(text)
    if not m:
        return ""
    loc = m.group("loc").strip().strip("\"'").rstrip(".,!?").strip()
    loc = _TRAILING_TIME_RE.sub("", loc).strip()
    if not loc or _NOT_A_PLACE.match(loc) or len(loc) > 40:
        return ""
    return loc


def _match_fast_path(user_input: str) -> tuple[str, dict] | None:
    """Return (tool_name, args) for a whole-input fast-path command, or None.

    Deliberately strict — only an exact command match fires. Anything else (extra
    detail, a question, a follow-up clause) falls through to the normal LLM path.
    Most fast-paths are no-arg ({}), but weather carries its extracted location.
    """
    text = user_input.strip()
    if not text or len(text) > 80:  # commands are short; long text is a real request
        return None
    for pattern, tool_name in _FAST_PATHS:
        if not pattern.match(text):
            continue
        # Weather carries its extracted location and tolerates a comma in the place;
        # a real joiner ("weather in X AND containers") still falls through.
        if tool_name == "weather.current":
            if _WEATHER_COMPOUND_RE.search(text):
                return None
            loc = _weather_location(text)
            return tool_name, ({"location": loc} if loc else {})
        # Other fast-paths are no-arg. A compound request ("disk space AND
        # containers") must not fast-path one — the trailing [^?.!]* would swallow
        # the second clause. Any clause joiner → fall through to the full path.
        if _COMPOUND_REQUEST_RE.search(text):
            return None
        return tool_name, {}
    return None


# ── Foreground-turn gate ─────────────────────────────────────────────────────
# Background work (knowledge extraction) checks this so it never queues a second
# generation behind a live user turn. A counter, not a bool: overlapping turns
# across brains all register, and background work only runs when the count is 0.
_active_turns = 0
_active_turns_lock = threading.Lock()


def _begin_turn() -> None:
    global _active_turns
    with _active_turns_lock:
        _active_turns += 1


def _end_turn() -> None:
    global _active_turns
    with _active_turns_lock:
        if _active_turns > 0:
            _active_turns -= 1


def foreground_busy() -> bool:
    """True while any user turn is mid-flight — background model work should wait."""
    with _active_turns_lock:
        return _active_turns > 0


# Shared compression prompt — used by _maybe_compress_history, flush_history_snapshot,
# and web.py _archive_pending_turns. Defined once to avoid drift.
COMPRESS_PROMPT = (
    "Compress this conversation into a single concise paragraph. "
    "Preserve: facts shared, decisions made, topics discussed, preferences stated. "
    "Keep every specific the user gave (names, numbers, amounts, places, dates) word for word, "
    "including those in an [earlier summary], because what you write replaces it. "
    "Write in past tense. Be specific. No filler."
)

LEARN_PROMPT = (
    "Review this conversation exchange. Extract any NEW, LASTING fact worth "
    "remembering about the user for future conversations — identity, profession, "
    "hardware, health, preferences, relationships, or knowledge they shared.\n"
    "Output ONE fact per line in the form:  CATEGORY | fact\n"
    "CATEGORY must be exactly one of: identity, profession, hardware, health, "
    "preference, relationship, knowledge.\n"
    "Keep each fact concise and specific. Skip greetings, small talk, and anything "
    "transient (moods, the current task).\n"
    "If nothing new was learned, respond with exactly: NONE"
)


def _trivial_exchange(user_text: str, assistant_text: str) -> bool:
    """Too short to carry anything worth remembering — skip the extraction LLM call."""
    return len(user_text.strip()) < 15 and len(assistant_text.strip()) < 50


# Always-present grounding calibration — appended to every turn's context so
# the rule sits at the decision boundary where the model generates. Defined
# once; shared by the normal turn path and the confirmed-tool path.
GROUNDING_RULE = (
    "\n\n[CRITICAL GROUNDING RULE]\n"
    "Before stating ANY fact about this system, ask: did a tool result "
    "or [SEMANTIC] entry provide this data in THIS conversation? "
    'If not — call a tool first, or say "I\'d need to check that." '
    "Never fabricate numbers, outputs, or success messages.\n"
    "Saying you do not know, or that no tool here can answer this, is a "
    "CORRECT and complete answer. It is never a failure. Guessing is."
)

# Citation fence injected after tool calls — keeps the final answer pinned to
# what the tools actually returned. Callers append the evidence text to it.
EVIDENCE_PROMPT = (
    "GROUNDING: Respond using ONLY data from tool results above. "
    "Quote exact values. If you lack data the user needs, say so.\n\n"
    "KEY EVIDENCE:\n"
)

# Injected when the post-answer grounding check flags a hedge (see
# cerebellum.verify_answer): the turn ran tools but the draft answer said it
# couldn't get the data or asked permission to retry. Nudge it to ACT, once.
# Injected when check_grounded finds numbers in the draft that trace to no tool
# result. The opposite failure to ANSWER_REVERIFY_NUDGE, and the more dangerous
# one: a confident answer built from invented values reads exactly like a real
# one. Names the offending numbers so the rewrite has something concrete to drop.
FABRICATION_NUDGE = (
    "Your draft states numbers that appear in NO tool result from this turn: "
    "{stray}. You may only report values the tools actually returned. Rewrite "
    "using those values alone. If the tools did not provide what the user asked "
    "for, say plainly that you could not get it and what you would need — that "
    "is a correct answer. Do not substitute plausible-looking values."
)

ANSWER_REVERIFY_NUDGE = (
    "Your draft reply says you couldn't get the information or asks the user "
    "whether to try — but you have the tools to get it yourself. Do NOT ask "
    "permission. If a tool result above already has the answer, use it. If a "
    "previous call used the wrong input (e.g. the wrong location or name), call "
    "the tool again with the correct input from the user's request, then answer "
    "directly. Only say you can't if no tool can do it."
)

# Injected when a destructive tool is parked at the confirm gate. The placeholder tool
# message used to be read as evidence, and the answer reported the work as done.
CONFIRM_PENDING_PROMPT = (
    "{label} ({name}) is waiting for the user's approval and has NOT run. In one or two "
    "sentences say what it will do and ask them to approve it. Do not describe it as "
    "started or finished, and do not report any result from it."
)

# Shown instead of the model's text when a parked tool's reply still claims it is
# running or done: the one message on that path that is certainly true.
STALE_APPROVAL_TEXT = (
    "That approval is no longer waiting, so nothing ran. If you still want it, ask again and "
    "approve the new request."
)

CONFIRM_WAITING_TEXT = (
    "This needs your approval before it runs, and nothing has run yet: {label} ({name}). "
    "Say \u201cgo ahead\u201d or use the button to run it, or tell me to skip it."
)

# Injected when check_claims_backed finds work reported that no tool did this turn.
COMMITMENT_NUDGE = (
    "Your draft reports work that no tool did this turn: {unbacked}. Rewrite it to report "
    "only what the tool results above show actually happened. If something was not done, "
    "say so plainly. Do not state a path or a result you have not seen in a tool result."
)


# Friendly tool-status labels (_TOOL_LABELS) now live in kai/core/engine.py with
# the tool loop that uses them — sourced from the registry (single source of truth).


# OllamaClient (the Ollama HTTP wrapper) now lives in kai/ollama.py — imported
# at the top of this module.


# ── Helpers ────────────────────────────────────────────────────────────────────


def _build_compress_messages(raw_text: str) -> list[dict]:
    """Build the messages list for a compression call. Single source of truth."""
    return [{"role": "user", "content": f"{COMPRESS_PROMPT}\n\n{raw_text}\n\nSummary:"}]


# ── Brain ──────────────────────────────────────────────────────────────────────


# The citation fence is injected into the prompt, so it is excerpted to keep the
# turn small. The grounding CHECK must not be: pc.deep_scan returns ~17k chars,
# and verifying an answer against only its first 500 flags every correctly-quoted
# value past that point as fabricated. Truncating the check turns it into a
# false-positive generator that makes Kai rewrite correct answers.
_FENCE_EXCERPT = 500


def _executed_results(messages: list[dict]) -> list[tuple[str, str]]:
    """(tool name, output) for each call that actually ran and succeeded this turn.

    Recipe output is split per step ("[tool.name] output"), so a delete a recipe really
    did backs a claim about it: T1-21 flagged exactly that as unbacked. Crew findings
    carry no tool name and so contribute nothing here.
    """
    results: list[tuple[str, str]] = []
    for m in messages:
        if m.get("role") != "tool":
            continue
        try:
            data = json.loads(m["content"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not data.get("success") or data.get("pending"):
            continue
        name = str(data.get("_source", "")).split(" · ", 1)[0].strip()
        output = str(data.get("output", ""))
        if name.startswith("skill."):
            for step in re.finditer(r"^\[([a-z0-9_.]+)\] (.*)$", output, re.MULTILINE):
                if not step.group(2).startswith("ERROR"):
                    results.append((step.group(1), step.group(2)))
        if name:
            results.append((name, output))
    return results


def _evidence_from(messages: list[dict], limit: int | None = _FENCE_EXCERPT) -> str:
    """Successful tool output from this turn. limit=None returns it in full.

    Shared by _ground_evidence (which fences the answer to an excerpt) and the
    post-answer check_grounded (which needs all of it, or it polices text the
    model was legitimately shown).
    """
    lines = []
    for m in messages:
        if m.get("role") != "tool":
            continue
        try:
            data = json.loads(m["content"])
        except (json.JSONDecodeError, TypeError):
            continue
        if data.get("success") and not data.get("pending"):
            out = str(data.get("output", ""))
            if limit is not None:
                out = out[:limit]
            if out:
                lines.append(out)
    return "\n---\n".join(lines)


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
        self.skill_registry = skill_registry  # kai.skills.SkillRegistry (optional)
        self._disabled_tools: set[str] = self._load_disabled_tools()  # Settings → Tools off
        self._history = HistoryManager()  # session history, lock, turn counters, compression
        self.session_id: str | None = None  # current persisted session UUID
        self._tool_index: dict[str, list[float]] = {}  # name → embedding vector, built lazily
        self._tool_index_ready: bool = False
        self._memory_router_ready: bool = False  # memory domain index built lazily
        self._handoff_router_ready: bool = False  # handoff pattern index seeded lazily
        from kai.memory.knowledge import HandoffRouter

        self._handoff_router = HandoffRouter()
        self._crew = CrewRunner(self)  # agent-crew execution layer
        self._pending_confirm: dict | None = None  # tool call awaiting user confirmation
        self._surfaced_paths: list[str] = []  # tree nodes shown this turn — reinforced post-turn
        self._tool_level: str = cfg.DEFAULT_TOOL_LEVEL  # which model runs tool rounds
        self._tool_model: str | None = None  # resolved lazily, availability-checked
        self._tool_model_resolved: bool = False
        self._bg_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="kai-bg")
        self._drain_lock = threading.Lock()  # post-turn and the scheduler can both start a drain

        # Active chat brain — defaults to local Ollama + CHAT_MODEL. set_active_brain()
        # can point the chat role at a connected cloud brain; _chat/_chat_stream wrap
        # the call with a fail→local fallback. The tool-round model always stays local.
        self._chat_client = self.ollama
        self._chat_model = self.model
        from kai.llm.models import BUILTIN_MODEL_NAME

        self._active_brain_name = BUILTIN_MODEL_NAME  # the models.json entry the chat role runs on

        # The shared tool-calling mechanism (tool rounds, execution, cerebellum,
        # chat/tool-model primitives). Brain drives it as the generalist; the crew
        # drives the same instance per specialist. Reads Brain's live runtime state
        # (active brain, cancel, session, temp, tool level) via proxy properties.
        self._engine = TurnEngine(self)

        # Sync this user's tool-doc tree nodes (tools/<namespace>/<tool_name>) once
        # per process — idempotent upsert keeps the docs current with the registry.
        # render_tool_index() reads these every turn via context.build() to emit the
        # [TOOLS] block. Failures are swallowed inside; never blocks construction.
        if self.tool_registry:
            from kai.memory.tool_docs import ensure_tool_docs_synced

            ensure_tool_docs_synced(self.user_id)

        # Re-apply a persisted cloud brain selection (best-effort; no-op if local).
        self._restore_active_brain()

        # Best-effort: drain any extraction rows a prior (possibly hard-killed)
        # session left queued for this user. Runs on the bg pool, foreground-gated,
        # so it never blocks construction or a live turn. The per-turn drain also
        # catches these on the user's next message.
        self._bg_pool.submit(self._drain_pending_learning)

    # ── Per-user tool enablement (Settings → Tools on/off) ───────────────────
    def _load_disabled_tools(self) -> set[str]:
        """Read the user's turned-off tool set from persistent settings.

        Stored as a JSON list under the `disabled_tools` fact — the same
        per-user fact mechanism the generation preset and tool level use.
        """
        raw = self.memory.get_fact("disabled_tools")
        if not raw:
            return set()
        try:
            data = json.loads(raw)
            return {str(t) for t in data} if isinstance(data, list) else set()
        except (json.JSONDecodeError, TypeError):
            return set()

    @property
    def disabled_tools(self) -> set[str]:
        """Tool names the user turned off — hidden from the model's schema and
        blocked at dispatch. The engine reads this via a proxy property."""
        return self._disabled_tools

    def set_tool_disabled(self, name: str, disabled: bool) -> set[str]:
        """Turn a tool off (disabled=True) or back on. Persists the change and
        updates the live set so it takes effect on the next turn. Returns the
        new disabled set."""
        if disabled:
            self._disabled_tools.add(name)
        else:
            self._disabled_tools.discard(name)
        self.memory.set_fact(
            "disabled_tools",
            json.dumps(sorted(self._disabled_tools)),
            source="user_setting",
        )
        return self._disabled_tools

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
        return {
            "key": key,
            "label": preset["label"],
            "think": self._think,
            "temp": self._final_temp,
        }

    def set_temperature(self, temp: float) -> float:
        """Override the final-answer temperature for this session (slider). Clamped."""
        self._final_temp = max(cfg.TEMP_MIN, min(cfg.TEMP_MAX, float(temp)))
        return self._final_temp

    def apply_tool_level(self, key: str) -> dict:
        """Select which model runs tool-call rounds. Mirrors apply_preset.

        An unavailable model (not pulled in Ollama) falls back to running the
        rounds on the chat model — same as the "off" level — so selecting any
        level never breaks anything.
        Returns the resolved {key, label, model, available}.
        """
        level = cfg.TOOL_MODEL_LEVELS.get(key)
        if not level:
            raise ValueError(f"Unknown tool level: {key!r}")
        self._tool_level = key
        self._tool_model_resolved = False  # re-check availability on next use
        model, available = self._engine._resolve_tool_model()
        return {
            "key": key,
            "label": level["label"],
            "model": level["model"],
            "available": available,
        }

    # ── Active chat brain (local Ollama or a connected cloud brain) ───────────

    def set_active_brain(self, entry: dict, persist: bool = True) -> dict:
        """Point the chat role at a model-registry entry — local or cloud.

        A cloud entry resolves its client + API key via the registry/keystore;
        a missing key raises LLMKeyMissing (the caller decides what to do).
        The tool-round model is unaffected — only the chat role moves.
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
        self._active_brain_name = entry.get("name", "")
        # Thinking belongs to the preset. The entry's flag used to override it, switching thinking
        # off while Settings still said "Thinking" (T2-40).
        if persist:
            try:
                self.memory.set_fact("active_brain", entry.get("name", ""), source="user_setting")
            except Exception:
                pass
        return {"name": entry.get("name", ""), "provider": provider, "model": model_id}

    @property
    def active_brain_name(self) -> str:
        """Name of the models.json entry in use. Matched by name, not model id: two entries can
        share an Ollama id (T2-41)."""
        return self._active_brain_name

    def _restore_active_brain(self) -> None:
        """Re-apply the persisted active brain on construction (best-effort).

        Local entries too: restoring only cloud ones silently dropped a chosen local model on
        restart while the setting still named it (P1-8). A missing entry or key, or any failure,
        leaves the chat role on the built-in model.
        """
        try:
            name = self.memory.get_fact("active_brain")
            if not name:
                return
            from kai.llm import models as _models

            entry = _models.get_model(name)
            if entry:
                self.set_active_brain(entry, persist=False)
        except Exception:
            if cfg.DEBUG:
                import traceback

                traceback.print_exc()

    # ── Engine delegators ────────────────────────────────────────────────────
    # The tool loop moved to TurnEngine (kai/core/engine.py). These thin
    # forwarders keep the historical Brain call surface — used by tests and by
    # Brain's own streaming / grounding / learning paths — pointing at the engine.
    def _chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        think: bool = False,
        temperature: float | None = None,
    ) -> dict:
        return self._engine._chat(messages, tools=tools, think=think, temperature=temperature)

    def _chat_stream(self, messages: list[dict], think: bool, temperature: float):
        return self._engine._chat_stream(messages, think=think, temperature=temperature)

    def _chat_local(
        self, messages: list[dict], think: bool = False, temperature: float | None = None
    ) -> dict:
        """Chat on the local brain, whatever the active brain happens to be.

        Background work must not ride the active brain. When that is a cloud
        provider, _chat sends the whole exchange off-device — fine for the reply
        the user asked for, not for work they never see. Memory formation and
        history compression run on a bg thread with nothing on screen, so they
        pin to Ollama here and the cloud only ever sees the visible answer.
        """
        temp = self._final_temp if temperature is None else temperature
        return self.ollama.chat(
            messages, tools=None, model=self.model, think=think, temperature=temp
        )

    def _execute_tool(self, name: str, args: dict, trace_id: str) -> dict:
        return self._engine._execute_tool(name, args, trace_id)

    def request_stop(self) -> None:
        """Signal the in-flight turn to stop — breaks tool-round and streaming loops.
        Whatever has been generated so far is kept and finalized."""
        self._cancel.set()

    def cancel_pending_confirm(self, call_id: str | None = None) -> str | None:
        """Drop a parked confirm-gated tool call without running it. Returns its name.

        With a call_id only that call is dropped: Skip on an older card must not cancel a
        newer one.
        """
        pending = self._pending_confirm
        if not pending or (call_id is not None and pending.get("call_id") != call_id):
            return None
        self._pending_confirm = None
        return pending["name"]

    def pending_confirm_summary(self) -> dict | None:
        """The approval still waiting, so a reloaded page can show its card again."""
        pending = self._pending_confirm
        if not pending:
            return None
        return {key: pending.get(key) for key in ("call_id", "name", "label", "args")}

    def generate_greeting(
        self, fresh: bool = False
    ) -> Generator[tuple[str, bool, dict], None, None]:
        """Stream Kai's own opening line so she starts the conversation.

        fresh=False (cold open): may use her welcome-back note + memory to pick up
            where things left off; consumes the welcome-back note.
        fresh=True (new chat): a brief clean-start greeting that ignores the note.

        Yields (token, done, meta) like run_stream. The greeting is kept in the
        in-session history (so the model has continuity) but is not persisted as a
        standalone DB turn — the session is created normally on the user's first reply.
        """
        context = self.memory.render_context(
            query="",
            include_welcome_back=not fresh,
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
            {"role": "user", "content": directive},
        ]
        # A Stop left over from a previous turn must not kill the greeting —
        # this is a fresh generation, so clear the flag like run_stream does.
        self._cancel.clear()
        full_text, _ = yield from self._stream_answer(
            messages,
            think=False,
            forward_thinking=False,
        )
        _, clean = _strip_thinking(full_text)
        clean = clean.strip()
        if clean:
            self._history.append("assistant", clean)
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
            schemas.append(
                {
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
                }
            )
        return schemas

    def _skill_step_guard(self, allow_destructive: bool):
        """Per-step check for skills that call tools from code (they can't be scanned up
        front): a turned-off tool never runs, and a destructive one runs only when the
        whole skill was approved."""
        from kai.tools.registry import confirm_tool_names

        disabled, needs_ok = set(self._disabled_tools), confirm_tool_names()

        def guard(tool_name: str) -> None:
            real = self.tool_registry.resolve_name(tool_name) if self.tool_registry else tool_name
            if real in disabled:
                raise PermissionError(f"{real} is turned off in Settings")
            if real in needs_ok and not allow_destructive:
                raise PermissionError(f"{real} needs your approval first")

        return guard

    def run_skill(
        self, name: str, args: dict | None = None, allow_destructive: bool = False
    ) -> dict:
        """
        Execute a skill by name. Returns a tool-result-shaped dict
        so it can slot into tool-call rounds seamlessly.
        """
        if not self.skill_registry:
            return {"success": False, "error": "No skill registry configured"}
        try:
            result = self.skill_registry.run(
                name, args or {}, guard=self._skill_step_guard(allow_destructive)
            )
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
        self._history.clear()
        self.session_id = None
        self._pending_confirm = None  # a parked tool must never carry into a new chat

    def snapshot_history(self) -> list[dict]:
        """Thread-safe snapshot of current history for archiving."""
        return self._history.snapshot()

    # ── Public surface for collaborators (api/state, web, sleep) ──────────────
    # These let external code drive the Brain without reaching into its private
    # state, so internals (history storage, the tool index, temp handling) can be
    # refactored without a ripple across the app.

    @property
    def final_temperature(self) -> float:
        """The temperature used for the final answer this session."""
        return self._final_temp

    def prime_indexes(
        self, tool_index: dict[str, list[float]] | None = None, router_ready: bool = False
    ) -> None:
        """Seed the per-user tool index + readiness flags from shared, already-built
        indexes so a freshly-created Brain skips re-embedding them."""
        if tool_index is not None:
            self._tool_index = dict(tool_index)
            self._tool_index_ready = bool(tool_index)
        self._memory_router_ready = bool(router_ready)

    def append_external_turn(self, role: str, content: str) -> None:
        """Append a turn produced outside the chat loop (e.g. a document-upload
        note) to the session history, taking the history lock internally."""
        self._history.append(role, content)

    def load_session(self, session_id: str, messages: list[dict]) -> int:
        """Replace in-memory history with a saved session. Returns message count."""
        count = self._history.replace(messages)
        self.session_id = session_id
        self._pending_confirm = None
        return count

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
        confirm_call_id: str | None = None,
    ) -> Generator[tuple[str, bool, dict], None, None]:
        """Public streaming entry. Marks a turn in flight (so background work
        yields to it) then delegates to the implementation."""
        _begin_turn()
        try:
            yield from self._run_stream_impl(user_input, trace_id, on_status, confirm_call_id)
        finally:
            _end_turn()

    def _run_stream_impl(
        self,
        user_input: str,
        trace_id: str | None = None,
        on_status: "Callable[[str], None] | None" = None,
        confirm_call_id: str | None = None,
    ) -> Generator[tuple[str, bool, dict], None, None]:
        """
        Streaming turn. Yields (token, done, {}) until done=True.
        The CLI iterates this and prints tokens as they arrive.
        Tool calls are handled internally (non-streaming) before the
        final answer is streamed.
        """
        trace_id = trace_id or short_id(8)
        turn_start = time.monotonic()
        tools_used: list[str] = []
        self._cancel.clear()  # fresh turn — clear any prior stop request
        self._turn_receipts: list[dict] = []  # tools that ran this turn, for receipts

        # Bind this turn's user/session to the thread up front so flow records
        # (route, rounds, etc.) are user-scoped — not just calls made after the
        # first tool dispatch, which re-sets these in _execute_tool.
        from kai.core._app_state import set_current_session_id, set_current_user_id

        set_current_user_id(self.user_id)
        set_current_session_id(self.session_id)

        # ── Approval from a card ──────────────────────────────────────────────
        # A card's Go ahead names the call it was shown for. Without that, Go ahead on an
        # older card would run whatever happens to be parked now.
        if confirm_call_id is not None:
            pending = self._pending_confirm
            if not pending or pending.get("call_id") != confirm_call_id:
                flow_rec.record(trace_id, "stale_approval", call_id=confirm_call_id)
                yield from self._reply_without_running(user_input, trace_id, turn_start)
                return
            log.info(f"[confirm] approved on its card — executing {pending['name']}")
            yield from self._run_confirmed_tool(user_input, trace_id, turn_start, on_status)
            return

        # ── Pending tool confirmation ────────────────────────────────────────
        # If a confirm-gated tool is waiting and the user just said "go ahead",
        # execute the tool now and stream the model's presentation of results.
        if self._pending_confirm and _CONFIRM_RE.match(user_input.strip()):
            log.info(f"[confirm] user confirmed — executing {self._pending_confirm['name']}")
            yield from self._run_confirmed_tool(
                user_input,
                trace_id,
                turn_start,
                on_status,
            )
            return

        # If the user said something other than confirm, clear the pending tool
        if self._pending_confirm:
            log.info(
                f"[confirm] user said '{user_input}' — clearing pending {self._pending_confirm['name']}"
            )
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
        history = self._history.window(_HISTORY_HARD_CAP)
        # Never replay failure placeholders — otherwise the model mimics them.
        # (Also scrubs any "[no response]" already stored from earlier sessions.)
        history = [
            m
            for m in history
            if not (m.get("role") == "assistant" and _is_failure_marker(m.get("content", "")))
        ]
        messages: list[dict] = [
            {"role": "system", "content": context},
            *history,
            {"role": "user", "content": user_input},
        ]
        tools_schema = self._select_tool_schema(user_input, history, query_emb, handoff_mode)

        try:
            _offered = (
                [t["function"]["name"] for t in tools_schema]
                if isinstance(tools_schema, list)
                else []
            )
        except Exception:
            _offered = ["?"]
        flow_rec.record(
            trace_id,
            "route",
            input=user_input,
            handoff=handoff_mode,
            think=use_think,
            tool_level=self._tool_level,
            context_chars=len(context),
            tools_offered=", ".join(_offered) or "none",
        )

        # ── Tool-call rounds (non-streaming) ──────────────────────────────────
        # Rounds pick and run tools. If the model answers in prose instead, that prose
        # comes back as `direct` and is discarded below. It is never the reply.
        direct = None
        took_fast_path = False
        # ── Pre-LLM fast-path: an exact, unambiguous command runs its tool
        # directly and skips the whole tool-round model call. Falls through to
        # the streamed answer with the result already grounded in `messages`.
        fast = _match_fast_path(user_input) if self.tool_registry else None
        if fast and fast[0] in set(self.tool_registry.list_tools()):
            fast_tool, fast_args = fast
            took_fast_path = True
            flow_rec.record(trace_id, "fast_path_hit", name=fast_tool, args=fast_args)
            yield from self._watch(
                self._engine._run_fast_path(
                    fast_tool,
                    messages,
                    tools_used,
                    args=fast_args,
                    query_emb=query_emb,
                    user_input=user_input,
                    trace_id=trace_id,
                    on_status=on_status,
                )
            )
        elif cfg.CREW_ENABLED and self.tool_registry:
            # Crew path (Part C/3b): triage → specialist(s); findings injected as
            # evidence, then Kai's voice synthesizes (unchanged). The generalist
            # _run_tool_rounds loop is bypassed entirely on this branch.
            # run_turn returns the triage think decision for the streamed answer.
            use_think = yield from self._watch(
                self._crew.run_turn(
                    user_input,
                    messages,
                    tools_used,
                    query_emb=query_emb,
                    handoff_mode=handoff_mode,
                    tools_open=bool(tools_schema),
                    trace_id=trace_id,
                    on_status=on_status,
                )
            )
        elif tools_schema:
            direct = yield from self._watch(
                self._engine._run_tool_rounds(
                    messages,
                    tools_schema,
                    tools_used,
                    query_emb=query_emb,
                    user_input=user_input,
                    trace_id=trace_id,
                    on_status=on_status,
                )
            )
        if direct is not None:
            # A tool round's own prose is never the reply. It is written at tool temperature
            # with no thinking, arrives in one piece, and skipped grounding and every answer
            # check: invented startup apps and "up to date" with 15 updates pending both
            # shipped this way (P1-1). The path below grounds and checks the answer when a
            # tool ran, and streams it with the preset's thinking when none did.
            flow_rec.record(trace_id, "discarded_prose", text=direct[1][:400])

        if self._pending_confirm:
            messages.append(
                {
                    "role": "system",
                    "content": CONFIRM_PENDING_PROMPT.format(
                        label=self._pending_confirm["label"], name=self._pending_confirm["name"]
                    ),
                }
            )

        # ── Ground the response in tool evidence ─────────────────────────────
        self._ground_evidence(
            messages, tools_used, on_status, trace_id=trace_id, fast_path=took_fast_path
        )

        # ── Stream the final answer ───────────────────────────────────────────
        if tools_used:
            self._emit_status("Responding...", on_status)

        # After tool rounds, disable thinking — the model already reasoned during
        # the tool phase.  Thinking here risks the entire final answer being
        # swallowed into <think> tags, producing "[no response]".
        final_think = use_think and not tools_used

        # Buffer, check, then reveal whenever the answer could misstate what happened: a
        # tool ran (unbacked claims, hedging despite evidence, invented numbers) or a tool
        # is parked for approval (claims that it is running or done). Chat streams live.
        parked = self._pending_confirm is not None
        verify = (bool(tools_used) or parked) and not self._cancel.is_set()
        streamed_live = not verify

        full_text, had_think = yield from self._stream_answer(
            messages,
            think=final_think,
            forward_thinking=True,
            forward_tokens=streamed_live,
        )
        _, clean_text = _strip_thinking(full_text)

        if verify and clean_text:
            from kai.memory import cerebellum as _cb

            if parked and _cb.claims_work(clean_text):
                # Told to ask for approval, the model kept saying it was executing (T1-15).
                # Say the true thing deterministically instead.
                flow_rec.record(
                    trace_id, "answer_replaced", check="parked_claim", text=clean_text[:400]
                )
                clean_text = full_text = CONFIRM_WAITING_TEXT.format(
                    label=self._pending_confirm["label"], name=self._pending_confirm["name"]
                )
            elif tools_used:
                full_text, clean_text = yield from self._checked_answer(
                    messages, full_text, clean_text, user_input, tools_used, trace_id
                )

        flow_rec.record(
            trace_id, "final_answer", think=final_think, had_think=had_think, text=full_text
        )

        if not clean_text and not self._cancel.is_set():
            # Model produced no visible output — thinking swallowed the answer, a
            # tool round left nothing, or the tool model returned no call and the
            # chat model went silent. Retry once with an explicit nudge so the
            # model writes a real reply instead of nothing. (Skipped on Stop.)
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Reply to the user now, directly and in plain text. Do not call any tools."
                    ),
                }
            )
            retry_text, _ = yield from self._stream_answer(
                messages,
                think=False,
                forward_thinking=False,
                forward_tokens=streamed_live,
            )
            flow_rec.record(trace_id, "retry_answer", text=retry_text)
            _, clean_text = _strip_thinking(retry_text)
        if not clean_text:
            clean_text = self._fallback_text(tools_used)
            flow_rec.record(trace_id, "fallback", text=clean_text)
            streamed_live = False  # the fallback text was never streamed

        # Reveal any answer not already shown live — a buffered (verified) tool
        # turn or the fallback path — so the UI shows it instead of an empty bubble.
        if not streamed_live and clean_text:
            if self.session_id:
                events.emit(events.EVENT_STREAM_TOKEN, self.session_id, token=clean_text)
            yield clean_text, False, {}

        turn_meta = {
            "receipts": self._turn_receipts,
            "grounding": self._grounding_for(messages, clean_text, parked),
        }
        msg_id, latency_ms = self._finalize_turn(
            user_input=user_input,
            clean_text=clean_text,
            raw_text=full_text,
            trace_id=trace_id,
            context=context,
            tools_used=tools_used,
            turn_start=turn_start,
            turn_meta=turn_meta,
        )
        yield "", True, self._done_meta(msg_id, latency_ms, turn_meta)

    def _reply_without_running(self, user_input: str, trace_id: str, turn_start: float):
        """Answer an approval whose call is no longer waiting. Nothing runs."""
        yield STALE_APPROVAL_TEXT, False, {}
        turn_meta = {"receipts": []}
        msg_id, latency_ms = self._finalize_turn(
            user_input=user_input,
            clean_text=STALE_APPROVAL_TEXT,
            raw_text=STALE_APPROVAL_TEXT,
            trace_id=trace_id,
            context="",
            tools_used=[],
            turn_start=turn_start,
            turn_meta=turn_meta,
        )
        yield "", True, self._done_meta(msg_id, latency_ms, turn_meta)

    def _watch(self, gen):
        """Pass an engine generator's yields through, keeping each tool receipt for this
        turn's done event and saved message. Returns what the generator returns."""
        try:
            item = next(gen)
            while True:
                if item[2].get("tool_result"):
                    self._turn_receipts.append(item[2]["tool_result"])
                sent = yield item
                item = gen.send(sent)
        except StopIteration as stop:
            return stop.value

    def _grounding_for(self, messages: list[dict], answer: str, parked: bool) -> dict:
        """How far the final answer is backed by what ran this turn: the UI's provenance line."""
        from kai.memory import cerebellum as _cb

        receipts = self._turn_receipts
        if parked:
            return {"state": "waiting"}
        if not receipts:
            return {"state": "unchecked"}
        if not any(r["ok"] for r in receipts):
            return {"state": "tool_failed", "failed": [r["name"] for r in receipts]}
        executed = _executed_results(messages)
        if executed and _cb.check_claims_backed(answer, executed).verdict >= _cb.Verdict.FLAG:
            return {"state": "unbacked"}
        checked, untraced = _cb.number_trace(answer, _evidence_from(messages, limit=None))
        if not checked:
            return {"state": "ran"}
        if untraced:
            return {"state": "partial", "checked": checked, "untraced": untraced[:6]}
        return {"state": "grounded", "checked": checked, "traced": checked}

    # ── Turn phases ──────────────────────────────────────────────────────────
    # run_stream is the orchestrator; each method below is one phase with one
    # job. Phases that stream UI chunks are generators and must be consumed
    # with `yield from`; they hand results back via their return value.
    # (The tool-round mechanism itself lives in kai/core/engine.py — TurnEngine.)

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
                    user_input,
                    _fast_embed,
                    query_emb=query_emb,
                )
                log.debug(f"[handoff] mode={handoff_mode} conf={handoff_conf:.3f}")
            except Exception:
                pass
        return query_emb, handoff_mode

    def _build_turn_context(
        self,
        user_input: str,
        query_emb: list[float] | None,
        trace_id: str = "",
    ) -> str:
        """Render the memory context block + learned knowledge + grounding rule."""
        context = self.memory.render_context(query=user_input, query_embedding=query_emb)
        # Tree memory: [MEMORY CONTEXT] ranked by the Version C equation.
        # Prepended so relationship state + ranked facts sit at the very top.
        tree_block = self._render_tree_context(query_emb, trace_id)
        if tree_block:
            context = tree_block + "\n\n" + context
        # Inject learned knowledge if the store has relevant entries for this query.
        # Runs for all modes — learned knowledge is always useful when it matches.
        try:
            if self.memory.knowledge_count() > 0:
                hits = self.memory.search_knowledge(user_input, query_embedding=query_emb)
                if hits:
                    lines = [f"[{h['topic'] or 'general'}] {h['content']}" for h in hits]
                    knowledge_block = "[LEARNED KNOWLEDGE — from past research]\n" + "\n---\n".join(
                        lines
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
        self._surfaced_paths = []  # reset each turn; reinforced in _post_turn
        try:
            from kai.memory import tree as _mtree

            uid = str(self.user_id)
            if _mtree.count_facts(uid) == 0:
                return ""
            import numpy as _np

            from kai.memory import loop as _memloop

            q = _np.asarray(query_emb, dtype=_np.float32) if query_emb else None
            block, flags, surfaced = _memloop.gather_context(uid, q)
            self._surfaced_paths = surfaced  # nodes used this turn — reinforce post-turn
            flow_rec.record(trace_id, "memory_tree", block=block, flags=len(flags))
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
        excluded = self._disabled_tools or None  # tools the user turned off
        gate = _query_needs_tools(user_input, history)
        # Optional semantic gate: the handoff router classified this turn from
        # its embedding — when it says tool/researcher, open the gate even if
        # the keyword regex missed. New tools then work without new keywords.
        if not gate and cfg.SEMANTIC_TOOL_GATE and handoff_mode in ("tool", "researcher"):
            log.debug(f"[tool gate] keyword miss, semantic hit ({handoff_mode})")
            gate = "direct"
        if not gate:
            # A durable fact stated in passing ("I work as a welder") is not a
            # request, so it never trips the keyword gate and the turn used to get
            # no tools at all — the model could not file it and narrated success
            # instead (measured 2026-08-07). Keep the memory-write tools available
            # even on a no-tool turn; nothing else survives a closed gate.
            return self._add_always_available([], excluded) or None

        # Follow-up turns ("yes", "run that again") carry no meaning of their
        # own — selecting categories from their embedding picks arbitrary
        # tools (this is how "yes" once produced a hallucinated shell tool).
        # Select with the embedding of the message that carried the intent.
        # Prefer USER messages: assistant replies are full of keyword noise
        # (every temps report says "CPU"/"GPU") and embed off-category.
        selection_emb = query_emb
        if gate == "follow_up":
            recent = history[-4:]
            candidates = [
                m
                for m in reversed(recent)
                if m.get("role") == "user" and _TOOL_SIGNALS.search(m.get("content", ""))
            ]
            if not candidates:
                candidates = [
                    m for m in reversed(recent) if _TOOL_SIGNALS.search(m.get("content", ""))
                ]
            if candidates:
                try:
                    from kai.llm.embed import embed as _fast_embed

                    selection_emb = _fast_embed(candidates[0]["content"][:500])
                except Exception:
                    pass

        if self._tool_index and selection_emb:
            try:
                # top_k=4, not 2. Measured 2026-09-07 on the composition corpus
                # (60 runs, gemma4:12b): composed 47% at k=2, 68% at k=4, 32% with
                # the gate fully open. Selection — not context budget — is the
                # ceiling; 78 schemas is 9.2% of the window and still the worst arm.
                tools_schema = self.tool_registry.select_tools_by_category(
                    selection_emb, self._tool_index, top_k=4, exclude=excluded
                )
            except Exception:
                tools_schema = self.tool_registry.get_schema(exclude=excluded)
        else:
            tools_schema = self.tool_registry.get_schema(exclude=excluded)

        tools_schema = self._add_always_available(tools_schema, excluded)

        # Inject skill schemas so the model can call skills as tools (skill.name)
        if tools_schema and self.skill_registry:
            tools_schema = list(tools_schema) + self._skill_schemas()
        return tools_schema

    def _add_always_available(self, schema: list[dict], excluded: set[str] | None) -> list[dict]:
        """Append cfg.ALWAYS_AVAILABLE_TOOLS the category gate didn't already pick.

        The gate ranks by topic and so cannot see "this is worth remembering" — a
        durable fact stated in passing routes to whatever its subject matter is, and
        the memory tools are never offered. Honors the user's disabled-tool list.
        """
        wanted = [t for t in cfg.ALWAYS_AVAILABLE_TOOLS if not (excluded and t in excluded)]
        if not wanted:
            return schema
        present = {t.get("function", {}).get("name") for t in schema}
        missing = [t for t in wanted if t not in present]
        if not missing:
            return schema
        try:
            return list(schema) + self.tool_registry.schema_for(missing)
        except Exception:
            return schema

    def _ground_evidence(
        self,
        messages: list[dict],
        tools_used: list[str],
        on_status: "Callable[[str], None] | None" = None,
        trace_id: str = "",
        fast_path: bool = False,
    ) -> None:
        """Pin the final answer to what the tools actually returned.

        One tool → light grounding (citation fence + evidence excerpt).
        Multiple tools → two-phase: extract facts first, then write the
        response. Separates reasoning (what did tools return?) from reporting
        (how to tell the user) — reduces confabulation.
        """
        if not tools_used:
            return
        evidence_text = _evidence_from(messages)
        if not evidence_text:
            return

        # The fast path matched a no-argument tool deterministically and its output
        # IS the answer, so there is nothing to confabulate between evidence and
        # reply — and it exists precisely to serve a turn without a model round.
        # Spending one here would undo the thing it is for. check_grounded still
        # runs afterwards, so the turn is not unchecked.
        if not fast_path and len(set(tools_used)) >= _FACT_EXTRACT_THRESHOLD:
            if on_status:
                on_status("Analyzing results...")
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Summarize the factual findings from the tool results above. "
                        "One bullet per finding. Only include data the tools returned. "
                        "No interpretation, no speculation, no training-data fill-in."
                    ),
                }
            )
            fact_resp = self._chat(messages, think=False, temperature=TEMPERATURE_REASON)
            facts = fact_resp.get("message", {}).get("content", "").strip()
            _, facts = _strip_thinking(facts)
            flow_rec.record(trace_id, "grounding", mode="two-phase", facts=facts)
            if facts:
                messages.append({"role": "assistant", "content": facts})
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Good. Now write your response to the user based ONLY on "
                            "the verified facts above. Use your natural voice. "
                            "Do not add data beyond what was extracted. "
                            "If the user needs information you don't have, say so."
                        ),
                    }
                )
        else:
            flow_rec.record(trace_id, "grounding", mode="light", evidence_chars=len(evidence_text))
            messages.append({"role": "system", "content": EVIDENCE_PROMPT + evidence_text})

    def _checked_answer(
        self,
        messages: list[dict],
        full_text: str,
        clean_text: str,
        user_input: str,
        tools_used: list[str],
        trace_id: str,
    ) -> Generator[tuple[str, bool, dict], None, tuple[str, str]]:
        """Deterministic checks on a buffered tool-turn answer; one silent retry on a flag.

        Ordered by damage: work reported that no tool did, then hedging despite the
        evidence, then numbers no tool returned. Returns (full_text, clean_text).
        """
        from kai.memory import cerebellum as _cb

        v = _cb.CerebellarResult(_cb.Verdict.CLEAR, "no per-tool results (crew)", 0.0)
        nudge, kind = "", ""
        executed = _executed_results(messages)
        if executed:
            v = _cb.check_claims_backed(clean_text, executed)
            nudge, kind = COMMITMENT_NUDGE.format(unbacked=v.reason), "unbacked_claim"
        if v.verdict < _cb.Verdict.FLAG:
            v = _cb.verify_answer(clean_text, user_input, tools_used)
            nudge, kind = ANSWER_REVERIFY_NUDGE, "hedge"
        if v.verdict < _cb.Verdict.FLAG:
            v = _cb.check_grounded(clean_text, _evidence_from(messages, limit=None))
            nudge, kind = FABRICATION_NUDGE.format(stray=v.reason), "fabrication"
        _cb.log_result("answer", "verify", v, self.user_id, output_snippet=clean_text[:500])
        if v.verdict < _cb.Verdict.FLAG:
            return full_text, clean_text
        flow_rec.record(trace_id, "answer_reverify", check=kind, reason=v.reason)
        messages.append({"role": "system", "content": nudge})
        full_text, _ = yield from self._stream_answer(
            messages, think=False, forward_thinking=False, forward_tokens=False
        )
        _, clean_text = _strip_thinking(full_text)
        return full_text, clean_text

    def _stream_answer(
        self,
        messages: list[dict],
        think: bool,
        forward_thinking: bool,
        forward_tokens: bool = True,
    ) -> Generator[tuple[str, bool, dict], None, tuple[str, bool]]:
        """Stream one model response, yielding (token, False, meta) chunks.

        The single home for the consume loop that the final answer, the
        empty-response retry, greetings, and confirmed-tool replies all share.
        Returns (full_text, had_think) — grab it with
        ``text, had_think = yield from self._stream_answer(...)``.
        forward_thinking=True passes think tokens/blocks through to the UI;
        False swallows them. forward_tokens=False BUFFERS the answer (collects the
        text without emitting or yielding it) so the caller can verify it before it
        reaches the user, then reveal it — used by the tool-turn grounding check so
        a hedge is caught before it's shown. Honors the Stop button: a set cancel
        flag ends the stream early and keeps whatever was generated.
        """
        _tokens: list[str] = []
        had_think = False
        for token, done, meta in self._chat_stream(
            messages,
            think=think,
            temperature=self._final_temp,
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
            if forward_tokens:
                if self.session_id:
                    events.emit(events.EVENT_STREAM_TOKEN, self.session_id, token=token)
                yield token, False, {}
        return "".join(_tokens), had_think

    def _fallback_text(self, tools_used: list[str]) -> str:
        """Last-resort response text when the model produced nothing visible."""
        if self._cancel.is_set():
            return "[stopped]"
        if tools_used:
            ran = ", ".join(dict.fromkeys(tools_used))
            return (
                f"{_NO_REPLY_PREFIX} I ran {ran} but couldn't turn the result into an answer. "
                "Ask again, or open the activity log to see what came back."
            )
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
        turn_meta: dict | None = None,
    ) -> tuple[int | None, int]:
        """Commit a finished turn — the single end-of-turn path for every exit.

        The trace keeps the raw text (with <think> tags) for debugging;
        history and the sessions DB get the clean version. Background
        post-turn work is submitted here, BEFORE the caller's final yield,
        so a consumer that stops iterating at done=True can't skip it.
        Returns (assistant message id | None, latency_ms) — id is None if
        persistence failed.
        """
        latency_ms = int((time.monotonic() - turn_start) * 1000)
        self._record_trace(trace_id, user_input, context, tools_used, raw_text, turn_start)
        turns = [{"role": "user", "content": user_input}]
        # Don't keep a pure failure placeholder in replayed history — it would
        # be mimicked next turn. The user turn stays so context isn't lost.
        if not _is_failure_marker(clean_text):
            turns.append({"role": "assistant", "content": clean_text})
        self._history.extend(turns)
        msg_id = self._persist_turn(user_input, clean_text, latency_ms, turn_meta)
        # The first reply of a session delivers the welcome-back note /
        # briefing — mark them consumed so they aren't re-injected next time.
        if self._history.turn_count <= 1:
            self._mark_session_notes_delivered()
        if self.session_id:
            events.emit(
                events.EVENT_STREAM_END,
                self.session_id,
                tools_used=tools_used,
                duration=round(latency_ms / 1000, 3),
            )
        # commit + learn + compression: runs off the hot path
        self._bg_pool.submit(self._post_turn, user_input, clean_text)
        return msg_id, latency_ms

    @staticmethod
    def _done_meta(msg_id: int | None, latency_ms: int, turn_meta: dict | None = None) -> dict:
        """Build the meta payload for the terminal done=True yield."""
        meta = dict(turn_meta or {})
        if msg_id:
            meta.update({"message_id": msg_id, "latency_ms": latency_ms})
        return meta

    def _emit_status(self, label: str, on_status: "Callable[[str], None] | None" = None) -> None:
        """Send a status label to both UIs: CLI callback + web event bus."""
        if on_status:
            on_status(label)
        if self.session_id:
            events.emit(events.EVENT_STATUS, self.session_id, label=label)

    def _mark_session_notes_delivered(self) -> None:
        """One-shot session notes (welcome-back, briefing) are consumed on
        delivery — mark them so the next session starts clean."""
        try:
            from kai.memory.context import (
                mark_briefing_delivered,
                mark_welcome_back_delivered,
            )

            mark_welcome_back_delivered(user_id=self.user_id)
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
        started = time.monotonic()
        result = self._engine._execute_tool_traced(tool_name, tool_args, trace_id, confirmed=True)
        receipt = tool_receipt(
            tool_name, tool_args, result, int((time.monotonic() - started) * 1000)
        )
        receipt["call_id"] = pending.get("call_id")  # the card that approved it shows done/failed
        self._turn_receipts.append(receipt)
        yield "", False, {"tool_result": receipt}

        # Build messages with the tool result injected
        context = (
            self.memory.render_context(
                query=user_input,
            )
            + GROUNDING_RULE
        )
        history = self._history.window(_HISTORY_HARD_CAP)

        evidence = str(result.get("output", ""))[:3000]
        messages: list[dict] = [
            {"role": "system", "content": context},
            *history,
            {"role": "user", "content": user_input},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": tool_name, "arguments": tool_args}}],
            },
            {"role": "tool", "content": json.dumps(result)},
            {"role": "system", "content": EVIDENCE_PROMPT + evidence},
        ]

        self._emit_status("Responding...", on_status)
        # Buffered and checked like any tool turn. This path used to stream unchecked,
        # and "The update is applied" shipped for a persona edit that wrote nothing (T1-43).
        full_text, _ = yield from self._stream_answer(
            messages,
            think=False,
            forward_thinking=False,
            forward_tokens=False,
        )
        _, clean_text = _strip_thinking(full_text)
        if clean_text and not self._cancel.is_set():
            full_text, clean_text = yield from self._checked_answer(
                messages, full_text, clean_text, user_input, tools_used, trace_id
            )
        flow_rec.record(trace_id, "final_answer", confirmed_tool=tool_name, text=full_text)
        if not clean_text:
            clean_text = self._fallback_text(tools_used)
            flow_rec.record(trace_id, "fallback", text=clean_text)
        if self.session_id:
            events.emit(events.EVENT_STREAM_TOKEN, self.session_id, token=clean_text)
        yield clean_text, False, {}

        turn_meta = {
            "receipts": self._turn_receipts,
            "grounding": self._grounding_for(messages, clean_text, parked=False),
        }
        msg_id, latency_ms = self._finalize_turn(
            user_input=user_input,
            clean_text=clean_text,
            raw_text=full_text,
            trace_id=trace_id,
            context=context,
            tools_used=tools_used,
            turn_start=turn_start,
            turn_meta=turn_meta,
        )
        yield "", True, self._done_meta(msg_id, latency_ms, turn_meta)

    def _persist_turn(
        self,
        user_input: str,
        response: str,
        latency_ms: int | None = None,
        meta: dict | None = None,
    ) -> int | None:
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
            sessions.append_message(
                self.session_id, "user", user_input, self._history.turn_order, user_id=self.user_id
            )
            msg_id = sessions.append_message(
                self.session_id,
                "assistant",
                response,
                self._history.turn_order + 1,
                user_id=self.user_id,
                latency_ms=latency_ms,
                meta=meta,
            )
            self._history.advance_turn_order(2)
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
            trace_log.record(
                trace_log.TraceEntry(
                    trace_id=trace_id,
                    timestamp=datetime.now().isoformat(),
                    user_input=user_input,
                    model=self.model,
                    context_len=len(context),
                    tool_calls=tools_used,
                    elapsed_ms=int((time.monotonic() - start_time) * 1000),
                    response_len=len(response),
                    user_id=self.user_id,
                )
            )
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
            log.debug(f"[tool index] {len(self._tool_index)} categories indexed")
        except Exception as exc:
            log.debug(f"[tool index] build failed (will use full schema): {exc}")
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
            log.debug(f"[memory router] {len(self.memory._domain_index)} domains indexed")
        except Exception as exc:
            log.debug(f"[memory router] build failed (will inject everything): {exc}")
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
            log.debug("[handoff router] seeded and ready")
        except Exception as exc:
            log.debug(f"[handoff router] init failed: {exc}")
        finally:
            self._handoff_router_ready = True

    # ── Conversational learning ──────────────────────────────────────────────

    def _drain_pending_learning(self) -> None:
        """Process the durable extraction queue, but only while no user turn is in
        flight — so a learning LLM call never queues a generation behind the user's
        next request. Rows survive process exit (kai.memory.pending), so anything
        left queued by a killed or short session re-drains here next time we're idle.
        """
        if foreground_busy():
            return
        # One drain at a time per account: two would extract the same queued row twice.
        if not self._drain_lock.acquire(blocking=False):
            return
        try:
            self._drain_queue()
        finally:
            self._drain_lock.release()

    def _drain_queue(self) -> None:
        while not foreground_busy():
            batch = pending.peek(self.user_id, limit=5)
            if not batch:
                break
            for row_id, user_text, assistant_text in batch:
                if foreground_busy():
                    return
                try:
                    self._extract_knowledge(user_text, assistant_text)
                except Exception:
                    if cfg.DEBUG:
                        import traceback

                        traceback.print_exc()
                # Delete after the attempt (success or handled failure) so a poison
                # row can't wedge the queue — durability is about surviving restarts,
                # not retrying a broken exchange forever.
                pending.delete(row_id)

    def _post_turn(self, user_input: str, assistant_text: str) -> None:
        """
        Background post-turn processing: persist turn + compress history +
        extract knowledge. Runs in the background pool — never blocks the user.
        """
        self.memory.commit_turn(user_input, assistant_text)
        # Reinforce the tree nodes that were surfaced into this turn's context —
        # frequency feeds the scorer's boost and offsets decay. Bounded by
        # scorer._FREQ_CAP. Runs here (background) so it stays off the hot path;
        # cleared so a later turn that renders no tree block can't re-bump stale paths.
        if self._surfaced_paths:
            from kai.memory import loop as _memloop

            uid = str(self.user_id)
            for path in self._surfaced_paths:
                _memloop.record_node_used(uid, path)
            self._surfaced_paths = []
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
        # Keep the monotonic turn counter (read elsewhere, e.g. greeting logic).
        self._history.bump_turn_count()
        # Queue this exchange for knowledge extraction. DURABLE: the queue lives in
        # SQLite (kai.memory.pending), so a short session or a hard kill can't drop
        # it. Every non-trivial turn is eligible — the drain runs the actual LLM call
        # only when no user turn is in flight, so it never queues behind the user.
        if learning_enabled(self.user_id) and not _trivial_exchange(user_input, assistant_text):
            pending.enqueue(self.user_id, user_input, assistant_text, self.session_id)
        self._drain_pending_learning()

        # Crash-survival: refresh the recall checkpoint each turn (cheap file
        # write, no LLM). A clean shutdown supersedes it; a hard kill leaves it
        # for promotion on the next startup.
        try:
            from kai.core.sleep import checkpoint_session

            checkpoint_session(self._history.snapshot(), user_id=self.user_id)
        except Exception:
            pass

    def _extract_knowledge(self, user_text: str, assistant_text: str) -> None:
        """
        Ask the model what it learned from this exchange, then file each fact as a
        node in the SCORED memory tree (kai/memory/tree.py) — the substrate the
        self-improving loop (loop.gather_context → scorer) actually reads. The model
        tags each fact with a category; extractor.save_fact_to_tree owns the path,
        scores, decay, and near-duplicate reinforcement.

        Pre-filtered: skips trivial exchanges (greetings, one-word responses)
        to avoid unnecessary Ollama calls that would queue and delay the next turn.
        """
        # Pre-filter: skip short/trivial exchanges (belt-and-suspenders — the
        # enqueue path filters too, but drained rows may predate that).
        if _trivial_exchange(user_text, assistant_text):
            return

        exchange = f"User: {user_text}\nKai: {assistant_text}"
        try:
            resp = self._chat_local(
                [{"role": "user", "content": f"{LEARN_PROMPT}\n\n{exchange}"}],
                think=False,
                temperature=TEMPERATURE_REASON,
            )
        except Exception:
            return  # Ollama down or model unloaded — skip silently

        result = resp.get("message", {}).get("content", "").strip()
        _, result = _strip_thinking(result)

        if not result or result.upper() == "NONE":
            return

        from kai.memory.extractor import save_fact_to_tree

        embed_fn = self.get_embed_fn()
        saved = 0
        for line in result.splitlines():
            line = line.strip().lstrip("-•* 0123456789.)").strip()
            if not line or len(line) <= 10:
                continue
            # Expect "CATEGORY | fact"; tolerate a bare line by defaulting the category.
            category, sep, fact = line.partition("|")
            if not sep:
                category, fact = "knowledge", line
            action, _path = save_fact_to_tree(
                self.user_id, category.strip(), fact.strip(), embed_fn=embed_fn
            )
            if action in ("created", "reinforced"):
                saved += 1

        if saved and self.session_id:
            events.emit(
                events.EVENT_MEMORY_WRITE,
                self.session_id,
                entries=saved,
                source="knowledge_extraction",
            )
        if saved:
            log.debug(f"[learn] saved/reinforced {saved} tree facts")

    def get_embed_fn(self):
        from kai.llm.embed import embed as _fast_embed

        return _fast_embed

    def _summarize_messages(self, msgs: list[dict]) -> str:
        """LLM-compress a list of turns into one past-tense paragraph.

        Shared by live history compression and clear-time archiving so the
        formatting (and the 800-char-per-message cap) can't drift apart.
        Returns "" on any failure — callers treat that as "skip, keep history".
        """
        # An earlier summary is already compact and is replaced by this one: pass it whole.
        raw = "\n\n".join(
            f"[earlier summary]: {m['content']}"
            if is_summary(m)
            else f"[{m['role']}]: {m.get('content', '')[:800]}"
            for m in msgs
        )
        try:
            resp = self._chat_local(
                _build_compress_messages(raw), think=False, temperature=TEMPERATURE_REASON
            )
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
        Compress the session history when it grows too large for the context window.

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
        keep_n = HISTORY_COMPRESS_KEEP * 2  # user + assistant = 2 messages per exchange
        to_compress = self._history.begin_compression(HISTORY_CHAR_LIMIT, keep_n)
        if to_compress is None:
            return  # nothing to do (already compressing / under limit / too short)
        if not to_compress:
            self._history.abort_compression()
            return

        try:
            summary = self._summarize_messages(to_compress)
            if not summary:
                return  # compression failed — history is still intact (never trimmed)
            # Atomic swap inside HistoryManager: drop the compressed prefix, inject
            # the summary. Safe because only appends happened during the LLM call.
            self._history.commit_compression(summary)
            # Archive to episodic DB off the hot path — nothing is lost.
            self._bg_pool.submit(self.memory.archive_history, summary)
        finally:
            self._history.abort_compression()  # idempotent — clears the in-progress flag

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
