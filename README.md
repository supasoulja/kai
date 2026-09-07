# Kai

Local AI agent. No cloud. No API keys. Runs entirely on your hardware.

**Stack:** Python + Ollama + SQLite. No LangChain. No frameworks.

> **Warning:** This is a solo developer project. Kai is an AI agent that can run system
> commands, modify files, and interact with your PC. **Do not blindly follow Kai's advice. Please for the love of God**
> Always review what they're proposing before approving destructive actions — especially file
> deletions and system changes. They can be wrong. They can hallucinate. They have safety rails,
> but they are not bulletproof. Use at your own risk.

---

## What Kai Is

Kai is an agent, not a chatbot. They observe, plan, act, and remember across sessions.
They own a domain — your machine — and use tools to diagnose, search, research, and help you.

Edit `kai/persona/persona.md` to change their behavior. No code changes needed.

---

## What's New

- **Native Linux desktop app** — pywebview window with system tray, single-instance lock, and `Ctrl+Shift+K` global hotkey. `bash scripts/install-desktop.sh` adds it to your app menu.
- **Filesystem tree memory** — hierarchical memory store with paths like `user/identity/profession`. Version C probabilistic scoring: recency × confidence × similarity × importance. Hardcoded prefixes (health, hardware, profession) are always surfaced first.
- **Cerebellum** — execution validation layer that runs pre/post every tool call. Detects intent drift, scope creep, loop repetition, and output incoherence. Returns CLEAR / FLAG / STOP without touching the main LLM (~5ms, CPU-only).
- **Intuition flags** — five detectors (contradiction, pattern break, emotional incongruence, accumulation, escalation approach) that override the scoring equation when something feels off. Sit outside the math so edge cases the equation misses still get caught.
- **Three state stores** — UserState (emotional register, session intent, terseness), KaiState, RelationshipState. Relationship depth scales every memory score — Kai asserts less when you're new, more when they know you.
- **Daily briefing** — LLM-free morning summary assembled from stale goals. Generated in <100ms, delivered at the start of the next session.
- **Usage pattern tracking** — async log of every tool call by time of day. After enough samples, proactive one-line suggestions appear in context ("You usually check temps around this time").
- **Goals system** — persistent multi-session tasks with ordered steps. Active goals are injected into every context block so they're never forgotten across conversations.
- **Study tools** — open academic search across arXiv, Semantic Scholar, PubMed, CORE, SciELO, Unpaywall, Open Access Button, Open Library, and Project Gutenberg. Local study library with vector search.
- **Scheduler** — lightweight background thread for daily jobs at configurable HH:MM times. Powers the morning briefing; extensible for any recurring task.

---

## Features

- **Multi-model routing** — fast CPU embedding classifies every user turn and routes to chat, reasoning (thinking mode), tool use, or researcher. Seeds from built-in patterns and grows dynamically from usage.
- **75+ tools** — web search, full-page URL fetching, browser automation, vision analysis, audio transcription, system diagnostics, sandboxed file management, network tools, notes, goals, study/research, self-inspection, and more
- **Filesystem tree memory** — hierarchical key-value nodes at paths like `user/preferences/gaming/fps`. Version C scoring (recency × confidence × similarity × importance). Hardcoded prefixes always surface first. Three state stores modulate every score.
- **Cerebellum validation** — pre/post execution checks on every tool call. Intent drift, scope, loop detection, and output coherence. CPU-only ONNX, ~5ms per check.
- **Intuition flags** — five detectors override scoring when the equation misses something. Surfaced in a `[FLAGS]` block the chat model can reason about.
- **5-tier memory** — semantic facts, episodic summaries, procedural rules, session cache, and per-user knowledge store. All SQLite, all local, all per-user isolated.
- **Dual embedding** — fast CPU-only ONNX (384-dim, ~5ms) for live routing and search; optional GPU re-embed at shutdown (2560-dim) for higher quality
- **Voice interface** — Web Audio API mic capture (16kHz WAV), faster-whisper STT, kokoro-onnx TTS with 20+ voices. Tap-to-toggle or hold for push-to-talk.
- **Document RAG** — upload PDFs and text files, chunked and embedded for vector search
- **Multi-user auth** — name + PIN + machine-bound certificate. Session cookies, per-user memory isolation.
- **ReAct tool loop** — non-streaming tool rounds with error escalation, JSON repair for broken tool calls, fact extraction and grounding, dangerous-tool confirmation gate
- **Streaming responses** — SSE streaming with markdown rendering, thinking block display, and activity logging
- **Kaomoji face system** — 640-combination ASCII face with 15 named presets and idle animation
- **LAN / phone access** — `python web.py --lan` serves over self-signed TLS for same-network phone access. PWA-ready with home screen install support.
- **Generation presets** — Thinking / Normal / Creative / Crazy modes adjust temperature and chain-of-thought
- **Daily briefing** — LLM-free morning summary delivered at session start. Covers goals that have stalled.
- **Usage pattern proactives** — Kai notices when you do the same thing at the same time and offers to do it before you ask.

