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
import urllib.request
from collections.abc import Callable, Generator
from datetime import datetime
from typing import TYPE_CHECKING

import kai.config as cfg
from kai.config import (
    CHAT_MODEL, EMBED_MODEL, REASONING_MODEL,
    OLLAMA_BASE_URL, CONTEXT_WINDOW, TEMPERATURE_TOOL, TEMPERATURE_FINAL,
    HISTORY_CHAR_LIMIT, HISTORY_COMPRESS_KEEP, LEARN_FROM_CONVERSATION,
)
from kai.memory.manager import MemoryManager
from kai import trace as trace_log
from kai import sessions

if TYPE_CHECKING:
    from kai.tools.registry import ToolRegistry

MAX_TOOL_ROUNDS   = 8   # increased to support multi-step tasks (scan → restore point → fix)
_HISTORY_HARD_CAP = 60  # safety ceiling — compression normally keeps history much smaller

# ── Tool signal detection ─────────────────────────────────────────────────────
# Categorized keyword lists composed into one regex at module load.
# Adding a new tool category = add a list entry here.

_TOOL_KEYWORDS_SINGLE = [
    # System / hardware
    "time", "date", "clock", "weather", "cpu", "gpu", "ram", "memory", "disk",
    "drive", "ssd", "hdd", "hardware", "storage", "monitor", "upgrade",
    "startup", "boot", "autostart", "autorun", "space",
    # Processes / performance
    "process", "processes", "running", "usage", "load", "speed", "fan", "volt",
    "spec", "specs", "performance", "slow", "fast", "laggy", "lagging",
    # Network
    "network", "wifi", "ethernet", "dns", "gateway", "ping", "tracert",
    "traceroute", "latency", "jitter", "bandwidth", "connectivity",
    # PC / system
    "pc", "computer", "machine", "check", "system",
    # Errors / crashes
    "crash", "crashes", "log", "logs", "error", "errors",
    # Notes / search
    "note", "notes", "remind", "search", "find", "web", "internet",
    # Steam / games
    "steam", "benchmark", "benchmarks", "fps",
    # Hardware parts
    "motherboard", "mobo", "psu", "nvme", "sata",
    "ryzen", "threadripper", "epyc", "xeon", "geforce", "radeon", "arc",
    # Documents
    "document", "pdf",
]

_TOOL_KEYWORDS_COMPOUND = [
    # Temperature / temps (word-boundary safe)
    r"temp(?:erature)?",
    # Lag variants
    r"lag(?:s|ging|gy)?",
    # Updates / patches
    r"update(?:s)?", r"patch(?:es)?",
    # Frame rate
    r"frame\s*rate",
    # Event / Windows logs
    r"event\s*log", r"windows\s*log", r"system\s*error",
    # IP / Wi-Fi
    r"ip\s*address", r"wi-fi", r"internet\s*connection",
    # Windows update
    r"windows\s*update",
    # File size triggers
    r"large\s*file", r"big\s*file", r"folder\s*size",
    r"old\s*file", r"recent\s*file",
    r"free\s*up", r"clean\s*up", r"cleanup",
    # Connection
    r"connection\s*test", r"slow\s*internet", r"high\s*ping", r"packet\s*loss",
    # Hardware upgrade
    r"should\s+i\s+(?:buy|get|upgrade)", r"worth\s+(?:it|buying|getting)",
    r"performance\s+(?:gain|delta|improvement)",
    r"compatible|compatibility|socket|am[45]|lga\d+|ddr[45]|pcie",
    r"cpu\s+cooler|aio\s+cooler|power\s+supply",
    r"m\.2|gen\s*[345]|pcie\s*[45]",
    r"versus|comparison|better\s+than|faster\s+than",
    # DLL / error codes
    r"\w+\.dll",
    # Document triggers
    r"docx?|word\s+doc|uploaded?\s+file|my\s+file|that\s+file",
    # Gaming triggers
    r"gaming\s+time|game\s+time|game\s+mode|ready\s+to\s+play|pre.?game",
]

