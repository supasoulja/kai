import os
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT_DIR        = Path(__file__).parent.parent
# Runtime data lives OUTSIDE the source package (kai/). Override the whole
# tree with KAI_VAR_DIR; model binaries live in models/ at the repo root.
VAR_DIR         = Path(os.getenv("KAI_VAR_DIR", ROOT_DIR / "var"))
MEMORY_DIR      = VAR_DIR / "memory"
KNOWLEDGE_DIR   = VAR_DIR / "knowledge"
STATE_DIR       = VAR_DIR / "state"
TREE_DIR        = VAR_DIR / "tree"
TLS_DIR         = VAR_DIR / "tls"             # self-signed certs (honors KAI_VAR_DIR)
APP_SETTINGS_PATH = STATE_DIR / "app_settings.json"  # desktop window state
AUDIO_MODELS_DIR = ROOT_DIR / "models"
DB_PATH         = MEMORY_DIR / "kai.db"
WATCHDOG_DB     = MEMORY_DIR / "watchdog.db"
PERSONA_PATH    = ROOT_DIR / "kai" / "persona" / "persona.md"
REFLECTIONS_PATH = MEMORY_DIR / "reflections.md"
CHANGELOG_PATH  = ROOT_DIR / "kai" / "persona" / "changelog.json"

MEMORY_DIR.mkdir(parents=True, exist_ok=True)

# ── Models ─────────────────────────────────────────────────────────────────────
# Sized for 16 GB VRAM.  Ollama swaps models so only one is loaded at a time —
# the roster below is built around that: one capable model covers every role
# that needs live language understanding, swapped on demand rather than held
# concurrently. Nothing here assumes a specific GPU vendor; swap-based loading
# is what makes the same roster viable on an 8 GB card or a 24 GB one.
# Set OLLAMA_KV_CACHE_TYPE=q8_0 in your environment to halve KV-cache VRAM.
#
# CHAT_MODEL      — conversation, tool calling, summarisation, knowledge
#                   extraction, vision (multimodal), deep reasoning (native
#                   thinking). gemma4:26b (MoE, ~4B active params, ~14 GB
#                   weights) — fast like a small model, reasons like a big one.
#                   REASONING_MODEL and SUMMARY_MODEL alias it: one model, not
#                   three, swapped in for whichever role the turn needs.
#
# MEMORY_MODEL    — the memory loop's semantic reads (contradiction,
#                   pattern_break, emotional_incongruence — see
#                   kai/memory/intuition.py). Swap-loaded between turns the
#                   same way REASONING_MODEL was, never held alongside
#                   CHAT_MODEL. Aliased to the same gemma4:26b: no second set
#                   of weights to keep resident, and its thinking capability
#                   carries over to judgment calls that need it.
#
# EMBED_MODEL     — dedicated embedding model for episodic vector search
#                   qwen3-embedding:4b: ~2.5 GB, 2560-dim vectors, MTEB top-tier
#
# FAST_EMBED      — lightweight CPU-only embedding (ONNX, no VRAM) for live ops.
#                   bge-small-en-v1.5: 33M params, 384-dim, ~50 MB download.
#                   Used for query routing, memory routing, and real-time search.
# HQ_EMBED        — heavy Ollama model run at shutdown to re-embed into
#                   high-quality shadow tables (no VRAM contention at shutdown).

CHAT_MODEL      = "gemma4:26b"
REASONING_MODEL = CHAT_MODEL             # unified — gemma4:26b handles both roles natively
SUMMARY_MODEL   = CHAT_MODEL             # unified — see CHAT_MODEL note above
MEMORY_MODEL    = CHAT_MODEL             # swap-loaded for the memory loop's semantic reads
# EMBED_MODEL is an alias for HQ_EMBED_MODEL — defined just below it (single source of truth)

# CPU embedding — live ops (no Ollama, no VRAM)
# Uses the Xenova ONNX-optimized version of bge-small-en-v1.5
FAST_EMBED_MODEL = "Xenova/bge-small-en-v1.5"
FAST_EMBED_DIM   = 384

# GPU embedding — shutdown re-embed to shadow tables
HQ_EMBED_MODEL   = "qwen3-embedding:4b"
HQ_EMBED_DIM     = 2560
EMBED_MODEL      = HQ_EMBED_MODEL   # shutdown re-embed alias — see note above

OLLAMA_BASE_URL = "http://127.0.0.1:11434"

# ── Context window ─────────────────────────────────────────────────────────────
# The gemma4 family supports 128K context. 8192 keeps KV-cache small and response
# latency low — raise to 32768 or higher if you need to feed large documents.
CONTEXT_WINDOW = 8192  # tokens; passed as num_ctx to Ollama

