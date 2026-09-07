import os
from pathlib import Path

from kai.system.platform import IS_MAC, IS_WINDOWS

# ── Paths ──────────────────────────────────────────────────────────────────────
# Two roots, deliberately separate. Conflating them is what breaks packaging:
#
#   PKG_DIR   read-only assets that ship WITH the code (persona.md, crew prompts,
#             the web UI). Package-relative, so they resolve the same from a git
#             checkout, a wheel in site-packages, or a frozen bundle.
#   VAR_DIR   everything mutable Kai owns (DBs, state, tree, TLS certs). Never
#             inside the package — an installed package may sit on a read-only
#             filesystem, and a reinstall would wipe it.
#
# Every user-data path below takes a KAI_* env override. Those exist for native
# reasons (a second profile, data on another disk, Ollama on another machine);
# containers just happen to use the same knobs. No path here knows or cares
# whether it is running in one.
PKG_DIR = Path(__file__).parent  # the kai/ package itself
ROOT_DIR = PKG_DIR.parent  # repo root in a source checkout

# A source checkout has pyproject.toml sitting next to the package; an installed
# wheel does not. This is the ONLY thing that distinguishes the two modes, and it
# only ever changes *defaults* — an explicit env var wins in both.
_IS_SOURCE_CHECKOUT = (ROOT_DIR / "pyproject.toml").is_file()


