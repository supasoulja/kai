from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT_DIR        = Path(__file__).parent.parent
MEMORY_DIR      = Path(__file__).parent / "memory" / "kai's memory"
DB_PATH         = MEMORY_DIR / "kai.db"
PERSONA_PATH    = ROOT_DIR / "kai" / "persona.md"
REFLECTIONS_PATH = MEMORY_DIR / "reflections.md"
CHANGELOG_PATH  = ROOT_DIR / "kai" / "changelog.json"

MEMORY_DIR.mkdir(parents=True, exist_ok=True)

# ── Models ─────────────────────────────────────────────────────────────────────
#
# CHAT_MODEL      — all conversation, tool calling, and knowledge extraction
#                   qwen3.5:9b: ~6.6GB, 256K context, multimodal, native tool calling
#
# REASONING_MODEL — heavy tasks (:model heavy in CLI), think mode enabled
#                   qwen3:14b: ~9GB, chain-of-thought reasoning
#
# EMBED_MODEL     — dedicated embedding model for episodic vector search
#                   qwen3-embedding:4b: ~4GB RAM, 2560-dim vectors, MTEB top-tier

CHAT_MODEL      = "qwen3.5:9b"
REASONING_MODEL = "qwen3:14b"
EMBED_MODEL     = "qwen3-embedding:4b"
SUMMARY_MODEL   = "qwen3:14b"

OLLAMA_BASE_URL = "http://127.0.0.1:11434"  # explicit IPv4 — localhost resolves to IPv6 on Windows

# ── Context window ─────────────────────────────────────────────────────────────
# qwen3.5:9b supports 256k context. 8192 keeps KV-cache small so the model
# stays fully in VRAM. Raise to 16384 if you need longer context (~1GB extra).
CONTEXT_WINDOW = 8192  # tokens; passed as num_ctx to Ollama

# ── Generation ─────────────────────────────────────────────────────────────────
# Research: 0.1-0.3 for tool-calling agents; 0.8 (Ollama default) causes
# hallucination drift. Use slightly higher for the final answer to preserve voice.
TEMPERATURE_TOOL  = 0.15  # tool-call rounds (non-streaming)
TEMPERATURE_FINAL = 0.35  # final streaming answer

# ── Memory ─────────────────────────────────────────────────────────────────────
EPISODIC_TOP_K     = 5     # how many episodic results to inject into context
MEMORY_ROUTER_TOP_K     = 2      # how many memory domains to activate per query
MEMORY_ROUTER_THRESHOLD = 0.15   # cosine similarity cutoff (below = domain doesn't match)
LEARN_FROM_CONVERSATION = True   # model extracts knowledge after each turn (background thread)
# Context budget — the identity block (persona + voice + rules) + procedural + semantic
# already uses ~5000-6000 chars.  Episodic entries need ~200-400 chars each.
# 8192 context window ≈ 32k chars.  10k chars ≈ 3000 tokens — leaves plenty
# of headroom for the conversation history.
MAX_CONTEXT_CHARS  = 10000  # max characters for the full context block
DM_CONTEXT_CHARS   = 14000  # larger budget when DM mode is active

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
WORKSPACE_DIR      = Path("C:/KaiFiles")   # only folder Kai can write files to

# Git repos Kai is allowed to clone. Add URLs here to grant access.
# Trailing slashes and .git suffixes are ignored during comparison.
ALLOWED_GIT_REPOS: list[str] = [
    "https://github.com/wasmerio/Python-Scripts",
    "https://github.com/geekcomputers/Python",
    "https://github.com/realpython/python-scripts",
    "https://github.com/DhanushNehru/Python-Scripts",
]

# ── Trace ──────────────────────────────────────────────────────────────────────
DEBUG = False  # override with --debug flag at CLI