# ── Generation ─────────────────────────────────────────────────────────────────
# Tiered temperatures — each task type gets the minimum randomness it needs.
# Lower = more deterministic = less hallucination.  Higher = more personality.
#
#   TOOL   (0.0)  — tool selection: greedy decoding, zero creativity needed
#   REASON (0.1)  — factual extraction, compression, knowledge learning
#   FINAL  (0.35) — user-facing answer: preserves Kai's voice and personality
#
# Research: 0.1-0.3 for tool-calling agents; 0.8 (Ollama default) causes
# hallucination drift on small models.
TEMPERATURE_TOOL   = 0.1    # tool-call rounds: near-greedy, slight slack for reliability
TEMPERATURE_REASON = 0.10   # fact extraction, compression, learning
TEMPERATURE_FINAL  = 0.399  # final streaming answer default ("Normal" preset)

# ── Generation presets ───────────────────────────────────────────────────────
# User-facing presets that bundle (think, temperature) for the final answer.
# These replace the old Fast/Heavy model swap + think toggle. The "Thinking"
# preset turns on the main model's native chain-of-thought; the others trade
# determinism for creativity. Users can override these temps in Settings →
# Advanced (persisted per user) or nudge a single thread with the slider.
GEN_PRESETS: dict[str, dict] = {
    "thinking": {"label": "Thinking", "think": True,  "temp": 0.6},
    "normal":   {"label": "Normal",   "think": False, "temp": 0.399},
    "creative": {"label": "Creative", "think": False, "temp": 0.7},
    "crazy":    {"label": "Crazy",    "think": False, "temp": 2.0},
}
DEFAULT_PRESET = "normal"
TEMP_MIN, TEMP_MAX = 0.0, 2.0   # slider range + validation bounds

# ── Tool-model levels ──────────────────────────────────────────────────────────
# Tool-call rounds run on a dedicated function-calling model (IBM Granite) so
# the chat model can stay focused on conversation. Bigger granite = better at
# inferring the right tool from vague phrasing ("something's off with my pc");
# smaller = faster, fine when requests are specific ("check my temps").
# "off" keeps rounds on CHAT_MODEL with thinking forced on — thinking is what
# keeps gemma emitting structured tool calls instead of narrating them. It is
# also the automatic fallback whenever the selected granite isn't installed.
TOOL_MODEL_LEVELS: dict[str, dict] = {
    "light":    {"label": "Light (3B)",    "model": "granite4.1:3b",
                 "blurb": "fast — best when you ask specifically"},
    "balanced": {"label": "Balanced (8B)", "model": "granite4.1:8b",
                 "blurb": "middle ground for mixed phrasing"},
    "deep":     {"label": "Deep (30B)",    "model": "granite4.1:30b",
                 "blurb": "best at inferring vague requests (17 GB download)"},
    "off":      {"label": "Main model",    "model": None,
                 "blurb": "tool rounds on the chat model with thinking on"},
}
DEFAULT_TOOL_LEVEL = "balanced"   # Granite 8B — good default for mixed phrasing

# Safety net: hard cap on a single reasoning trace (chars). If the model loops,
# we cut thinking off and force it to answer directly instead of running forever.
THINK_CHAR_CAP = 8000

# ── Memory ─────────────────────────────────────────────────────────────────────
EPISODIC_TOP_K     = 5     # how many episodic results to inject into context
MEMORY_ROUTER_TOP_K     = 2      # how many memory domains to activate per query
MEMORY_ROUTER_THRESHOLD = 0.15   # cosine similarity cutoff (below = domain doesn't match)
LEARN_FROM_CONVERSATION = True   # model extracts knowledge after each turn (background thread)
# Semantic facts below this confidence are not injected into context — low-trust
# regex guesses and facts that have decayed over time stay out of recall until
# either re-confirmed or pruned. Set to 0 to recall everything.
RECALL_CONFIDENCE_MIN = 0.5
# Context budget — the identity block (persona + voice + rules) + procedural + semantic
# already uses ~5000-6000 chars.  Episodic entries need ~200-400 chars each.
# 8192 context window ≈ 32k chars.  10k chars ≈ 3000 tokens — leaves plenty
# of headroom for the conversation history.
MAX_CONTEXT_CHARS  = 10000  # max characters for the full context block

# ── History compression ─────────────────────────────────────────────────────────
# Compression fires when _session_history exceeds HISTORY_CHAR_LIMIT total chars.
# Rule of thumb: ~4 chars per token, so 12 000 chars ≈ 3 000 tokens.
# Swap the estimator for tiktoken later if you want exact counts.
HISTORY_CHAR_LIMIT    = 12000  # compress when active history exceeds this
HISTORY_COMPRESS_KEEP = 4      # keep last N user/assistant exchanges verbatim

# ── Tools ──────────────────────────────────────────────────────────────────────
SEARCH_MAX_RESULTS = 5
NOTES_SEARCH_TOP_K = 5

