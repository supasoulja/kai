# Kai

Local AI agent. No cloud. No API keys. Runs entirely on your hardware.

**Stack:** Python + Ollama + SQLite. No LangChain. No frameworks.

---

## What Kai Is

Kai is an agent, not a chatbot. She observes, plans, acts, and remembers across sessions.
She owns a domain — your machine — and proactively uses tools to diagnose, monitor, and fix things.

Edit `kai/persona.md` to change her behavior. No code changes needed.

---

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) installed and running
- AMD or NVIDIA GPU recommended (CPU works, just slower)

---

## Setup

```bash
# 1. Clone the repo
git clone <https://github.com/supasoulja/newb_agent>
cd newb_agent

# 2. Install dependencies
pip install -r requirements.txt

# 3. Pull required models
ollama pull qwen3.5:9b            # primary model — chat, tools, summarization (~6.6GB)
ollama pull qwen3:14b              # reasoning model — heavy tasks / :model heavy (~9GB)
ollama pull qwen3-embedding:4b     # embeddings — episodic + document vector search (~4GB)

# 4. Run (web UI)
python web.py

# Or run as CLI
python cli.py
```

The web UI opens automatically at `http://localhost:7860`.

First run creates a machine certificate and prompts you to register an account (name + PIN).

---

## AMD GPU Note

If Ollama is running on CPU instead of GPU, the context window may be too large.
The config sets `CONTEXT_WINDOW = 8192` to fit in 16GB VRAM. Verify with `ollama ps`:

```
NAME           SIZE    PROCESSOR    CONTEXT
qwen3.5:9b    6.6 GB  100% GPU     8192
```

If it still shows CPU, verify your ROCm or AMDGPU-PRO drivers are installed.

---

## Web UI

```
python web.py [--port 8080] [--no-browser]
```

Multi-user web interface with:
- **Login/register** — name + PIN + machine-bound certificate (no cloud auth)
- **Dashboard** — memory stats, recent sessions, quick actions
- **Chat** — streamed responses with status indicator, markdown rendering, animated ASCII face
- **Settings** — response mode, reasoning toggle, memory browser, document upload, DM mode
- **D&D DM mode** — full campaign management with NPCs, quests, and event logs

Each user gets isolated memory, sessions, and campaigns.

---

## CLI

```
python cli.py [--debug] [--model heavy]
```

| Command | What it does |
|---------|-------------|
| `:memory` | Show all memory — facts, rules, episodic entries |
| `:facts` | Show stored semantic facts |
| `:forget <key>` | Delete a semantic fact |
| `:rules` | Show behavioral rules |
| `:history` | Show last 10 episodic entries |
| `:trace` | Show last 10 turn traces with timing |
| `:tools` | List registered tools |
| `:model heavy` | Switch to qwen3:14b (thinking ON) |
| `:model fast` | Switch back to qwen3.5:9b |
| `:debug` | Toggle debug output |
| `exit` | Quit |

---

## Tools

Kai picks the right tool automatically. You never have to ask her to use one.

| Tool | What it does |
|------|-------------|
| `time.now` | Current date and time |
| `weather.current` | Current weather (DuckDuckGo, no API key) |
| `search.web` | Web search (DuckDuckGo) |
| `system.info` | CPU, RAM, disk usage snapshot |
| `system.temps` | CPU and GPU temperatures |
| `system.crashes` | Recent Windows crash/error events |
| `system.gpu_crashes` | GPU crash events from Windows event log |
| `system.game_crashes` | Game crash events from Windows event log |
| `system.create_restore_point` | Create a Windows restore point before changes |
| `system.clear_temp_files` | Delete temp files |
| `system.disable_startup_program` | Disable a startup entry |
| `system.run_disk_cleanup` | Run Windows Disk Cleanup |
| `pc.startup_programs` | List startup programs |
| `pc.event_logs` | Scan Windows event logs |
| `pc.network_info` | IP, adapters, connection status |
| `pc.windows_updates` | Check for pending Windows updates |
| `pc.deep_scan` | Full system diagnostic (CPU, GPU, disk, crashes, startup, network) |
| `files.disk_usage` | Per-drive usage |
| `files.find_large` | Find largest files/folders |
| `files.find_old` | Find files not accessed recently |
| `files.recent` | Files modified in the last N days |
| `files.read` | Read the contents of a file |
| `files.list` | List files in a directory |
| `files.write` | Create or overwrite a file in the workspace |
| `files.append` | Append text to a file |
| `files.edit` | Edit a specific section of a file |
| `network.ping` | Ping a host |
| `network.traceroute` | Trace route to a host |
| `network.full_diagnostic` | Full network diagnostic |
| `notes.save` | Save a note |
| `notes.search` | Search saved notes |
| `notes.list` | List recent notes |
| `workspace.git_clone` | Clone an allowed git repository |
| `workspace.git_pull` | Update a cloned repository |
| `workspace.git_list_allowed` | List repos Kai is allowed to clone |
| `campaign.npc_save` | Save an NPC to campaign memory (DM mode) |
| `campaign.event_log` | Log a campaign event (DM mode) |
| `campaign.quest_update` | Update a quest (DM mode) |
| `campaign.recall` | Search campaign memory (DM mode) |
| `campaign.status` | Get active campaign status (DM mode) |

---

## Memory

Four tiers — all SQLite, all local:

| Tier | What it stores | Persists? |
|------|---------------|-----------|
| **Semantic** | Stable facts: user name, preferences, hardware model | Yes — forever |
| **Episodic** | Session summaries (compressed from raw turns) | Yes — across sessions |
| **Procedural** | Behavioral rules (tone, response style) | Yes — set at startup |
| **Session** | Runtime stats: CPU%, temps, disk% | No — current session only |