_TOOL_PHRASE_PATTERNS = [
    # Question patterns
    r"what.{0,20}(?:running|using|taking|my)",
    r"how.{0,20}(?:perform|fast|slow|much)",
    r"can you.{0,20}(?:check|see|look|find|get|test)",
    r"my (?:pc|cpu|gpu|ram|disk|specs|system|network|ip|files?|drive|internet|connection|ping)",
    # Action + target patterns
    r"(?:free|clear|clean).{0,15}(?:space|storage|disk)",
    r"what.{0,20}(?:eating|using).{0,10}(?:space|disk|storage)",
    r"(?:test|check|diagnose).{0,20}(?:network|internet|connection|ping|lag)",
    r"i.{0,10}(?:lag|lagging|ping|connection).{0,20}(?:game|games|server|high|bad)",
    r"(?:speed\s*up|fix|optimize|clean\s*up|scan|tune|boost).{0,25}(?:pc|computer|system|windows|disk)",
    r"(?:my\s+(?:pc|computer|system)).{0,30}(?:slow|lag|problem|issue|wrong|broken|fix)",
    r"(?:what.{0,10}wrong|health\s*check|diagnose).{0,20}(?:pc|computer|system)",
    r"(?:restore\s*point|undo\s*changes|rollback|revert\s*changes)",
    r"make.{0,15}(?:pc|computer|system).{0,15}(?:faster|better|run)",
    # File read/write triggers
    r"(?:read|open|show|view|cat|print).{0,20}(?:file|code|script|config|log|\.py|\.txt|\.json|\.md|\.log|\.yaml|\.toml)",
    r"(?:what.{0,15}in|contents?\s+of|look\s+at|show\s+me).{0,20}(?:file|folder|directory|\.py|\.txt|\.json|\.md)",
    r"(?:list|browse|explore).{0,15}(?:files?|folder|directory|dir\b)",
    r"\.(?:py|txt|json|md|log|yaml|yml|toml|ini|cfg|js|ts|html|css|sh|bat|ps1)\b",
    # Search / current-info triggers
    r"(?:latest|newest|most\s+recent|current)\s+(?:game|news|update|patch|version|release|trailer|price)",
    # Workspace / file-write triggers
    r"(?:write|create|save|make).{0,20}(?:file|script|code|txt|log|config|\.py|\.txt|\.json|\.md)",
    r"(?:append|add\s+to|add\s+a\s+line).{0,20}(?:file|notes?|log)",
    r"(?:edit|change|update|fix).{0,20}(?:file|script|code|config)\b",
    r"(?:clone|pull|download).{0,20}(?:repo|repository|git|code\s*base)",
    r"git\s+(?:clone|pull|push|status|log|commit)",
    # Game/trending triggers
    r"what.{0,20}(?:game|games).{0,20}(?:popular|trending|hot|new|out|good|recommend|play)",
    r"what.{0,15}(?:new|out|trending|popular|hot)\s+(?:right\s+now|this\s+(?:week|month|year)|in\s+202[5-9])",
    r"(?:right\s+now|these\s+days|in\s+202[5-9]|currently).{0,20}(?:popular|trending|good|out|best|top)",
    r"(?:price|cost|how\s+much).{0,20}(?:is|does|for|costs?)",
    r"news\s+about|latest\s+news|what\s+happened\s+to",
    r"(?:is|has).{0,10}(?:been\s+)?(?:released|out|announced|launched|available).{0,15}(?:yet|now)?",
    # Error code / DLL fault triggers
    r"0x[0-9a-fA-F]{4,}",
    r"(?:fix|solve|debug).{0,25}(?:crash|error|fault|exception|freeze|hang)",
    r"(?:crash|error|fault|exception).{0,25}(?:fix|solve|debug|help|what)",
    # Steam / game library triggers
    r"my\s+(?:installed\s+)?(?:games?|game\s+library|steam\s+library)",
    r"installed\s+games?|games?\s+installed",
    r"what\s+games?\s+(?:do\s+i\s+have|i\s+have|i\s+own|are\s+on)",
    # Hardware comparison triggers
    r"(?:should\s+i|is\s+it\s+worth|do\s+i\s+need).{0,30}(?:new|upgrade|replace|buy|get)",
    r"(?:will\s+it\s+(?:fit|work|be\s+compatible)|does\s+it\s+(?:support|work\s+with))",
    # Document / RAG triggers
    r"(?:search|find|look).{0,20}(?:in|through|inside|across).{0,20}(?:document|file|pdf|upload)",
    r"(?:what.{0,15}in|contents?\s+of|summarize|explain).{0,20}(?:document|pdf|file|upload)",
    r"(?:list|show).{0,15}(?:uploaded?|my)\s+(?:file|document|pdf)",
    r"(?:delete|remove).{0,15}(?:document|file|pdf)",
]

