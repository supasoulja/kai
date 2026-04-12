# Kai

Local AI agent. No cloud. No API keys. Runs entirely on your hardware.

**Stack:** Python + Ollama + SQLite. No LangChain. No frameworks.

---

## What Kai Is

Kai is an agent, not a chatbot. She observes, plans, acts, and remembers across sessions.
She owns a domain — your machine — and proactively uses tools to diagnose, monitor, and fix things.

Edit `kai/persona.md` to change her behavior. No code changes needed.

---

## Features

- **Native desktop app** — pywebview (Edge WebView2), system tray, global hotkey (Ctrl+Shift+K), single-instance lock, startup-on-login toggle
- **Kai's Computer** — a simulated Ubuntu/GNOME desktop that visualizes Kai's behind-the-scenes activity in real time. Every tool call becomes a window: web searches open a browser, file ops open a file manager, system commands run in a terminal. Pure downstream projection — Kai never reads the event log.
- **Kaomoji face system** — 640-combination ASCII face (8 eyes x 8 mouths x 10 flairs) with 15 named presets, 3-stage blink transitions, and idle animation. Kai controls her expression via `<face:annoyed>` tags in her response stream.
- **Event bus** — SQLite-backed event log with real-time WebSocket streaming. Every tool call, reasoning chunk, and status change is recorded and broadcastable.
- **40 tools** — system diagnostics, file management, web search, notes, network tools, crash analysis, and D&D campaign management
- **4-tier memory** — semantic facts, episodic summaries, procedural rules, session cache. All SQLite, all local, all per-user isolated.
- **Dual embedding** — fast CPU-only ONNX (384-dim, ~5 ms) for live search, optional GPU re-embed at shutdown (2560-dim) for higher quality
- **Document RAG** — upload PDFs and text files, chunked and embedded for vector search
- **Multi-user auth** — name + PIN + machine-bound certificate. Session cookies, per-user memory isolation.
- **ReAct tool loop** — non-streaming tool rounds with error escalation, JSON repair for broken tool calls, fact extraction and grounding
- **Streaming responses** — SSE streaming with markdown rendering, thinking block display, and activity logging

---

## Requirements