**How it works:**
- Raw turns are staged in episodic memory temporarily (never injected into context)
- When active history exceeds ~3 000 tokens, the oldest turns are compressed into a single summary message that stays in the conversation — keeping the thread intact without blowing the token budget
- The compressed content is archived to the episodic DB; raw turns are then deleted
- Archives are only retrieved when semantically relevant to the current query — not read on every turn
- On "New Chat", the current session is compressed and archived before clearing
- Volatile stats (CPU%, temps) never touch the DB; they live in the session cache only
- On startup, any stale volatile facts from old sessions are automatically purged
- **Per-user isolation** — every memory table is scoped by `user_id`; users never see each other's data

**Document RAG:**
- Upload PDFs and text files via the web UI
- Documents are chunked and embedded for vector search
- Relevant chunks are auto-injected into context when the query matches
- Owner-only delete; shared documents visible to all users

---

## Authentication

Kai uses a three-factor local auth system:

1. **Name** — identifies the account (case-insensitive)
2. **PIN** — 4+ digits, stored only as a SHA-256 hash
3. **Machine certificate** — a 30-byte random key generated once per installation (`kai/device.py`). Its hash is stored per user at registration. A copied database is useless on another machine.

Session cookies (httpOnly, strict SameSite) keep you logged in for 7 days.

---

## Customizing Kai

Edit `kai/persona.md` — no code changes needed. The file controls:
- What Kai is and how she thinks
- Her domain (what she owns and monitors)
- Voice, tone, and communication style
- Rules for memory, tools, and system changes

---

## Project Structure

```
newb_agent/
├── web.py                    <- FastAPI server + SSE streaming + multi-user auth
├── cli.py                    <- terminal REPL entry point
├── requirements.txt
├── kai/
│   ├── persona.md            <- edit this to change behavior
│   ├── brain.py              <- Ollama HTTP client + ReAct tool-call loop
│   ├── identity.py           <- builds system prompt from persona.md
│   ├── config.py             <- all settings (models, paths, thresholds)
│   ├── schema.py             <- shared data types
│   ├── trace.py              <- turn timing and observability
│   ├── sessions.py           <- persist and browse conversation history
│   ├── campaign.py           <- D&D campaign data (DM mode)
│   ├── db.py                 <- database schema + migrations
│   ├── users.py              <- user registration, login, machine-bound auth
│   ├── device.py             <- machine certificate generation
│   ├── upgrade.py            <- version change detection
│   ├── _app_state.py         <- thread-local user_id + shared embed function
│   ├── memory/
│   │   ├── manager.py        <- single interface over all memory tiers
│   │   ├── semantic.py       <- long-term key-value facts
│   │   ├── procedural.py     <- behavioral rules
│   │   ├── episodic.py       <- session summaries + vector search
│   │   ├── documents.py      <- document RAG (upload, chunk, search)
│   │   ├── extractor.py      <- auto-extract facts from conversation
│   │   ├── context.py        <- assembles the system prompt context block
│   │   └── router.py         <- memory domain routing via embeddings
│   ├── tools/
│   │   ├── registry.py       <- tool router + Ollama schema declarations
│   │   ├── system_info.py    <- CPU, RAM, disk
│   │   ├── temps.py          <- GPU/CPU temperatures
│   │   ├── pc_tools.py       <- startup programs, event logs, deep scan
│   │   ├── system_ops.py     <- restore points, cleanup, disk cleanup
│   │   ├── file_tools.py     <- large/old/recent file search + read/list
│   │   ├── workspace_tools.py<- file write/append/edit + git clone/pull
│   │   ├── network.py        <- ping, traceroute, diagnostics
│   │   ├── crash_logs.py     <- Windows error event parsing
│   │   ├── campaign_tools.py <- NPC/event/quest tools for DM mode
│   │   ├── search.py         <- DuckDuckGo web search
│   │   ├── weather.py        <- weather via DuckDuckGo
│   │   ├── notes.py          <- note save/search
│   │   ├── rag.py            <- document upload/search/delete tools
│   │   ├── memory_tools.py   <- episodic search + session recall tools
│   │   └── time_tool.py      <- current datetime
│   └── static/
│       ├── login.html        <- login/register page
│       ├── app.html          <- main app shell (Dashboard | Chat | Settings)
│       ├── style.css         <- shared CSS (dark theme)
│       └── app.js            <- tab switching, SSE streaming, all UI logic
└── tests/
    ├── test_memory.py
    ├── test_brain.py
    ├── test_tools.py
    └── test_integration.py   <- requires Ollama running
```

---

## Running Tests

```bash
# Unit tests -- no Ollama needed
python -m pytest tests/test_memory.py tests/test_brain.py tests/test_tools.py -v

# Integration tests -- requires Ollama + models
python -m pytest tests/test_integration.py -v -s
```

---

## Configuration

All settings are in `kai/config.py`:

```python
CHAT_MODEL            = "qwen3.5:9b"          # chat + tools
REASONING_MODEL       = "qwen3:14b"           # heavy tasks (:model heavy)
EMBED_MODEL           = "qwen3-embedding:4b"  # episodic + document vector search
SUMMARY_MODEL         = "qwen3:14b"           # background summarization
CONTEXT_WINDOW        = 8192                   # tokens passed to Ollama
HISTORY_CHAR_LIMIT    = 12000                  # compress history when exceeded (~3k tokens)
HISTORY_COMPRESS_KEEP = 4                      # keep last N exchanges verbatim after compression
EPISODIC_TOP_K        = 5                      # archive entries injected per prompt
```