# ── Document RAG ───────────────────────────────────────────────────────────────
RAG_TOP_K     = 3    # max chunks auto-injected into context per query
RAG_THRESHOLD = 0.5  # cosine distance cutoff (0=identical, 2=opposite); 0.5 = relevant

# ── Audio (STT + TTS) ──────────────────────────────────────────────────────────
WHISPER_MODEL  = "small"      # faster-whisper model size: tiny/base/small/medium/large-v3
TTS_VOICE      = "af_heart"   # Kokoro voice (af_heart, af_bella, af_alloy, bf_emma, am_adam…)
TTS_SPEED      = 1.0          # speech speed multiplier

# ── Knowledge / Handoff routing ────────────────────────────────────────────────
KNOWLEDGE_TOP_K      = 5     # max knowledge entries injected per query
KNOWLEDGE_THRESHOLD  = 0.4   # cosine distance cutoff for knowledge search
HANDOFF_THRESHOLD    = 0.55  # cosine distance cutoff for handoff routing (lower = stricter)
# Semantic tool gate — lets the handoff router's verdict ("tool"/"researcher")
# open the tool gate when the keyword regex in brain.py misses. Keyword hits
# still work either way; this only ADDS recall. Off by default so it can be
# A/B-tested against the pure-regex gate before the keyword lists shrink.
SEMANTIC_TOOL_GATE   = False

# Turn-flow recorder — logs every step inside a turn (model requests, raw
# responses, thinking, tool calls with outputs, discarded text, fallbacks)
# to the flow_log table. View with :flow in the CLI or GET /debug/flow.
# Off by default: it's a debug firehose (10–20 fsync'd commits/turn) and writes
# user content to disk. Turn on per-session when actually debugging a turn.
FLOW_TRACE           = False
# Cap on flow_log rows — oldest rows are trimmed past this so the debug log can't
# grow unbounded when FLOW_TRACE is left on.
FLOW_LOG_MAX         = 5000
from kai.system.platform import IS_WINDOWS
WORKSPACE_DIR      = (                     # only folder Kai can write files to
    Path("C:/KaiFiles") if IS_WINDOWS
    else Path.home() / "KaiFiles"
)

# Git repos Kai is allowed to clone. Add URLs here to grant access.
# Trailing slashes and .git suffixes are ignored during comparison.
ALLOWED_GIT_REPOS: list[str] = [
    "https://github.com/wasmerio/Python-Scripts",
    "https://github.com/geekcomputers/Python",
    "https://github.com/realpython/python-scripts",
    "https://github.com/DhanushNehru/Python-Scripts",
]

# ── Cerebellum ─────────────────────────────────────────────────────────────────
# Validation layer that runs before and after every tool call.
# Drift scores are cosine distances: 0.0 = identical to intent, ~1.0 = orthogonal.
CEREBELLUM_ENABLED          = True
CEREBELLUM_DRIFT_WARN       = 0.55   # flag when a read tool drifts this far from intent
CEREBELLUM_DRIFT_STOP       = 0.75   # abort chain when drift is this bad
CEREBELLUM_WRITE_DRIFT_WARN = 0.45   # tighter warn threshold for write-capable tools
CEREBELLUM_WRITE_DRIFT_STOP = 0.65   # tighter stop threshold for write-capable tools

# ── Autonomous features ─────────────────────────────────────────────────────────
# Scheduled briefings
BRIEFING_ENABLED    = True
BRIEFING_TIME       = "09:00"    # HH:MM local time — when the daily briefing generates
BRIEFING_DEPTH      = "quick"    # "quick" (3-5 bullets) or "full" (detailed per-node)

# Pattern recognition
PATTERN_ENABLED     = True
PATTERN_MIN_SAMPLES = 5          # minimum observations before a pattern is surfaced
PATTERN_SUGGEST_WINDOW = 30      # minutes around scheduled time to offer proactive suggestion

# Goals
GOAL_STALE_DAYS     = 3          # days without progress before a goal appears in briefing

# ── Study mode ─────────────────────────────────────────────────────────────────
# Unpaywall is a free legal service that finds open-access copies of papers.
# They only ask for an email address for rate-limiting — no account required.
UNPAYWALL_EMAIL    = os.getenv("UNPAYWALL_EMAIL", "")
STUDY_LIBRARY_PATH = os.getenv("STUDY_LIBRARY_PATH", str(ROOT_DIR / "data" / "library"))
# CORE aggregates 200M+ open-access papers. Free API key at core.ac.uk/services/api
# Works without a key but rate-limited. With key: much higher limits.
CORE_API_KEY       = os.getenv("CORE_API_KEY", "")

# ── Trace ──────────────────────────────────────────────────────────────────────
DEBUG = False  # override with --debug flag at CLI