# Compose single keywords into \b(word1|word2|...)\b, then join with phrase patterns.
_single_pattern = r"\b(" + "|".join(re.escape(w) for w in _TOOL_KEYWORDS_SINGLE) + r")\b"
_compound_pattern = r"\b(" + "|".join(_TOOL_KEYWORDS_COMPOUND) + r")\b"
_phrase_pattern = "|".join(_TOOL_PHRASE_PATTERNS)
_TOOL_SIGNALS = re.compile(
    f"{_single_pattern}|{_compound_pattern}|{_phrase_pattern}",
    re.IGNORECASE,
)

# Short confirmations that delegate a task — "go ahead", "yes do it", "proceed", etc.
_FOLLOW_UP_SIGNALS = re.compile(
    r"\b(go\s*ahead|proceed|do\s*(it|that|what|them)|yes(\s*please)?|sure(\s*thing)?|"
    r"ok(ay)?|sounds?\s*good|continue|carry\s*on|"
    r"you\s*can(\s*do)?|please\s*do|do\s*what\s*you\s*(need|want|think))\b",
    re.IGNORECASE,
)

# Detects when Kai herself signals she wants to retry — e.g. "Let me try a different approach"
# When this fires after a failed tool call, the loop gives her one more round automatically.
_KAI_RETRY_SIGNALS = re.compile(
    r"let\s+me\s+(try|check|look|see|investigate|figure|attempt|search|test)|"
    r"i('ll| will)\s+(try|check|look|see|attempt|investigate|search|test)|"
    r"let\s+me.{0,40}(again|another|different|instead)|"
    r"(trying|attempt(ing)?)\s+(a\s+)?(different|another|alternative)",
    re.IGNORECASE,
)

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


# ── Auto-think: skip reasoning for trivial prompts ────────────────────────────
# When reasoning mode is ON, this classifier decides per-prompt whether to
# actually send think=True to Ollama. Simple greetings, single-word replies,
# and casual chat skip thinking entirely — saving 10-30s of wasted compute.
# Complex queries (multi-step, debugging, comparisons, analysis) keep it on.

# Patterns that ALWAYS skip thinking (cheap chat)
_TRIVIAL_PATTERNS = re.compile(
    r"^("
    # Greetings
    r"h(ello|i|ey|owdy|iya|eya?)(\s+(there|kai|buddy|dude|bro|man|friend))?"
    r"|yo\b"
    r"|sup\b"
    r"|what'?s?\s*up"
    r"|good\s+(morning|afternoon|evening|night)"
    r"|g'?(morning|night)"
    r"|gm\b|gn\b"
    # Farewells
    r"|bye\b|goodbye|see\s*ya|later|night|cya|peace|ttyl"
    # Acknowledgements
    r"|ok(ay)?|k\b|sure|yep|yup|yeah|yes|no|nah|nope|mhm|hmm"
    r"|thanks?(\s*(you|a?\s*lot|so\s+much|kai))?"
    r"|ty\b|thx\b"
    r"|got\s*it|understood|makes?\s*sense|fair\s*(enough)?"
    r"|nice|cool|neat|sick|dope|bet|based|lol|lmao|haha|rofl"
    r"|wow|whoa|damn|dang|huh|oh|ah|oof|rip"
    # Simple identity / small talk
    r"|how\s+are\s+you(\s+doing)?(\s+today)?"
    r"|how'?s?\s*it\s+going"
    r"|what\s+are\s+you(\s+up\s+to)?"
    r"|who\s+are\s+you"
    r"|what'?s?\s+your\s+name"
    r"|tell\s+me\s+(about\s+)?yourself"
    r"|you\s+there\??"
    r"|are\s+you\s+(awake|alive|there|ready|up)"
    r")[\s?!.,]*$",
    re.IGNORECASE,
)