- Python 3.12+
- [Ollama](https://ollama.com) installed (Kai auto-starts it if not running)
- AMD or NVIDIA GPU recommended (CPU works, just slower)
- 8 GB VRAM minimum

---

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/supasoulja/newb_agent
cd newb_agent

# 2. Create a virtual environment
python -m venv .venv
.venv/Scripts/activate    # Windows
# source .venv/bin/activate  # Linux/Mac

# 3. Install dependencies
pip install -r requirements.txt

# 4. Pull required models
ollama pull qwen3.5:9b            # primary model (~6.3 GB)

# Optional:
ollama pull qwen3:8b              # reasoning model (~6.0 GB)
ollama pull qwen3-embedding:4b    # HQ embedding for shutdown re-embed (~2.5 GB)

# 5. Run
python app.py     # desktop app (recommended)
python web.py     # browser-based web UI
python cli.py     # terminal REPL
```

Or on Windows, double-click `start.bat` — it auto-detects the venv.

First run downloads a small (~25 MB) ONNX embedding model and prompts you to register an account.

---

## Running Modes

| Mode | Command | What you get |
|------|---------|-------------|
| **Desktop app** | `python app.py` | Native window, system tray, hotkey, close-to-tray dialog |
| **Web UI** | `python web.py` | Browser at `http://localhost:7860`, same full UI |
| **CLI** | `python cli.py` | Terminal REPL with `:commands` |

---

## Kai's Computer

A simulated Ubuntu/GNOME desktop that shows what Kai is doing behind the scenes.

**How it works:**
- Click the **Computer** button in the top bar of the main chat UI
- A new window opens with a boot sequence (BIOS → splash → login → desktop)
- Every tool Kai uses spawns a corresponding window on the desktop:
  - `search.web` → browser window with search results
  - `system.*`, `pc.*`, `network.*` → terminal window with command output
  - `files.*` → file manager or text editor
  - `<think>` reasoning → text editor showing thought process
- Window manager supports drag, resize, minimize, maximize, close
- Top bar shows clock, connection status, and current activity
- Dock shows which window types are active

**Architecture:** The event bus (`kai/events.py`) records every tool call, thinking chunk, and status change to SQLite. Kai's Computer connects via WebSocket and renders events as desktop windows. This is a pure downstream projection — Kai's brain never reads the event log and behaves identically whether the desktop is open or not.

---

## Face System

Kai has a visible ASCII face in the chat window that reflects her emotional state.

**Part library:** 8 eyes x 8 mouths x 10 flairs = 640 unique combinations.

**15 named presets:** idle, thinking, working, focused, happy, amused, proud, excited, annoyed, confused, surprised, sympathetic, tired, sleepy, error.

**Three-tier system:**
1. **Auto-preset** — brain state drives idle/thinking/working automatically
2. **Named shortcuts** — Kai writes `<face:annoyed>` in her response (tag stripped before display)
3. **Compositional** — `<face eyes=smug mouth=smirk flair=sparkle>` for custom expressions

All face changes use a 3-stage blink transition (current → eyes-closed → new face). Idle blinking runs every 3-7 seconds.

---

## Models

Sized for 8 GB VRAM. Ollama swaps models so only one is loaded at a time.

| Model | Role | VRAM |
|-------|------|------|
| `qwen3.5:9b` | Chat, tools, summarization | ~6.3 GB |
| `qwen3:8b` | Reasoning / heavy tasks | ~6.0 GB |
| `Xenova/bge-small-en-v1.5` | Live embedding (CPU, ONNX) | 0 — CPU only |
| `qwen3-embedding:4b` | HQ re-embed at shutdown | ~2.5 GB (optional) |

Set `OLLAMA_KV_CACHE_TYPE=q8_0` in your environment to halve KV-cache VRAM usage.

---

## Tools

Kai picks the right tool automatically. 40 tools across 10 namespaces:

| Namespace | Tools | Purpose |
|-----------|-------|---------|
| `system.*` | info, temps, crashes, gpu_crashes, game_crashes, create_restore_point, clear_temp_files, disable_startup_program, run_disk_cleanup | System diagnostics and maintenance |
| `pc.*` | startup_programs, event_logs, network_info, windows_updates, deep_scan | Hardware and OS inspection |
| `files.*` | disk_usage, find_large, find_old, recent, read, list, write, append, edit | File system operations |
| `network.*` | ping, traceroute, full_diagnostic | Network diagnostics |
| `search.*` | web | DuckDuckGo web search |
| `workspace.*` | git_clone, git_pull, git_list_allowed | Git repository management |
| `notes.*` | save, search, list | Personal note taking |
| `weather.*` | current | Weather via DuckDuckGo |
| `time.*` | now | Current date and time |
| `campaign.*` | npc_save, event_log, quest_update, recall, status | D&D campaign management (WIP) |

---

## Memory

Four tiers — all SQLite, all local:

| Tier | What it stores | Persists? |
|------|---------------|-----------|
| **Semantic** | Stable facts: user name, preferences, hardware | Yes — forever |
| **Episodic** | Session summaries (compressed from raw turns) | Yes — across sessions |
| **Procedural** | Behavioral rules (tone, response style) | Yes — set at startup |
| **Session** | Runtime stats: CPU%, temps, disk% | No — current session only |

- History auto-compresses when it exceeds ~3k tokens
- Archives retrieved only when semantically relevant — not injected every turn
- Per-user isolation — users never see each other's data

---

## Authentication

Three-factor local auth:

1. **Name** — identifies the account (case-insensitive)
2. **PIN** — 4+ digits, stored as SHA-256 hash
3. **Machine certificate** — 30-byte random key generated once per installation. A copied database is useless on another machine.

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
| `:model heavy` | Switch to reasoning model |
| `:model fast` | Switch back to chat model |
| `:models` | List all configured models |
| `:debug` | Toggle debug output |
| `exit` | Quit |

---

## Project Structure

```
newb_agent/
├── app.py                    <- native desktop app (pywebview + pystray)
├── web.py                    <- FastAPI server + SSE streaming + WebSocket
├── cli.py                    <- terminal REPL
├── start.bat                 <- Windows launcher (auto-detects venv)
├── requirements.txt
├── kai/
│   ├── persona.md            <- edit this to change behavior
│   ├── brain.py              <- Ollama client + ReAct loop + event emissions
│   ├── events.py             <- event bus (SQLite + pub/sub + WebSocket)
│   ├── config.py             <- all settings
│   ├── identity.py           <- system prompt builder
│   ├── embed.py              <- CPU embedding (ONNX) + shutdown HQ re-embed
│   ├── models.py             <- model registry
│   ├── sessions.py           <- conversation history persistence
│   ├── users.py              <- auth + machine-bound certificates
│   ├── device.py             <- machine certificate generation
│   ├── memory/
│   │   ├── manager.py        <- unified interface over all memory tiers
│   │   ├── semantic.py       <- long-term key-value facts
│   │   ├── procedural.py     <- behavioral rules
│   │   ├── episodic.py       <- session summaries + vector search
│   │   ├── documents.py      <- document RAG
│   │   ├── context.py        <- system prompt context assembly
│   │   └── router.py         <- memory domain routing via embeddings
│   ├── tools/
│   │   ├── registry.py       <- tool router + schema declarations
│   │   ├── system_info.py    <- CPU, RAM, disk
│   │   ├── temps.py          <- GPU/CPU temperatures
│   │   ├── pc_tools.py       <- startup programs, event logs, deep scan
│   │   ├── system_ops.py     <- restore points, cleanup
│   │   ├── file_tools.py     <- file search + read/write
│   │   ├── workspace_tools.py<- git clone/pull + file edit
│   │   ├── network.py        <- ping, traceroute, diagnostics
│   │   ├── crash_logs.py     <- Windows error event parsing
│   │   ├── search.py         <- DuckDuckGo web search
│   │   ├── weather.py        <- weather
│   │   ├── notes.py          <- note save/search
│   │   └── ...
│   └── static/
│       ├── app.html          <- main chat UI (Tailwind + Material Design)
│       ├── app.js            <- chat streaming, face system, all UI logic
│       ├── computer.html     <- Kai's Computer (simulated GNOME desktop)
│       ├── login.html        <- login/register page
│       └── style.css         <- shared styles
└── tests/
    ├── test_memory.py
    ├── test_brain.py
    ├── test_tools.py
    └── test_integration.py
```

---

## Known Issues

- **Desktop shortcut may not launch correctly** — `pythonw.exe` swallows errors silently. Use `python app.py` from the terminal to debug. May need to kill stale Python processes on port 7860 first.
- **Session end/clear buttons not responding** — the New Chat / clear session buttons in the sidebar don't fire their click handlers. Under investigation.
- **Kai's Computer windows don't populate on first load** — the WebSocket connection requires an active session ID passed via URL parameter. Opening Kai's Computer before starting a chat session shows an empty desktop.
- **Model slow on large persona context** — the face system instructions in `persona.md` add to the system prompt. On 8 GB VRAM, this can add 1-2 seconds to first response. Keep persona additions minimal.
- **DM mode incomplete** — campaign tools exist but the full D&D hosting experience is still under development.

---

## Build Plan

Kai is under active development. Here's the roadmap:

### Completed
- **Phase 1 — App Shell:** Native desktop app with pywebview, system tray, single-instance lock, global hotkey, close-to-tray dialog, startup shortcut management
- **Phase 2a — Event Bus:** SQLite-backed event log with real-time WebSocket streaming, wired into brain.py at all tool/thinking/streaming hook points
- **Phase 2b — Kai's Computer:** Simulated Ubuntu desktop with 4 window types, boot sequence, window manager, event-to-window mapping with real tool output
- **Phase 2c — Face System:** 640-combination kaomoji face with part library, 15 presets, composition engine, face tag parser, blink transitions

### In Progress
- **Phase 3 — Data Collection:** Use Kai daily for 1-2 weeks to collect event bus data. This data informs the tool audit and provides training data for fine-tuning.

### Planned
- **Phase 4 — Tool Audit & Distillation:** Analyze event data to identify which tools are used, which overlap, and which can be merged. Goal: trim 40 tools down to ~25 high-quality tools.
- **Phase 5 — Model System Overhaul:** Merge chat and reasoning into one model. Dynamic embedding model selection by VRAM (0.6b/4b/8b). Structured JSON output for tool calls to replace regex parsing.
- **Phase 6 — Fine-Tuning:** QLoRA fine-tune of qwen3.5:9b using real conversation data, tool call patterns, face expressions, and structured output. Training via unsloth, deployed as a custom Ollama model.
- **Phase 7 — Fun Features:** Backlog of quality-of-life improvements and experimental features.

---

## Configuration

All settings are in `kai/config.py`:

```python
CHAT_MODEL       = "qwen3.5:9b"               # chat + tools + summarization
REASONING_MODEL  = "qwen3:8b"                  # heavy tasks
CONTEXT_WINDOW   = 8192                        # tokens passed to Ollama
FAST_EMBED_MODEL = "Xenova/bge-small-en-v1.5"  # CPU embedding (ONNX)
```

---

## Running Tests

```bash
# Unit tests — no Ollama needed
python -m pytest tests/test_memory.py tests/test_brain.py tests/test_tools.py -v

# Integration tests — requires Ollama + models
python -m pytest tests/test_integration.py -v -s
```

---

## License

MIT