def _user_data_home() -> Path:
    """Per-OS conventional home for application data."""
    if IS_WINDOWS:
        base = os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "Kai"
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "Kai"
    return Path(os.getenv("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "kai"


# Running from a checkout keeps data in ./var so a dev box stays self-contained
# and `rm -rf var/` is a clean reset. Installed, it goes where the OS expects.
_DEFAULT_VAR_DIR = ROOT_DIR / "var" if _IS_SOURCE_CHECKOUT else _user_data_home()

VAR_DIR = Path(os.getenv("KAI_VAR_DIR", _DEFAULT_VAR_DIR))
MEMORY_DIR = VAR_DIR / "memory"
KNOWLEDGE_DIR = VAR_DIR / "knowledge"
STATE_DIR = VAR_DIR / "state"
TREE_DIR = VAR_DIR / "tree"
TLS_DIR = VAR_DIR / "tls"  # self-signed certs (honors KAI_VAR_DIR)
APP_SETTINGS_PATH = STATE_DIR / "app_settings.json"  # desktop window state
DB_PATH = MEMORY_DIR / "kai.db"
REFLECTIONS_PATH = MEMORY_DIR / "reflections.md"

# Model binaries (ONNX embed, Kokoro voices) — downloaded on first run, GB-sized,
# and worth keeping across reinstalls, so they are user data, not package data.
_DEFAULT_MODELS_DIR = ROOT_DIR / "models" if _IS_SOURCE_CHECKOUT else VAR_DIR / "models"
AUDIO_MODELS_DIR = Path(os.getenv("KAI_MODELS_DIR", _DEFAULT_MODELS_DIR))

# User-authored SKILL.md recipes. Repo-local in a checkout; alongside the rest of
# the user's data once installed, so an upgrade can't delete them.
_DEFAULT_SKILLS_DIR = ROOT_DIR / "user_skills" if _IS_SOURCE_CHECKOUT else VAR_DIR / "user_skills"
SKILLS_DIR = Path(os.getenv("KAI_SKILLS_DIR", _DEFAULT_SKILLS_DIR))

# Downloaded study material (papers, PDFs). User data for the same reason models
# are: large, slow to re-fetch, and must survive a reinstall.
_DEFAULT_LIBRARY_DIR = ROOT_DIR / "data" / "library" if _IS_SOURCE_CHECKOUT else VAR_DIR / "library"

# ── Package assets — read-only, ship with the code ─────────────────────────────
PERSONA_PATH = PKG_DIR / "persona" / "persona.md"
CHANGELOG_PATH = PKG_DIR / "persona" / "changelog.json"
CREW_PROMPTS_DIR = PKG_DIR / "persona" / "crew_prompts"
STATIC_DIR = PKG_DIR / "static"

# The tree self_inspect walks when Kai reads her own code. A checkout has tests,
# scripts and the entry points worth showing; an installed copy has only the
# package.
SOURCE_ROOT = ROOT_DIR if _IS_SOURCE_CHECKOUT else PKG_DIR


def ensure_dirs() -> None:
    """Create the runtime directory tree.

    Called from the entry points and from db.get_conn(). Deliberately NOT run at
    import time: importing kai.config must stay side-effect free, or `--help`,
    a wheel build, and a read-only package layer all write to disk as a side
    effect of an import.
    """
    for d in (MEMORY_DIR, KNOWLEDGE_DIR, STATE_DIR, TREE_DIR):
        d.mkdir(parents=True, exist_ok=True)


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
#                   thinking). One model covers all of it.
#
# MEMORY_MODEL    — the memory loop's semantic reads (contradiction,
#                   pattern_break, emotional_incongruence — see
#                   kai/memory/intuition.py). Aliased to CHAT_MODEL: no second
#                   set of weights to keep resident.
#
# EMBED_MODEL     — dedicated embedding model for episodic vector search
#                   qwen3-embedding:4b: ~2.5 GB, 2560-dim vectors, MTEB top-tier
#
# FAST_EMBED      — lightweight CPU-only embedding (ONNX, no VRAM) for live ops.
#                   bge-small-en-v1.5: 33M params, 384-dim, ~50 MB download.
#                   Used for query routing, memory routing, and real-time search.
# HQ_EMBED        — heavy Ollama model run at shutdown to re-embed into
#                   high-quality shadow tables (no VRAM contention at shutdown).

# gemma4:12b (8.4 GB) — fits 100% on the 16 GB card with room for a
# large KV cache. The 17 GB gemma4:26b overflowed VRAM and spilled ~22% of
# layers onto the CPU (slow). Bump back up only on a ≥24 GB GPU.
CHAT_MODEL = "gemma4:12b"
MEMORY_MODEL = CHAT_MODEL  # swap-loaded for the memory loop's semantic reads
# EMBED_MODEL is an alias for HQ_EMBED_MODEL — defined just below it (single source of truth)

# CPU embedding — live ops (no Ollama, no VRAM)
# Uses the Xenova ONNX-optimized version of bge-small-en-v1.5
FAST_EMBED_MODEL = "Xenova/bge-small-en-v1.5"
FAST_EMBED_DIM = 384

# GPU embedding — shutdown re-embed to shadow tables
HQ_EMBED_MODEL = "qwen3-embedding:4b"
HQ_EMBED_DIM = 2560
EMBED_MODEL = HQ_EMBED_MODEL  # shutdown re-embed alias — see note above

# Where Ollama lives. An env override matters natively long before it matters in
# a container: a beefier GPU box on the LAN, a second Ollama on another port, an
# SSH tunnel. Containers reuse the same knob rather than needing their own path.
OLLAMA_BASE_URL = os.getenv("KAI_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")

# ── Context window ─────────────────────────────────────────────────────────────
# gemma4:12b advertises 262144. Sliding-window attention makes its KV cache
# unusually cheap — measured on the 16 GB card, every size below stayed
# 100% on GPU:
#     8192 → 10.5 GB    32768 → 10.8 GB    65536 → 11.3 GB
#   131072 → 12.1 GB   262144 → 14.3 GB  (no headroom left for the compositor)
# So 4x the window costs ~250 MB. num_ctx is an allocation ceiling, not content:
# raising it does not slow prefill for a given prompt.
# Keep MAX_CONTEXT_CHARS below in step with this — see the note there.
#
# 131072 is the largest size in the table that leaves real headroom. Verified live
# 2026-09-02: `ollama ps` reports 131072 CONTEXT at 100% GPU, 11.6 GB of 16.4 GB
# resident — ~4.7 GB clear for the compositor and the CPU-side embed/STT/TTS work,
# and slightly better than the table's 12.1 GB estimate.
# 262144 (14.3 GB) is the one to leave alone.
CONTEXT_WINDOW = 131072  # tokens; passed as num_ctx to Ollama

# ── Generation ─────────────────────────────────────────────────────────────────
# Tiered temperatures — each task type gets the minimum randomness it needs.
# Lower = more deterministic = less hallucination.  Higher = more personality.
#
#   TOOL   (0.1)   — tool selection: near-greedy, slight slack for reliability
#   REASON (0.10)  — factual extraction, compression, knowledge learning
#   FINAL  (0.399) — user-facing answer: preserves Kai's voice and personality
#
# Research: 0.1-0.3 for tool-calling agents; 0.8 (Ollama default) causes
# hallucination drift on small models.
TEMPERATURE_TOOL = 0.1  # tool-call rounds: near-greedy, slight slack for reliability
TEMPERATURE_REASON = 0.10  # fact extraction, compression, learning
TEMPERATURE_FINAL = 0.399  # final streaming answer default ("Normal" preset)

# ── Generation presets ───────────────────────────────────────────────────────
# User-facing presets that bundle (think, temperature) for the final answer.
# These replace the old Fast/Heavy model swap + think toggle. The "Thinking"
# preset turns on the main model's native chain-of-thought; the others trade
# determinism for creativity. Users can override these temps in Settings →
# Advanced (persisted per user) or nudge a single thread with the slider.
GEN_PRESETS: dict[str, dict] = {
    "thinking": {"label": "Thinking", "think": True, "temp": 0.6},
    "normal": {"label": "Normal", "think": False, "temp": 0.399},
    "creative": {"label": "Creative", "think": False, "temp": 0.7},
    "crazy": {"label": "Crazy", "think": False, "temp": 2.0},
}
DEFAULT_PRESET = "normal"
TEMP_MIN, TEMP_MAX = 0.0, 2.0  # slider range + validation bounds

# ── Tool-model levels ──────────────────────────────────────────────────────────
# Which model runs tool-call rounds. Bigger = better at inferring the right tool
# from vague phrasing ("something's off with my pc"); smaller = faster, fine when
# requests are specific ("check my temps"). "off" keeps rounds on CHAT_MODEL.
#
# Re-pointed from IBM Granite to gemma (2026-08-07) — granite is no longer used
# anywhere in the runtime. Measured resident VRAM at num_ctx=32768, all 100% GPU:
#     gemma4:e4b   3.3 GB   (8B; 9.6 GB on disk — nested model, loads a slice)
#     gemma4:12b   8.4 GB   (11.9B — also CHAT_MODEL)
# e4b + 12b together = 11.7 GB, so "light" genuinely fits alongside the chat model
# on a 16 GB card. "balanced" IS CHAT_MODEL: same weights, nothing extra loaded.
TOOL_MODEL_LEVELS: dict[str, dict] = {
    "light": {
        "label": "Light (8B)",
        "model": "gemma4:e4b",
        "blurb": "fast — best when you ask specifically",
    },
    "balanced": {
        "label": "Balanced (12B)",
        "model": CHAT_MODEL,
        "blurb": "the chat model — no second model loaded",
    },
    # gemma4:31b is 19 GB and will spill to CPU on a 16 GB card (unmeasured here —
    # the 17 GB gemma4:26b spilled ~22% of layers and was unusably slow). ≥24 GB only.
    "deep": {
        "label": "Deep (31B)",
        "model": "gemma4:31b",
        "blurb": "best at inferring vague requests — needs ≥24 GB VRAM",
    },
    "off": {
        "label": "Main model",
        "model": None,
        "blurb": "tool rounds on the chat model — one model, fastest",
    },
}
# Default to the single-model path: tool rounds on the chat model. This keeps only
# one model resident and skips the per-round thinking tax. See brain._run_tool_rounds
# (rounds_think=False) and the fast-path / narrated-recovery nets that keep
# tool-calling reliable. The chat model occasionally narrates a call instead of
# emitting it; those nets catch that.
DEFAULT_TOOL_LEVEL = "off"

# Safety net: hard cap on a single reasoning trace (chars). If the model loops,
# we cut thinking off and force it to answer directly instead of running forever.
THINK_CHAR_CAP = 8000

# ── Memory ─────────────────────────────────────────────────────────────────────
EPISODIC_TOP_K = 5  # how many episodic results to inject into context
MEMORY_ROUTER_TOP_K = 2  # how many memory domains to activate per query
MEMORY_ROUTER_THRESHOLD = 0.15  # cosine similarity cutoff (below = domain doesn't match)
LEARN_FROM_CONVERSATION = True  # model extracts knowledge after each turn (background thread)
# Semantic facts below this confidence are not injected into context — low-trust
# regex guesses and facts that have decayed over time stay out of recall until
# either re-confirmed or pruned. Set to 0 to recall everything.
RECALL_CONFIDENCE_MIN = 0.5
# Context budget, in characters, for the whole rendered context block.
#
# MUST stay well under CONTEXT_WINDOW * 4 (≈4 chars/token), with room left for
# tool schemas, conversation history, and the reply. At 32768 tokens the window
# holds ~131k chars; 60k for context leaves ~17k tokens for everything else.
#
# Measured composition (user with real history, 2026-08-07):
#   identity (persona.md, near-verbatim)  11,654 chars   ~2,913 tok
#   tool_index (all tool names)            4,010 chars   ~1,002 tok
#   directory + facts + procedural           ~400 chars
#   episodic (EPISODIC_TOP_K=5)            ~1,100 chars
#   → ~17-19k chars rendered
#
# The trimmer below (context.build) can only evict episodic and RAG. When this
# budget was 15000, the static blocks alone (15,664) already exceeded it, so
# every retrieved memory was silently dropped on every turn and the block still
# came in over budget. context.build now refuses to trim in that case and logs
# instead — but keep this comfortably above the static floor regardless.
MAX_CONTEXT_CHARS = 60000

# ── History compression ─────────────────────────────────────────────────────────
# Compression fires when _session_history exceeds HISTORY_CHAR_LIMIT total chars.
# Rule of thumb: ~4 chars per token, so 12 000 chars ≈ 3 000 tokens.
# Swap the estimator for tiktoken later if you want exact counts.
HISTORY_CHAR_LIMIT = 12000  # compress when active history exceeds this
HISTORY_COMPRESS_KEEP = 4  # keep last N user/assistant exchanges verbatim

# ── Tools ──────────────────────────────────────────────────────────────────────
# Tools appended to EVERY gated schema, regardless of what the category gate picks.
#
# The gate ranks by topic, which cannot see intent. "I work as a welder" is topically
# about work, so the top-2 categories came back self_inspection + workspace_and_code and
# the offered tools were git clone/pull and self.inspect — no memory tool at all, so
# Kai could not file the fact and narrated success instead (measured 2026-08-07).
# A statement worth remembering has no topical signature; it needs the write tool
# present unconditionally. Cost: ~310 tokens of the 32768 window.
ALWAYS_AVAILABLE_TOOLS: list[str] = ["tree.save", "tree.find"]

SEARCH_MAX_RESULTS = 5
# Web reads come in two modes. SEARCH mode (fetch_url / browser.read_page) returns
# a tight excerpt so the orchestrator model isn't fed a whole page — saves inference.
# LIBRARY mode (research.add_to_library) reads the full document on the way into the
# RAG store. Keep the excerpt small; the deep path has no cap.
WEB_EXCERPT_CHARS = 6000

# ── Document RAG ───────────────────────────────────────────────────────────────
RAG_TOP_K = 3  # max chunks auto-injected into context per query
RAG_THRESHOLD = 0.5  # cosine distance cutoff (0=identical, 2=opposite); 0.5 = relevant

# ── Audio (STT + TTS) ──────────────────────────────────────────────────────────
WHISPER_MODEL = "small"  # faster-whisper model size: tiny/base/small/medium/large-v3
TTS_VOICE = "af_heart"  # Kokoro voice (af_heart, af_bella, af_alloy, bf_emma, am_adam…)
TTS_SPEED = 1.0  # speech speed multiplier

# ── Knowledge / Handoff routing ────────────────────────────────────────────────
# Knowledge search top_k/threshold are NOT configurable here — kai/memory/knowledge.py
# takes them as call arguments (top_k=5, threshold=0.6). Two settings used to sit
# here advertising 0.4; nothing read them. Wire them through if you want the knob.
HANDOFF_THRESHOLD = 0.55  # cosine distance cutoff for handoff routing (lower = stricter)
# Semantic tool gate — lets the handoff router's verdict ("tool"/"researcher")
# open the tool gate when the keyword regex in brain.py misses. Keyword hits
# still work either way; this only ADDS recall (additive — never closes the gate).
# Enabled 2026-06-24 (3e): the crew triage relies on the same semantic tool axis
# (HandoffRouter.axis_match), so the legacy _select_tool_schema path is brought in
# line. Set KAI_SEMANTIC_GATE=0 to force off for A/B.
SEMANTIC_TOOL_GATE = os.environ.get("KAI_SEMANTIC_GATE", "1").lower() not in ("0", "false", "no")

# Crew routing — when True, run_stream routes tool turns through the triage tree
# (kai/core/crew.py): FAST → one specialist, BOSS → Otto orchestration, instead
# of the single generalist _run_tool_rounds loop. Off by default so it can be
# A/B-tested against the current loop; flip to True (or set KAI_CREW=1) to enable.
CREW_ENABLED = os.environ.get("KAI_CREW", "").lower() in ("1", "true", "yes")

# Turn-flow recorder — logs every step inside a turn (model requests, raw
# responses, thinking, tool calls with outputs, discarded text, fallbacks)
# to the flow_log table. View with :flow in the CLI or GET /debug/flow.
# Off by default: it's a debug firehose (10–20 fsync'd commits/turn) and writes
# user content to disk. Turn on per-session when actually debugging a turn.
FLOW_TRACE = False
# Cap on flow_log rows — oldest rows are trimmed past this so the debug log can't
# grow unbounded when FLOW_TRACE is left on.
FLOW_LOG_MAX = 5000

# The only folder Kai can write files to. Overridable so the sandbox root can be
# pointed at a mounted volume, a scratch disk, or a per-profile folder.
WORKSPACE_DIR = Path(
    os.getenv("KAI_WORKSPACE_DIR")
    or (Path("C:/KaiFiles") if IS_WINDOWS else Path.home() / "KaiFiles")
)

# Where notes.save mirrors each note to disk as .md + .txt. The SQLite `notes`
# table stays the source of truth for search; these files are the human-readable
# copy you can open, diff, and commit. Defaults inside the workspace sandbox.
# If the user points Kai at a notes repo (settings → Notes & repo, stored as the
# `notes_repo` fact), notes land in <repo>/NOTES_REPO_SUBDIR instead.
NOTES_DIR = WORKSPACE_DIR / "notes"
NOTES_REPO_SUBDIR = "notes"  # folder inside the configured repo notes are written to

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
CEREBELLUM_ENABLED = True
CEREBELLUM_DRIFT_WARN = 0.55  # flag when a read tool drifts this far from intent
CEREBELLUM_DRIFT_STOP = 0.75  # abort chain when drift is this bad
CEREBELLUM_WRITE_DRIFT_WARN = 0.45  # tighter warn threshold for write-capable tools
CEREBELLUM_WRITE_DRIFT_STOP = 0.65  # tighter stop threshold for write-capable tools

# ── Autonomous features ─────────────────────────────────────────────────────────
# Scheduled briefings
BRIEFING_ENABLED = True
BRIEFING_TIME = "09:00"  # HH:MM local time — when the daily briefing generates

# Pattern recognition
PATTERN_ENABLED = True
PATTERN_MIN_SAMPLES = 5  # minimum observations before a pattern is surfaced
PATTERN_SUGGEST_WINDOW = 30  # minutes around scheduled time to offer proactive suggestion

# Goals
GOAL_STALE_DAYS = 3  # days without progress before a goal appears in briefing

# ── Study mode ─────────────────────────────────────────────────────────────────
# Unpaywall is a free legal service that finds open-access copies of papers.
# They only ask for an email address for rate-limiting — no account required.
UNPAYWALL_EMAIL = os.getenv("UNPAYWALL_EMAIL", "")
STUDY_LIBRARY_PATH = os.getenv("STUDY_LIBRARY_PATH", str(_DEFAULT_LIBRARY_DIR))
# CORE aggregates 200M+ open-access papers. Free API key at core.ac.uk/services/api
# Works without a key but rate-limited. With key: much higher limits.
CORE_API_KEY = os.getenv("CORE_API_KEY", "")

# ── Trace ──────────────────────────────────────────────────────────────────────
DEBUG = False  # override with --debug flag at CLI