# Patterns that ALWAYS need thinking (complex reasoning)
_COMPLEX_PATTERNS = re.compile(
    r"("
    r"explain.{0,30}(how|why|difference|between|vs|versus)"
    r"|compare|contrast|analyze|evaluat"
    r"|step.by.step|walk\s+me\s+through"
    r"|pros?\s+and\s+cons?"
    r"|debug|diagnos|troubleshoot"
    r"|why\s+(is|does|did|would|should|can'?t|won'?t|isn'?t|doesn'?t|aren'?t)"
    r"|how\s+(would|should|could|do)\s+(?:i|you|we).{10,}"
    r"|what.{0,10}(best|optimal|right|correct)\s+(way|approach|method|strategy)"
    r"|design|architect|implement|refactor|optimize"
    r"|trade.?off|down.?side|caveat|implication"
    r"|write\s+(?:a\s+)?(?:function|class|script|program|code|algorithm)"
    r"|fix\s+(?:this|the|my)\s+(?:code|bug|error|issue|problem)"
    r")",
    re.IGNORECASE,
)


def _query_needs_thinking(query: str) -> bool:
    """Decide whether a query warrants chain-of-thought reasoning.

    Returns False for trivial prompts (greetings, acks, small talk).
    Returns True for complex prompts (analysis, debugging, comparisons).
    For ambiguous prompts, uses word count as a heuristic — longer = more likely complex.
    """
    stripped = query.strip()
    if not stripped:
        return False
    # Fast path: trivial patterns never need thinking
    if _TRIVIAL_PATTERNS.match(stripped):
        return False
    # Fast path: complex patterns always need thinking
    if _COMPLEX_PATTERNS.search(stripped):
        return True
    # Heuristic: very short prompts (< 8 words) are usually casual
    word_count = len(stripped.split())
    if word_count < 8:
        return False
    return True


def _query_needs_tools(query: str, history: list[dict] | None = None) -> bool:
    if _TOOL_SIGNALS.search(query):
        return True
    # If this looks like a follow-up confirmation, check whether the last
    # assistant turn was tool-heavy — if so, tools are still needed.
    if history and _FOLLOW_UP_SIGNALS.search(query):
        for msg in reversed(history):
            if msg["role"] == "assistant":
                return bool(_TOOL_SIGNALS.search(msg["content"]))
    return False


# Friendly labels for tool status messages shown in the web UI.
_TOOL_LABELS: dict[str, str] = {
    "system.info":          "Checking system stats",
    "system.temps":         "Checking temperatures",
    "system.crashes":       "Checking crash logs",
    "system.gpu_crashes":   "Checking GPU crash history",
    "system.game_crashes":  "Searching for game crash logs",
    "pc.startup_programs":  "Checking startup programs",
    "pc.event_logs":        "Scanning event logs",
    "pc.network_info":      "Checking network",
    "pc.windows_updates":   "Checking for updates",
    "files.disk_usage":     "Analyzing disk usage",
    "files.find_large":     "Finding large files",
    "files.find_old":       "Finding old files",
    "files.recent":         "Finding recent files",
    "search.web":           "Searching the web",
    "weather.current":      "Checking weather",
    "notes.save":           "Saving a note",
    "notes.search":         "Looking up notes",
    "notes.list":           "Reading notes",
    "time.now":             "Checking the time",
    "network.ping":                  "Pinging host",
    "network.traceroute":            "Tracing route",
    "network.full_diagnostic":       "Running network diagnostic",
    "pc.deep_scan":                  "Running full system scan (~2 min)",
    "system.create_restore_point":   "Creating restore point",
    "system.clear_temp_files":       "Clearing temp files",
    "system.disable_startup_program":"Disabling startup program",
    "system.run_disk_cleanup":       "Running disk cleanup",
    "system.repair_files":           "Running system file repair (sfc /scannow)",
    "system.kill_process":           "Killing process",
    "files.read":                    "Reading file",
    "files.list":                    "Listing directory",
    "campaign.npc_save":             "Saving NPC",
    "campaign.event_log":            "Logging event",
    "campaign.quest_update":         "Updating quest",
    "campaign.recall":               "Searching campaign memory",
    "campaign.status":               "Loading campaign",
    "files.write":                   "Writing file",
    "files.append":                  "Appending to file",
    "files.edit":                    "Editing file",
    "workspace.git_clone":           "Cloning repository",
    "workspace.git_pull":            "Updating repository",
    "workspace.git_list_allowed":    "Listing allowed repos",
    "memory.get_detail":             "Reading full memory transcript",
    "memory.search_history":         "Searching past conversations",
    "memory.reflect":                "Writing a reflection",
    "memory.read_reflections":       "Reading past reflections",
    "docs.search":                   "Searching documents",
    "docs.list":                     "Listing documents",
    "docs.delete":                   "Removing document",
}