---

## Requirements

- Python 3.12+
- [Ollama](https://ollama.com) installed and running
- 16 GB VRAM recommended (RTX 4080 or similar)
- ffmpeg installed (for audio transcription tool)
- Chromium downloaded via Playwright (for browser automation tool)

CPU-only works but reasoning and tool-call speed will be significantly slower.

---

## Install

Three ways in. They share one packaging foundation, so nothing is a second-class
path — pick whichever suits you.

| | Command | Best for |
|---|---|---|
| **Clone** | `git clone … && pip install -r requirements.txt` | Hacking on Kai. Data stays in `./var`. |
| **Package** | `pip install kai-agent[voice,documents]` | Just running it. Data goes to your OS data dir. |
| **Container** | `docker compose -f docker/compose.yaml up` | Isolating Kai from your system Python. See [docker/README.md](docker/README.md). |

Ollama is a prerequisite in all three cases — it is never bundled, because it
needs your GPU. Point Kai at it with `KAI_OLLAMA_URL` if it is not on
`127.0.0.1:11434`.

Installed as a package, you get three commands:

```bash
kai          # terminal REPL
kai-web      # browser UI at http://localhost:7860
kai-app      # native desktop window (needs the [desktop] extra + system packages)
```

### Configuration

Every path Kai writes to takes an environment override. Useful for a second
profile, data on a bigger disk, or an Ollama running elsewhere:

| Variable | Default (clone) | Default (installed) |
|---|---|---|
| `KAI_OLLAMA_URL` | `http://127.0.0.1:11434` | same |
| `KAI_VAR_DIR` | `./var` | `~/.local/share/kai` (Linux) |
| `KAI_MODELS_DIR` | `./models` | `$KAI_VAR_DIR/models` |
| `KAI_SKILLS_DIR` | `./user_skills` | `$KAI_VAR_DIR/user_skills` |
| `KAI_WORKSPACE_DIR` | `~/KaiFiles` | `~/KaiFiles` |

On Windows the installed default is `%LOCALAPPDATA%\Kai`; on macOS,
`~/Library/Application Support/Kai`.

---

## Setup (from a clone)

```bash
# 1. Clone the repo
git clone <your-repo-url>
cd newB2_kai

# 2. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies (one command — covers core, voice, and browser)
pip install -r requirements.txt

# 4. Download the Chromium browser binary for the browser-automation tool
#    (the playwright package itself is already installed by step 3)
python -m playwright install chromium

# 5. Install ffmpeg (for audio transcription)
# Ubuntu/Debian:
sudo apt-get install ffmpeg
# Windows: download from https://ffmpeg.org

# 6. Pull required models
# The two Kai needs to boot:
ollama pull gemma4:12b              # chat / tools / vision / reasoning (~8 GB)
ollama pull qwen3-embedding:4b      # HQ embedding for shutdown re-embed (~2.5 GB)

# Optional — only if you change Settings → tool-model level away from the default:
# ollama pull gemma4:e4b            # "Light"  — faster tool rounds (~3.3 GB resident)
# ollama pull gemma4:31b            # "Deep"   — needs a 24 GB card, spills below that

# 7. Run
python web.py       # browser-based web UI at http://localhost:7860
python cli.py       # terminal REPL
python app.py       # native desktop window (see Desktop App below)
```

First run downloads a small (~25 MB) ONNX embedding model and prompts you to register an account.

On first voice use, faster-whisper downloads the Whisper `small` model (~462 MB) automatically.

### Optional feature groups

Some features are gated behind extra dependencies so you only install what you
use (and a future installer can offer them as checkboxes). Each is guarded by
`try/except` in code — Kai runs fine without them; the feature just stays off.

| Feature | Install | Enables |
|---------|---------|---------|
| **Document ingest** | `pip install -r requirements-documents.txt` *(or `kai-agent[documents]`)* | Upload PDFs/Word docs for RAG; index downloaded study material (uses `pypdf` + `python-docx`) |
| **Native desktop app** | `pip install -r requirements-desktop.txt` *(or `kai-agent[desktop]`)* | `python app.py` window + tray (see [Desktop App](#desktop-app-linux--windows)) |
| **HTTPS / LAN** | `pip install cryptography` | Self-signed TLS for `python web.py --https` / `--lan` (already in core requirements) |
| **AMD GPU temps (Windows)** | `pip install pyadl` | GPU temperature readout on AMD cards via the AMD Display Library |

---

## Running Modes

| Mode | Command | What you get |
|------|---------|-------------|
| **Web UI** | `python web.py` | Browser at `http://localhost:7860` |
| **LAN / Phone** | `python web.py --lan` | HTTPS on your local IP, phone-accessible PWA |
| **CLI** | `python cli.py` | Terminal REPL with `:commands` |
| **Desktop App** | `python app.py` | Native window, system tray, global hotkey |

---

## Desktop App (Linux / Windows)

A pywebview window wraps the same web UI without a browser. Features:

- **System tray** — minimize to tray, restore from tray or `Ctrl+Shift+K`
- **Close dialog** — choose "minimize to tray" or "quit completely" with a "remember my choice" checkbox
- **Single-instance lock** — launching a second copy brings the existing window to front
- **Autostart** — enable/disable via the app settings; writes to `~/.config/autostart/` on Linux, Windows Startup folder on Windows

**Linux setup (one-time):**

```bash
# Install system packages (Ubuntu/Debian)
sudo apt install python3-gi gir1.2-webkit2-4.1

# pywebview renders via the system GTK/PyGObject (`gi`), which lives outside
# the venv. Your venv must be allowed to see system packages, or you'll get
# "No module named 'gi'". Either create it that way:
#     python3 -m venv --system-site-packages .venv
# or flip the flag on an existing venv:
#     sed -i 's/include-system-site-packages = false/include-system-site-packages = true/' .venv/pyvenv.cfg

# Install Python desktop dependencies (into your project venv —
# activate it first, or pip will hit PEP 668 "externally-managed-environment")
source .venv/bin/activate
pip install -r requirements-desktop.txt

# Add to app menu (and optionally autostart)
bash scripts/install-desktop.sh

# Run
python app.py
```

---

## Models

| Model | Role | VRAM | Notes |
|-------|------|------|-------|
| `gemma4:12b` | Chat, tools, vision — always resident | ~8 GB | Multimodal, native function calling |
| `gemma4:26b` | Deep reasoning — loaded on demand | ~14 GB | MoE: 4B active params, 26B total |
| `Xenova/bge-small-en-v1.5` | Live embedding (CPU, ONNX) | 0 — CPU only | ~5 ms per embed |
| `qwen3-embedding:4b` | HQ re-embed at shutdown | ~2.5 GB (optional) | 2560-dim vectors |

Ollama swaps models as needed. The 12B stays resident; the 26B is loaded when the handoff router
classifies a turn as requiring deep reasoning, then released after.

Set `OLLAMA_KV_CACHE_TYPE=q8_0` in your environment to halve KV-cache VRAM usage.

---

## Multi-Model Routing

Every user turn is embedded and compared against a vector index of routing patterns to decide
which mode handles it — all in ~5 ms using the CPU-only ONNX embedder, zero GPU cost.

| Mode | Trigger examples | What changes |
|------|-----------------|-------------|
| `chat` | Greetings, personal questions, memory recall | Normal response |
| `reasoning` | "think through this", "explain why", complex analysis | Gemma 4 thinking mode forced on |
| `tool` | "debug this", "run a command", "write code" | Tool definitions prioritized |
| `researcher` | "search for", "look this up", "what is" | Knowledge store searched, researcher prompt injected |

The router starts with seed patterns and grows from real usage — call `HandoffRouter.learn()` to
teach it new patterns from successful interactions.

---

## Memory

### Tree memory

Hierarchical key-value nodes at filesystem-style paths:

```
user/identity/profession        → "software developer"
user/preferences/gaming/fps     → ["CS2", "Apex"]
user/health/allergies           → "penicillin"
```

Every node is scored on retrieval using the **Version C equation**:

```
score = P(still_true) × P(correct) × P(relevant_now) × boost(importance, frequency)
         recency_decay   confidence   cosine_similarity   importance × specificity + freq_lift
```

Hardcoded prefixes (`user/health`, `user/identity/hardware`, `user/identity/profession`, `user/identity/critical`) bypass scoring and always surface first.

### Scoring modifiers

Three state stores produce a `context_modifier` scalar that scales every node score:

- **UserState** — emotional register, session intent, terseness, recent override rate
- **KaiState** — Kai's own current mode and confidence
- **RelationshipState** — depth (0.0–1.0), trust, conversation count

Low relationship depth compresses scores toward neutral (Kai asserts less). High depth lets scores spread out.

### Intuition flags

Five detectors run every turn alongside the scoring equation:

| Detector | What it catches |
|----------|----------------|
| `contradiction` | New statement conflicts with a stored node (needs semantic read) |
| `pattern_break` | Behavior deviates sharply from established patterns (needs semantic read) |
| `emotional_incongruence` | Stated emotion doesn't match conversation tone (needs semantic read) |
| `accumulation` | Same node type queried too many times in one session (arithmetic only) |
| `escalation_approach` | Conversation heading toward a topic Kai should slow down on (arithmetic only) |

A flag produces a level (`soft` / `hard` / `alert`), an action (`hold` / `ask` / `soften` / `escalate`), and a plain-English reason surfaced in a `[FLAGS]` block. Dominant flag wins when several trip at once.

### Cerebellum

Execution validation layer sitting between the tool router and every tool call:

- **pre_check** — before a tool fires: intent drift from the original request, scope boundary check, loop detection (same tool + similar args recently)
- **post_check** — after a tool returns: output coherence check

Verdicts: `CLEAR` (proceed), `FLAG` (inject warning into stream, Kai decides), `STOP` (abort chain, Kai explains). All checks use the 384-dim CPU ONNX embedder, ~5ms, zero LLM cost.

### Flat tiers (alongside the tree)

| Tier | What it stores | Persists? | Isolated? |
|------|---------------|-----------|-----------|
| **Semantic** | Stable facts: user name, preferences, hardware | Yes — forever | Per-user |
| **Episodic** | Session summaries (compressed from raw turns) | Yes — across sessions | Per-user |
| **Procedural** | Behavioral rules (tone, response style) | Yes — set at startup | Per-user |
| **Session** | Runtime stats: CPU%, temps | No — current session only | Per-user |
| **Knowledge** | Researcher-discovered facts (vector-searchable) | Yes — grows over time | Per-user (separate DB file) |

- History auto-compresses when it exceeds ~3k tokens
- Memory router activates only relevant domains per query (embedding-based, 7 domains)
- Knowledge store searched on every turn and injected as context when relevant

---

## Goals

Persistent multi-session tasks with ordered steps:

```
goals.create  — create a new goal with optional step list
goals.list    — show active goals and step progress
goals.update  — mark a step complete, add notes
goals.complete / goals.abandon
```

Active goals are injected into every context block. Stale goals (no progress in N days) appear in the daily briefing.

---

## Study Tools

Open academic search — all sources are legitimately free, no paywall bypass:

| Source | Coverage |
|--------|---------|
| arXiv | Preprints: physics, math, CS, economics, biology |
| Semantic Scholar | 200M+ papers, PDF links, citation graph |
| PubMed/NCBI | NIH-funded research |
| CORE | 200M+ full-text open-access papers |
| SciELO | Latin America's scientific output |
| Unpaywall | Legal free copy of any paper by DOI |
| Open Access Button | Unpaywall + author request fallback |
| Open Library | Internet Archive digital lending + public domain |
| Project Gutenberg | 70k public-domain epub books |

Also includes `study.ask_library` — vector search over locally indexed study items.

Set `UNPAYWALL_EMAIL` and optionally `CORE_API_KEY` (free at core.ac.uk) in `kai/config.py`.

---

## Daily Briefing

Runs at `BRIEFING_TIME` (configurable in `kai/config.py`). Assembles:

- Active goals with no progress in `GOAL_STALE_DAYS`

LLM-free — structured fact assembly only. Runs in <100ms, zero VRAM. Delivered at the start of the next chat session.

---

## Usage Patterns

Every tool call is logged asynchronously (tool name × hour of day × day of week). After `PATTERN_MIN_SAMPLES` observations for a `(user, tool, hour)` cluster, a one-line proactive suggestion is injected into context:

> `[PATTERN] You usually check temps around this time — want a quick scan?`

All detection is a fast DB aggregate query. No LLM, no embeddings.

Tracking is on by default but can be turned off per user, and the recorded history can be wiped — see [Privacy & Data at Rest](#privacy--data-at-rest).

---

## Tools

Kai picks the right tool automatically. 84 tools across 17 namespaces:

| Namespace | Key tools | Purpose |
|-----------|-----------|---------|
| `research.*` | `fetch_url` | Fetch + strip HTML from any URL |
| `browser.*` | `read_page`, `screenshot` | JS-rendered pages via headless Chromium |
| `vision.*` | `describe` | Analyze images with Gemma 4 multimodal |
| `audio.*` | `transcribe` | Transcribe audio/video files via ffmpeg + Whisper |
| `search.*` | `web` | DuckDuckGo web search |
| `system.*` | `info`, `temps`, `crashes`, `gpu_crashes` | System diagnostics |
| `files.*` | `read`, `write`, `edit`, `find_large`, `recent` | File operations |
| `network.*` | `ping`, `traceroute`, `full_diagnostic` | Network diagnostics |
| `notes.*` | `save`, `search`, `list` | Personal note taking |
| `goals.*` | `create`, `list`, `update`, `complete`, `abandon` | Multi-session goal tracking |
| `study.*` | `search_papers`, `find_free`, `search_books`, `ask_library` | Open academic search + local library |
| `workspace.*` | `git_clone`, `git_pull` | Git operations |
| `docs.*` | `search`, `list`, `delete` | Document RAG |
| `memory.*` | `search_history`, `reflect` | Memory inspection |
| `self.*` | `inspect`, `check_persona`, `propose_persona_update` | Self-inspection and persona management |

---

## Knowledge Store

Two-layer system built on SQLite + sqlite-vec:

**Handoff patterns** (`kai.db` — shared, no user data) — routing signals. Every pattern has a
target mode and a use count. Patterns grow dynamically as Kai learns which requests go where.

**User knowledge** (`kai/memory/knowledge/users/{user_id}.db` — per-user, fully isolated) —
facts the researcher discovers. Stored as vector-searchable entries, injected into context when
relevant. Deleting a user's file removes all their learned data with no risk to other users.

---

## Voice Interface

**Listening (STT):**
- Click the mic button to toggle recording, or hold it for push-to-talk
- Browser captures 16kHz mono audio and encodes to WAV client-side (no ffmpeg needed)
- WAV sent to `/voice/transcribe` → faster-whisper `small` model → text filled into input → auto-sent

**Speaking (TTS):**
- After each Kai response, `POST /voice/tts` fetches synthesized audio from kokoro-onnx
- Plays automatically in the browser via the Web Audio API
- Toggle with the speaker button next to the mic (state saved in localStorage)
- Default voice: `af_heart` — configurable in `kai/config.py` (`TTS_VOICE`)

Both STT and TTS are fully local — no network calls, no API keys.

---

## Authentication

Three-factor local auth:

1. **Name** — identifies the account (case-insensitive)
2. **PIN** — 4+ digits, stored as SHA-256 hash
3. **Machine certificate** — 30-byte random key generated once per installation. A copied database is useless on another machine.

Phone access via `--lan` only requires name + PIN (no machine certificate check on remote devices).

---

## Privacy & Data at Rest

Kai is local-only, but "local" is not the same as "encrypted." Be aware of how your data sits on disk:

- **`var/` is plaintext at rest.** All of Kai's data lives under `var/` (`var/memory/` for the SQLite databases, `var/state/` for app settings, `var/tls/` for the self-signed cert). The SQLite files are **not** encrypted at the application level — anyone with read access to `var/` can read your facts, conversations, and knowledge store directly. Secrets are the exception: PINs and session tokens are hashed, and provider API keys are encrypted with the per-device key.
- **To protect data at rest, put `var/` on an encrypted volume** — full-disk encryption (LUKS, FileVault, BitLocker) or a dedicated encrypted partition. Point Kai at it with `KAI_VAR_DIR=/path/to/encrypted/var`. Transparent SQLCipher-style database encryption is a possible future option but is not built in today.
- **Silent learning is on by default but controllable, per user.** Two background subsystems build a profile of you: conversation learning (`LEARN_FROM_CONVERSATION`) and usage-pattern tracking (`PATTERN_ENABLED`). Each can be turned off per user, and the recorded usage-pattern history can be wiped — see `kai/memory/privacy.py` (`set_learning_enabled`, `set_patterns_enabled`, `forget_usage_patterns`). Deleting your account (`delete_user`) erases all of it.

---

## LAN / Phone Access

```bash
python web.py --lan
```

- Detects your LAN IP automatically
- Generates a self-signed TLS cert with the LAN IP in the SANs
- Prints `[✓] Phone URL: https://192.168.x.x:7860` on startup
- Open the URL on your phone, accept the cert once, log in with name + PIN
- PWA-ready — "Add to Home Screen" for a native app feel

---

## CLI Commands

| Command | What it does |
|---------|-------------|
| `:memory` | Show all memory |
| `:facts` | Show stored semantic facts |
| `:forget <key>` | Delete a semantic fact |
| `:rules` | Show behavioral rules |
| `:history` | Show last 10 episodic entries |
| `:trace` | Show last 10 turn traces |
| `:tools` | List registered tools |
| `:debug` | Toggle debug output |
| `exit` | Quit |

---

## Configuration

Key settings in `kai/config.py`:

```python
CHAT_MODEL          = "gemma4:12b"                # always-resident chat + tools model
WHISPER_MODEL       = "small"                     # STT model size
TTS_VOICE           = "af_heart"                  # Kokoro TTS voice
CONTEXT_WINDOW      = 8192                        # tokens passed to Ollama
FAST_EMBED_MODEL    = "Xenova/bge-small-en-v1.5"  # CPU embedding (ONNX)
HANDOFF_THRESHOLD   = 0.55                        # routing confidence cutoff
KNOWLEDGE_TOP_K     = 5                           # knowledge entries injected per turn
BRIEFING_ENABLED    = True                        # daily morning briefing
BRIEFING_TIME       = "09:00"                     # HH:MM local time
GOAL_STALE_DAYS     = 3                           # days before a goal appears in briefing
PATTERN_ENABLED     = True                        # usage pattern tracking
PATTERN_MIN_SAMPLES = 5                           # observations before proactive suggestion fires
UNPAYWALL_EMAIL     = ""                          # email for Unpaywall rate limiting (no account needed)
CORE_API_KEY        = ""                          # free at core.ac.uk, raises rate limits
```

---

## Project Structure

```
newB2_kai/
├── web.py                      <- FastAPI server + SSE streaming + voice endpoints
├── app.py                      <- native desktop app (pywebview + pystray, Linux + Windows)
├── cli.py                      <- terminal REPL
├── scripts/
│   ├── start.sh / start.bat    <- launcher: checks deps/Ollama/models, then runs the app
│   ├── install-desktop.sh      <- adds Kai to Linux app menu, optional autostart
│   ├── kai.desktop             <- XDG desktop entry for the project directory
│   └── migrate_embeddings.py   <- one-off embedding migration
├── kai/
│   ├── config.py               <- all settings (single source of truth for data paths)
│   ├── audio.py                <- STT (faster-whisper) + TTS (kokoro-onnx)
│   ├── core/                   <- turn engine + app lifecycle
│   │   ├── brain.py            <- Ollama client + ReAct loop + handoff routing
│   │   ├── tool_gate.py        <- per-turn tool/reasoning gating
│   │   ├── flow.py             <- turn-flow recorder
│   │   ├── events.py           <- event bus (brain → UI)
│   │   ├── bootstrap.py        <- shared startup/shutdown
│   │   ├── sleep.py            <- shutdown consolidation + welcome-back
│   │   └── trace.py            <- per-turn trace log
│   ├── llm/                    <- model inference plumbing
│   │   ├── ollama.py           <- Ollama HTTP client
│   │   ├── embed.py            <- CPU embedding (ONNX) + shutdown HQ re-embed
│   │   ├── models.py           <- user-configurable model registry
│   │   └── vecmath.py          <- cosine similarity/distance
│   ├── store/                  <- SQLite persistence layer
│   │   ├── db.py               <- connection mgmt (WAL, thread-local)
│   │   ├── schema.py           <- shared dataclasses
│   │   ├── sessions.py         <- conversation history
│   │   └── users.py            <- user management + auth
│   ├── system/                 <- host / OS concerns
│   │   ├── platform.py         <- OS detection (single source of truth)
│   │   ├── device.py           <- machine certificate
│   │   ├── hwinfo.py           <- HWiNFO64 sensor reads (Windows)
│   │   └── upgrade.py          <- version-change awareness
│   ├── persona/                <- identity + persona assets
│   │   ├── identity.py         <- system prompt builder
│   │   ├── persona.md          <- Kai's authoritative self-description; edit to change behavior
│   │   ├── crew_prompts/       <- specialist system prompts (runtime assets)
│   │   └── changelog.json      <- version history Kai remembers
│   ├── util/                   <- text.py, log.py (small shared helpers)
│   ├── memory/
│   │   ├── tree.py             <- filesystem-style hierarchical memory (path-addressed nodes)
│   │   ├── scorer.py           <- Version C probabilistic scoring equation
│   │   ├── state.py            <- three state stores: UserState, KaiState, RelationshipState
│   │   ├── intuition.py        <- five detectors that override scoring on edge cases
│   │   ├── cerebellum.py       <- tool execution validation (pre/post checks, CLEAR/FLAG/STOP)
│   │   └── ...                 <- loop, briefing, scheduler, patterns, manager, knowledge,
│   │                              semantic, episodic, router, documents, context
│   ├── tools/                  <- grouped by domain (registry.py + _shell.py at root)
│   │   ├── system/             <- pc, system_info, system_ops, temps, crash_logs, self_inspect, time
│   │   ├── files/              <- file_tools, workspace_tools
│   │   ├── web/                <- network, browser, search, weather, researcher
│   │   ├── knowledge/          <- rag, study, notes
│   │   ├── memory/             <- memory_tools
│   │   ├── media/              <- audio_tools, vision
│   │   ├── compute/            <- sandbox
│   │   └── agent/              <- goals
│   ├── api/                    <- FastAPI routers (voice, study, models, deps)
│   ├── skills/                 <- skill definitions
│   └── static/
│       ├── app.html            <- main chat UI
│       ├── app.js              <- chat SSE, face system, voice recording, TTS playback
│       ├── login.html          <- login/register page
│       ├── icon-192.png        <- PWA + desktop icon
│       └── manifest.json       <- PWA manifest
├── var/                        <- runtime data (gitignored): live DBs, knowledge, state, tree
├── models/                     <- Kokoro TTS model files (gitignored, downloaded on first run)
├── data/                       <- study library / seed content
└── docs/
    ├── HISTORY_AND_VISION.md   <- 18-month build history
    └── BRAIN_DESIGN.md         <- full memory architecture spec
```

---

## Known Issues

- **Whisper model downloads on first voice use** (~462 MB for `small`). This happens once and is cached. Expect a delay on the first STT request.
- **Kokoro + Whisper in the same process** — loading both simultaneously can cause ONNX runtime contention. Both are lazy-loaded with threading locks; in practice they load on separate threads and work correctly.
- **Browser automation is slow** (~3-5s) — by design. `browser.read_page` is the fallback for JS-rendered pages; `research.fetch_url` is used first.
- **Global hotkey on Linux** — the `keyboard` package requires root or udev rules on Linux. The hotkey is silently disabled if permissions are missing; everything else works normally.
- **Windows-specific tools** (crash logs, Windows Updates, restore points, startup programs) report "only available on Windows" on Linux. Core tools work fully on both platforms.

---

## License

AGPLv3