# ── Ollama HTTP client ─────────────────────────────────────────────────────────

class OllamaClient:
    def __init__(self, base_url: str = OLLAMA_BASE_URL):
        self.base_url = base_url.rstrip("/")

    def _base_payload(
        self, model: str, messages: list, think: bool, tools=None,
        temperature: float = TEMPERATURE_FINAL, keep_alive: str = "10m",
    ) -> dict:
        p: dict = {
            "model": model,
            "messages": messages,
            "keep_alive": keep_alive,
            "think": think,
            "options": {"num_ctx": CONTEXT_WINDOW, "temperature": temperature},
        }
        if tools:
            p["tools"] = tools
        return p

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str = CHAT_MODEL,
        think: bool = False,
        temperature: float = TEMPERATURE_TOOL,
        keep_alive: str = "10m",
    ) -> dict:
        """Non-streaming chat. Used for tool-call rounds."""
        payload = self._base_payload(model, messages, think, tools, temperature, keep_alive)
        payload["stream"] = False
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str = CHAT_MODEL,
        think: bool = False,
        temperature: float = TEMPERATURE_FINAL,
    ) -> Generator[tuple[str, bool, dict], None, None]:
        """
        Streaming chat. Yields (token, done, final_message).
        - token: the text chunk to print
        - done: True on the last chunk
        - final_message: full message dict (on done=True only)
        """
        payload = self._base_payload(model, messages, think, tools, temperature)
        payload["stream"] = True
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            in_think  = False
            think_buf: list[str] = []
            for raw_line in resp:
                line = raw_line.strip()
                if not line:
                    continue
                chunk = json.loads(line)
                done = chunk.get("done", False)
                msg = chunk.get("message", {})
                token = msg.get("content", "")

                if done:
                    yield "", True, msg
                    return

                # Ollama 0.6+ with think=True sends thinking in a separate
                # message.thinking field (not embedded as <think> tags in content).
                # Older builds / some models still use <think> tags in content.
                # Handle both.
                thinking_chunk = msg.get("thinking", "")
                if thinking_chunk:
                    think_buf.append(thinking_chunk)
                    continue

                # Legacy: <think>...</think> tags inside content stream
                if "<think>" in token:
                    in_think = True
                    after = token.split("<think>", 1)[1]
                    if after:
                        think_buf.append(after)
                    continue

                if in_think:
                    if "</think>" in token:
                        in_think = False
                        before = token.split("</think>", 1)[0]
                        think_buf.append(before)
                        yield "", False, {"think_block": "".join(think_buf).strip()}
                        think_buf = []
                    else:
                        think_buf.append(token)
                    continue

                # If we accumulated thinking chunks via message.thinking and
                # are now receiving content, flush the think buffer first.
                if think_buf and not in_think:
                    yield "", False, {"think_block": "".join(think_buf).strip()}
                    think_buf = []

                yield token, False, {}

    def embed(self, text: str, model: str = EMBED_MODEL) -> list[float]:
        payload = {"model": model, "input": text}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/embed",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result["embeddings"][0]

    def embed_batch(self, texts: list[str], model: str = EMBED_MODEL) -> list[list[float]]:
        """Embed a list of strings in one HTTP call. Returns one vector per input."""
        payload = {"model": model, "input": texts}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/embed",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result["embeddings"]

    def is_alive(self) -> bool:
        try:
            urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=3)
            return True
        except Exception:
            return False

    def installed_models(self) -> list[str]:
        with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=5) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return [m["name"] for m in result.get("models", [])]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _strip_thinking(text: str) -> tuple[str, str]:
    think_match = re.search(r"<think>(.*?)</think>", text, re.DOTALL)
    thinking = think_match.group(1).strip() if think_match else ""
    clean = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    return thinking, clean


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
    ):
        self.memory = memory
        self.tool_registry = tool_registry
        self.model = model
        self.ollama = ollama or OllamaClient()
        self._think = think
        self.user_id = user_id
        self._session_history: list[dict] = []  # rolling conversation turns for this session
        self._history_lock = threading.Lock()    # protects _session_history mutations
        self.session_id: str | None = None       # current persisted session UUID
        self._turn_order: int = 0                # monotonic counter for message ordering
        self.dm_mode: bool = False               # DM mode — injects [CAMPAIGN] block + campaign tools
        self._tool_index: dict[str, list[float]] = {}  # name → embedding vector, built lazily
        self._tool_index_ready: bool = False
        self._memory_router_ready: bool = False       # memory domain index built lazily
        self._compressing: bool = False               # prevents concurrent history compressions
        self._turn_count: int = 0                     # monotonic counter for learn-rate gating

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

        # Auto-think: when reasoning mode is ON, still skip it for trivial
        # prompts like "hello" to avoid 30s of wasted chain-of-thought.
        use_think = self._think and _query_needs_thinking(user_input)

        if on_status:
            on_status("Thinking...")

        self._maybe_compress_history(on_status=on_status)

        # ── Embed query once — shared by memory router + tool router ──────────
        # One Ollama embed call (~30ms) replaces separate calls scattered across
        # memory and tool routing. Memory router uses it to classify which memory
        # stores to activate; tool router uses it to pick relevant tool categories.
        self._ensure_memory_router()
        self._ensure_tool_index()
        query_emb: list[float] | None = None
        try:
            query_emb = self.ollama.embed(user_input)
        except Exception:
            pass  # fallback: both routers inject everything when embedding is None

        context = self.memory.render_context(
            query=user_input, dm_mode=self.dm_mode, query_embedding=query_emb
        )
        with self._history_lock:
            history = list(self._session_history[-_HISTORY_HARD_CAP:])
        messages: list[dict] = [
            {"role": "system", "content": context},
            *history,
            {"role": "user",   "content": user_input},
        ]
        # Select semantically relevant tools rather than injecting all 40 every round.
        # Paper "Less is More": filtering to top-K tools improves selection accuracy
        # 30-70% for small/quantized models and halves context size.
        # DM mode bypasses filtering — campaign calls are explicit in persona.md routines.
        tools_schema = None
        if self.tool_registry and (self.dm_mode or _query_needs_tools(user_input, history)):
            if self.dm_mode:
                tools_schema = self.tool_registry.get_schema()
            else:
                if self._tool_index and query_emb:
                    try:
                        tools_schema = self.tool_registry.select_tools_by_category(
                            query_emb, self._tool_index, top_k=2
                        )
                    except Exception:
                        tools_schema = self.tool_registry.get_schema()
                else:
                    tools_schema = self.tool_registry.get_schema()

        # ── Tool-call rounds (non-streaming) ──────────────────────────────────
        _escalated = False  # True after first tool error → full schema injected
        for round_num in range(MAX_TOOL_ROUNDS):
            if not tools_schema:
                break  # no tools → skip to streaming final answer

            resp = self.ollama.chat(
                messages, tools=tools_schema, model=self.model, think=use_think
            )
            msg = resp.get("message", {})

            # Emit tool-round thinking as a step to be shown inline before the
            # tool label in the activity log — not as a floating reasoning dropdown.
            tool_round_thinking = msg.get("thinking", "")
            if tool_round_thinking and use_think:
                yield "", False, {"think_step": True, "text": tool_round_thinking}

            if cfg.DEBUG:
                print(f"\n[{trace_id}] tool round={round_num} "
                      f"tool_calls={bool(msg.get('tool_calls'))}")

            if not msg.get("tool_calls"):
                content = msg.get("content", "")
                _, clean = _strip_thinking(content)
                final = clean or "[no response]"

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

                # Model chose not to use tools — return its response directly
                self._record_trace(trace_id, user_input, context, tools_used, final, turn_start)
                with self._history_lock:
                    self._session_history.append({"role": "user",      "content": user_input})
                    self._session_history.append({"role": "assistant", "content": final})
                self._persist_turn(user_input, final)
                yield final, False, {}
                yield "", True, {}
                # commit (embed round-trip) runs off the hot path
                threading.Thread(
                    target=self.memory.commit_turn,
                    args=(user_input, final),
                    daemon=True,
                ).start()
                return

            # Execute tool calls
            messages.append({
                "role": "assistant",
                "content": msg.get("content", ""),
                "tool_calls": msg["tool_calls"],
            })
            any_tool_error = False   # Python exception — tool completely failed
            any_soft_error = False   # Tool ran but output contains a Windows error code
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                tool_name = fn.get("name") or ""
                tools_used.append(tool_name)
                if on_status:
                    on_status(_TOOL_LABELS.get(tool_name, tool_name))
                result = self._execute_tool(tool_name, fn.get("arguments", {}), trace_id)
                if cfg.DEBUG:
                    print(f"[{trace_id}] TOOL: {tool_name} → {result}")
                messages.append({"role": "tool", "content": json.dumps(result)})
                # Hard error: the tool itself crashed (Python exception or no registry)
                if not result.get("success", True):
                    any_tool_error = True
                # Soft error: tool ran fine but the system returned a Windows error code.
                # (e.g. pc.windows_updates → "Windows Update check failed: 0x80240032")
                # Detected by the 0x hex-code pattern — unambiguous, no false positives.
                elif re.search(r"\b0x[0-9a-fA-F]{4,}\b", result.get("output", "")):
                    any_soft_error = True

            # Error escalation (paper "Less is More", Tier 2 fallback):
            # First failure → give the model the full tool set so it has every alternative.
            # Subsequent failures → push hard and let it exit gracefully if truly stuck.
            if any_tool_error:
                if not _escalated and self.tool_registry:
                    tools_schema = self.tool_registry.get_schema()
                    _escalated = True
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

        # ── Stream the final answer ───────────────────────────────────────────
        if on_status and tools_used:
            on_status("Responding...")

        _tokens: list[str] = []
        for token, done, meta in self.ollama.chat_stream(
            messages, tools=None, model=self.model, think=use_think
        ):
            if done:
                break
            # Think block — pass to UI as a separate event, don't add to response text
            if meta.get("think_block") is not None:
                yield "", False, {"think": True, "text": meta["think_block"]}
                continue
            _tokens.append(token)
            yield token, False, {}
        full_text = "".join(_tokens)

        # Strip any leaked <think> tags before persisting (matches non-streaming path).
        # Trace keeps the raw text for debugging; everything else gets the clean version.
        _, clean_text = _strip_thinking(full_text)
        clean_text = clean_text or full_text  # fallback if strip removed everything

        self._record_trace(trace_id, user_input, context, tools_used, full_text, turn_start)
        with self._history_lock:
            self._session_history.append({"role": "user",      "content": user_input})
            self._session_history.append({"role": "assistant", "content": clean_text})
        msg_id = self._persist_turn(user_input, clean_text)
        yield "", True, {"message_id": msg_id} if msg_id else {}
        # commit + learn: runs off the hot path so the user isn't waiting
        threading.Thread(
            target=self._post_turn,
            args=(user_input, clean_text),
            daemon=True,
        ).start()

    def _persist_turn(self, user_input: str, response: str) -> int | None:
        """Persist user+assistant messages to the sessions DB. Returns assistant message id."""
        try:
            if not self.session_id:
                self.session_id = sessions.new_session(user_input, user_id=self.user_id)
            sessions.append_message(self.session_id, "user",      user_input, self._turn_order, user_id=self.user_id)
            msg_id = sessions.append_message(self.session_id, "assistant", response, self._turn_order + 1, user_id=self.user_id)
            self._turn_order += 2
            return msg_id
        except Exception:
            if cfg.DEBUG:
                import traceback; traceback.print_exc()
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
            ))
        except Exception:
            if cfg.DEBUG:
                import traceback; traceback.print_exc()

    def _ensure_tool_index(self) -> None:
        """
        Build the category-level embedding index in one batch call. No-op after first run.
        Embeds the 10 category descriptions (not all 43 tool schemas) — fast and coherent.
        Failures leave _tool_index empty — brain falls back to the full schema.
        """
        if self._tool_index_ready or not self.tool_registry:
            return
        try:
            self._tool_index = self.tool_registry.build_category_index(self.ollama.embed_batch)
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
            self.memory.init_router(self.ollama.embed_batch)
            if cfg.DEBUG:
                print(f"[memory router] {len(self.memory._domain_index)} domains indexed")
        except Exception as exc:
            if cfg.DEBUG:
                print(f"[memory router] build failed (will inject everything): {exc}")
        finally:
            self._memory_router_ready = True

    # ── Conversational learning ──────────────────────────────────────────────

    def _post_turn(self, user_input: str, assistant_text: str) -> None:
        """
        Background post-turn processing: persist turn + extract knowledge.
        Runs in a daemon thread — never blocks the user.
        """
        self.memory.commit_turn(user_input, assistant_text)
        self._turn_count += 1
        # Rate-limit: only extract knowledge every 3rd turn to reduce Ollama
        # queue pressure. The background LLM call delays the next turn's embed
        # + chat because Ollama serializes GPU work.
        if LEARN_FROM_CONVERSATION and self._turn_count % 3 == 0:
            try:
                self._extract_knowledge(user_input, assistant_text)
            except Exception:
                if cfg.DEBUG:
                    import traceback; traceback.print_exc()

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
            resp = self.ollama.chat(
                messages=[{"role": "user", "content": f"{LEARN_PROMPT}\n\n{exchange}"}],
                model=self.model,
                think=False,
                temperature=TEMPERATURE_TOOL,
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

        if cfg.DEBUG and saved:
            print(f"[learn] saved {saved} knowledge entries")

    def _execute_tool(self, name: str, args: dict, trace_id: str) -> dict:
        if not self.tool_registry:
            return {"success": False, "error": f"No tool registry — cannot run '{name}'"}
        # Set thread-local user_id so tools can scope DB queries per-user
        from kai._app_state import set_current_user_id
        set_current_user_id(self.user_id)
        try:
            output = self.tool_registry.execute(name, args)
            return {"success": True, "output": output}
        except KeyError:
            # Unknown tool name — try alias learning (model may have hallucinated a name)
            target = self.tool_registry.learn_alias(name)
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
        return lambda text: self.ollama.embed(text)

    def _maybe_compress_history(
        self, on_status: "Callable[[str], None] | None" = None
    ) -> None:
        """
        Compress _session_history when it grows too large for the context window.

        Fires only when total chars exceed HISTORY_CHAR_LIMIT (~3k tokens at 4 chars/token).
        Older turns are replaced with a single summary system message so the model
        keeps the thread without blowing the token budget.

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

            if on_status:
                on_status("Compressing memory...")

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
            raw = "\n\n".join(
                f"[{m['role']}]: {m.get('content', '')[:800]}" for m in to_compress
            )
            resp = self.ollama.chat(
                messages=_build_compress_messages(raw),
                model=self.model,
                think=False,
                temperature=TEMPERATURE_TOOL,
            )
            summary = resp.get("message", {}).get("content", "").strip()
            _, summary = _strip_thinking(summary)

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
            threading.Thread(
                target=self.memory.archive_history,
                args=(summary,),
                daemon=True,
            ).start()
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
        raw = "\n\n".join(
            f"[{m['role']}]: {m.get('content', '')[:800]}" for m in messages
        )
        resp = self.ollama.chat(
            messages=_build_compress_messages(raw),
            model=self.model,
            think=False,
            temperature=TEMPERATURE_TOOL,
        )
        summary = resp.get("message", {}).get("content", "").strip()
        _, summary = _strip_thinking(summary)
        if summary:
            self.memory.archive_history(summary)
