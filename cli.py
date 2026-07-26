"""
Kai — entry point.
Usage:
    python cli.py
    python cli.py --debug
    python cli.py --mode thinking   (modes: thinking | normal | creative | crazy)
"""

import argparse
import os
import sys

import kai.config as cfg
from kai.core import bootstrap
from kai.core import trace as trace_log
from kai.core.brain import Brain
from kai.llm.ollama import OllamaClient
from kai.memory.manager import MemoryManager
from kai.tools import registry as tool_registry

# ── Startup checks ─────────────────────────────────────────────────────────────


def check_ollama(ollama: OllamaClient) -> bool:
    if not ollama.is_alive():
        print("[!] Ollama is not running or not reachable.")
        print(f"    Connecting to: {cfg.OLLAMA_BASE_URL}")
        print("    Start it with: ollama serve")
        return False
    return True


def check_models(ollama: OllamaClient, required: list[str]) -> bool:
    missing = [m for m in required if not bootstrap.is_model_installed(ollama, m)]
    if missing:
        print(f"[!] Missing models: {', '.join(missing)}")
        for m in missing:
            print(f"    ollama pull {m}")
        return False
    return True


def startup_report(memory: MemoryManager, model: str) -> str:
    """Build the brief status line shown on launch."""
    from kai.core.sleep import load_welcome_back

    facts = memory.list_facts()
    recent = memory.recent_episodes(limit=1)

    name = next((f.value for f in facts if f.key == "user_name"), None)
    greeting = f"Hey {name}." if name else "Hey."

    last_session = (
        f"Last seen: {recent[0].timestamp.strftime('%b %d')}" if recent else "First session."
    )

    report = (
        f"{greeting} Model: {model} | "
        f"Facts: {len(facts)} | Episodes: {len(recent)} | {last_session}"
    )

    # Show welcome-back message if Kai left herself a note
    wb = load_welcome_back()
    if wb:
        report += f"\n\n  [Kai's note to herself]\n  {wb[:200]}{'...' if len(wb) > 200 else ''}"

    return report


# ── CLI commands ───────────────────────────────────────────────────────────────

HELP_TEXT = """
Commands:
  :memory       show all memory (facts, rules, episodes)
  :facts        show semantic facts only
  :forget <key> delete a semantic fact
  :rules        show procedural rules
  :history      show last 10 episodic entries
  :sleep        show Kai's last welcome-back note
  :trace        show last 10 turn traces (timing, tools used)
  :flow [id]    replay a turn step by step (model calls, thinking, tools)
  :flowlive     toggle live flow — print every internal step as it happens
  :tools        list registered tools
  :vector       show vector table stats (episodic + RAG embeddings)
  :mode <name>  set generation mode: thinking | normal | creative | crazy
  :toollevel <name>  which model runs tool calls: light | balanced | deep | off
  :temp <0-2>   set temperature for this session (overrides the mode)
  :model <name> switch to a user-added model (see :models)
  :models       list all configured models
  :debug        toggle debug mode
  :help         show this
  :quit / exit  exit
"""


# :flowlive state — the currently-subscribed live printer (None = off)
_flow_live_tap = None


def handle_command(cmd: str, brain: Brain, memory: MemoryManager) -> bool:
    """
    Handle a colon-prefixed command.
    Returns True if handled, False if it should be passed to the brain.
    """
    parts = cmd.strip().split(None, 1)
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if command == ":memory":
        _show_memory(memory)

    elif command == ":facts":
        facts = memory.list_facts()
        if facts:
            for f in facts:
                print(f"  {f.key} = {f.value}  [{f.source}]")
        else:
            print("  No facts stored yet.")

    elif command == ":forget":
        if arg:
            memory.delete_fact(arg.strip())
            print(f"  Deleted: {arg.strip()}")
        else:
            print("  Usage: :forget <key>")

    elif command == ":rules":
        rules = memory.list_rules()
        for r in rules:
            print(f"  {r.key} = {r.value}")

    elif command == ":history":
        episodes = memory.recent_episodes(limit=10)
        for ep in episodes:
            print(f"\n  [{ep.timestamp.strftime('%b %d %H:%M')}] {ep.content[:120]}...")

    elif command == ":models":
        from kai.llm import models as _models

        all_models = _models.list_models()
        active_id = brain.model
        print("  Configured models:")
        for m in all_models:
            marker = " *" if m["ollama_id"] == active_id else "  "
            think = " (think)" if m["think"] else ""
            tag = " [built-in]" if m.get("builtin") else ""
            print(f"  {marker} {m['name']:12s}  {m['ollama_id']}{think}{tag}")
        print("\n  Switch with :model <name>")

    elif command == ":mode":
        key = arg.strip().lower()
        if key in cfg.GEN_PRESETS:
            r = brain.apply_preset(key)
            try:
                memory.set_fact("gen_preset", key, source="user_setting")
            except Exception:
                pass
            print(
                f"  Mode: {r['label']}  (thinking {'ON' if r['think'] else 'OFF'}, temp {r['temp']:.2f})"
            )
        else:
            print(f"  Usage: :mode <{' | '.join(cfg.GEN_PRESETS)}>")

    elif command == ":temp":
        try:
            t = brain.set_temperature(float(arg.strip()))
            print(f"  Temperature: {t:.2f} (this session)")
        except ValueError:
            print(f"  Usage: :temp <{cfg.TEMP_MIN}-{cfg.TEMP_MAX}>")

    elif command == ":toollevel":
        key = arg.strip().lower()
        if key in cfg.TOOL_MODEL_LEVELS:
            r = brain.apply_tool_level(key)
            try:
                memory.set_fact("tool_level", key, source="user_setting")
            except Exception:
                pass
            note = ""
            if r["model"] and not r["available"]:
                note = (
                    f"  (not installed — ollama pull {r['model']}; "
                    "falling back to main model + thinking)"
                )
            print(f"  Tool level: {r['label']}{note}")
        else:
            print(f"  Usage: :toollevel <{' | '.join(cfg.TOOL_MODEL_LEVELS)}>")

    elif command == ":model":
        from kai.llm import models as _models

        entry = _models.get_model(arg.strip())
        if entry:
            brain.model = entry["ollama_id"]
            brain._think = entry.get("think", False)
            think_str = "ON" if brain._think else "OFF"
            print(f"  Switched to: {entry['ollama_id']} (thinking {think_str})")
        else:
            print(f"  Unknown model '{arg.strip()}'. Use :models to see available options.")

    elif command == ":flow":
        from kai.core import flow as _flow

        tid = arg.strip()
        if not tid:
            turns = _flow.recent_turns(limit=5)
            if not turns:
                print("  No flow recorded yet (is FLOW_TRACE on in config.py?).")
            else:
                tid = turns[0]["trace_id"]
                print("  Recent turns (showing the newest — :flow <id> for older):")
                from datetime import datetime as _dt

                for t in turns:
                    when = _dt.fromtimestamp(t["ts"]).strftime("%H:%M:%S")
                    print(f"    {t['trace_id']}  {when}  {t['steps']:3d} steps  {t['input']!r}")
                print()
        if tid:
            steps = _flow.get_flow(tid)
            if not steps:
                print(f"  No flow recorded for {tid!r}.")
            for s in steps:
                kind = s.pop("kind", "?")
                s.pop("ts", None)
                detail = "  ".join(
                    f"{k}={str(v)[:160]!r}" for k, v in s.items() if v not in (None, "", "none")
                )
                print(f"  [{kind}] {detail}")

    elif command == ":flowlive":
        global _flow_live_tap
        from kai.core import flow as _flow

        if _flow_live_tap is None:

            def _tap(tid, kind, data):
                detail = "  ".join(
                    f"{k}={str(v)[:100]!r}" for k, v in data.items() if v not in (None, "", "none")
                )
                print(f"\n  ⚡[{kind}] {detail}")

            _flow_live_tap = _tap
            _flow.subscribe(_tap)
            print("  Live flow ON — every internal step prints as it happens. :flowlive to stop.")
        else:
            _flow.unsubscribe(_flow_live_tap)
            _flow_live_tap = None
            print("  Live flow OFF.")

    elif command == ":trace":
        entries = trace_log.recent(limit=10)
        if not entries:
            print("  No trace entries yet.")
        else:
            for e in entries:
                tools = ", ".join(e.tool_calls) if e.tool_calls else "none"
                print(
                    f"  [{e.trace_id}] {e.timestamp[:19]}  {e.elapsed_ms}ms  "
                    f"model={e.model.split(':')[0]}  tools=[{tools}]  "
                    f"ctx={e.context_len}ch  resp={e.response_len}ch"
                )
                if cfg.DEBUG:
                    print(f"    q: {e.user_input[:80]}")

    elif command == ":tools":
        if brain.tool_registry:
            tools = brain.tool_registry.list_tools()
            print(f"  {len(tools)} tools registered:")
            for t in tools:
                print(f"    {t}")
        else:
            print("  No tool registry loaded.")

    elif command == ":vector":
        _show_vector_stats()

    elif command == ":sleep":
        from kai.config import MEMORY_DIR
        from kai.core.sleep import load_welcome_back

        wb = load_welcome_back()
        if wb:
            print(f"\n  [Pending welcome-back note]\n  {wb}\n")
        else:
            log_file = MEMORY_DIR / "sleep_log.txt"
            if log_file.exists():
                lines = log_file.read_text(encoding="utf-8").strip().split("\n---")
                last = lines[-1].strip() if lines else ""
                if last:
                    print(f"\n  [Last sleep note]\n  {last}\n")
                else:
                    print("  No sleep notes yet.")
            else:
                print("  No sleep notes yet. Kai hasn't gone to sleep.")

    elif command == ":debug":
        cfg.DEBUG = not cfg.DEBUG
        print(f"  Debug mode: {'ON' if cfg.DEBUG else 'OFF'}")

    elif command == ":help":
        print(HELP_TEXT)

    else:
        return False  # unknown command — pass to brain

    return True


def _show_memory(memory: MemoryManager) -> None:
    facts = memory.list_facts()
    rules = memory.list_rules()
    recent = memory.recent_episodes(limit=5)

    print("\n── Semantic Facts ──")
    if facts:
        for f in facts:
            print(f"  {f.key} = {f.value}  [{f.source}]")
    else:
        print("  (none)")

    print("\n── Procedural Rules ──")
    if rules:
        for r in rules:
            print(f"  {r.key} = {r.value}")
    else:
        print("  (none)")

    print("\n── Recent Episodes ──")
    if recent:
        for ep in recent:
            print(f"  [{ep.timestamp.strftime('%b %d %H:%M')}] {ep.content[:100]}...")
    else:
        print("  (none)")
    print()


def _show_vector_stats() -> None:
    """Display stats about all vector tables (episodic + RAG)."""
    from kai.store.db import get_conn, sqlite_vec_available

    if not sqlite_vec_available():
        print("  sqlite-vec is not installed — no vector tables available.")
        return

    conn = get_conn()

    # ── Episodic vectors ──────────────────────────────────────────────────
    print("\n── Episodic Vectors ──")
    try:
        total = conn.execute("SELECT COUNT(*) FROM episodic_vec").fetchone()[0]
        print(f"  Total vectors: {total}")

        if total > 0:
            # Break down by entry_type
            rows = conn.execute(
                "SELECT e.entry_type, COUNT(*) "
                "FROM episodic_entries e "
                "JOIN episodic_vec v ON e.rowid = v.rowid "
                "GROUP BY e.entry_type ORDER BY COUNT(*) DESC"
            ).fetchall()
            for entry_type, count in rows:
                print(f"    {entry_type}: {count}")

            # Show recent entries with vectors
            print("\n  Recent entries with vectors:")
            recent = conn.execute(
                "SELECT e.entry_type, e.timestamp, substr(e.content, 1, 80) "
                "FROM episodic_entries e "
                "JOIN episodic_vec v ON e.rowid = v.rowid "
                "ORDER BY e.timestamp DESC LIMIT 10"
            ).fetchall()
            for entry_type, ts, preview in recent:
                ts_short = ts[:16].replace("T", " ")
                print(f"    [{ts_short}] ({entry_type}) {preview}...")

        # Entries WITHOUT vectors
        no_vec = conn.execute(
            "SELECT COUNT(*) FROM episodic_entries e "
            "LEFT JOIN episodic_vec v ON e.rowid = v.rowid "
            "WHERE v.rowid IS NULL"
        ).fetchone()[0]
        if no_vec > 0:
            print(f"\n  Entries without vectors: {no_vec}")
            type_rows = conn.execute(
                "SELECT e.entry_type, COUNT(*) "
                "FROM episodic_entries e "
                "LEFT JOIN episodic_vec v ON e.rowid = v.rowid "
                "WHERE v.rowid IS NULL "
                "GROUP BY e.entry_type"
            ).fetchall()
            for entry_type, count in type_rows:
                print(f"    {entry_type}: {count}")

    except Exception as e:
        print(f"  Error reading episodic_vec: {e}")

    # ── RAG vectors ───────────────────────────────────────────────────────
    print("\n── RAG Document Vectors ──")
    try:
        total = conn.execute("SELECT COUNT(*) FROM rag_chunks_vec").fetchone()[0]
        print(f"  Total chunk vectors: {total}")

        if total > 0:
            # Breakdown by document
            rows = conn.execute(
                "SELECT d.filename, COUNT(*) "
                "FROM rag_chunks c "
                "JOIN rag_chunks_vec v ON c.rowid = v.rowid "
                "JOIN rag_documents d ON d.doc_id = c.doc_id "
                "GROUP BY d.filename ORDER BY COUNT(*) DESC"
            ).fetchall()
            for filename, count in rows:
                print(f"    {filename}: {count} chunks")

        # Chunks without vectors
        no_vec = conn.execute(
            "SELECT COUNT(*) FROM rag_chunks c "
            "LEFT JOIN rag_chunks_vec v ON c.rowid = v.rowid "
            "WHERE v.rowid IS NULL"
        ).fetchone()[0]
        if no_vec > 0:
            print(f"  Chunks without vectors: {no_vec}")

    except Exception as e:
        print(f"  Error reading rag_chunks_vec: {e}")

    # ── DB file size ──────────────────────────────────────────────────────
    try:
        db_size = cfg.DB_PATH.stat().st_size
        if db_size < 1024 * 1024:
            size_str = f"{db_size / 1024:.1f} KB"
        else:
            size_str = f"{db_size / (1024 * 1024):.1f} MB"
        print(f"\n  DB file size: {size_str}")
    except Exception:
        pass

    print()


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    os.environ.setdefault("KAI_ENTRYPOINT", "cli")
    parser = argparse.ArgumentParser(description="Kai — local AI agent")
    parser.add_argument("--debug", action="store_true", help="Enable debug output")
    parser.add_argument(
        "--mode",
        choices=list(cfg.GEN_PRESETS),
        default=cfg.DEFAULT_PRESET,
        help="Generation mode: thinking | normal | creative | crazy",
    )
    args = parser.parse_args()

    if args.debug:
        cfg.DEBUG = True

    active_model = cfg.CHAT_MODEL
    # Embed model is CPU-based now — only need the chat model in Ollama
    required_models = [active_model]

    # ── Startup checks ─────────────────────────────────────────────────────────
    ollama = OllamaClient()

    if not check_ollama(ollama):
        sys.exit(1)

    if not check_models(ollama, required_models):
        print("\n[!] Pull missing models then restart.")
        sys.exit(1)

    # ── Fast CPU embedding ────────────────────────────────────────────────────
    from kai.llm.embed import embed as fast_embed
    from kai.llm.embed import warm_up as _warm_embed

    _warm_embed()  # pre-load ONNX model (~50 MB first-run download)

    # ── Initialize memory + identity ───────────────────────────────────────────
    memory = MemoryManager(embed_fn=fast_embed)
    bootstrap.run_migrations_and_seed()  # migrate stale keys + seed procedural rules

    # ── Initialize brain ───────────────────────────────────────────────────────
    from kai.skills import build_skill_registry

    brain = Brain(
        memory=memory,
        model=active_model,
        ollama=ollama,
        tool_registry=tool_registry,
        skill_registry=build_skill_registry(tool_registry),
    )
    # Apply generation mode: CLI flag overrides the saved preference.
    _preset = (
        args.mode
        if args.mode != cfg.DEFAULT_PRESET
        else (memory.get_fact("gen_preset") or cfg.DEFAULT_PRESET)
    )
    if _preset not in cfg.GEN_PRESETS:
        _preset = cfg.DEFAULT_PRESET
    brain.apply_preset(_preset)
    # Restore the saved tool-model level (which model runs tool rounds).
    _tl = memory.get_fact("tool_level") or cfg.DEFAULT_TOOL_LEVEL
    if _tl not in cfg.TOOL_MODEL_LEVELS:
        _tl = cfg.DEFAULT_TOOL_LEVEL
    brain.apply_tool_level(_tl)

    # ── Pre-warm: build indexes now so the first message has zero cold-start ──
    brain._ensure_memory_router()
    brain._ensure_tool_index()

    # ── Upgrade awareness ──────────────────────────────────────────────────────
    from kai.system.upgrade import check_for_upgrade

    upgrade_msg = check_for_upgrade(embed_fn=fast_embed)
    if upgrade_msg:
        print(f"\n  [upgrade] {upgrade_msg[:100]}")

    # Register shutdown hook: sleep cycle + HQ re-embed
    import atexit

    atexit.register(lambda: bootstrap.run_shutdown(ollama, [brain]))

    # ── Startup report ─────────────────────────────────────────────────────────
    print()
    print(startup_report(memory, active_model))
    print("Type :help for commands. Ctrl+C or 'exit' to quit.\n")

    # ── Kai opens the conversation herself (cold open: uses her welcome-back note) ──
    try:
        print("Kai: ", end="", flush=True)
        got = False
        for token, done, _ in brain.generate_greeting(fresh=False):
            if not done:
                got = True
                print(token, end="", flush=True)
        print("\n" if got else "\r", end="", flush=True)
        if got:
            print()
    except Exception:
        print()  # never let a greeting failure block startup

    # ── REPL ───────────────────────────────────────────────────────────────────
    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nKai: Later.")
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit", ":quit"):
            print("Kai: Later.")
            break

        # Colon commands
        if user_input.startswith(":"):
            handled = handle_command(user_input, brain, memory)
            if not handled:
                print(f"  Unknown command '{user_input}'. Try :help")
            continue

        # Normal turn — stream tokens as they arrive
        try:
            print("Kai: ", end="", flush=True)
            for token, done, _ in brain.run_stream(user_input):
                if not done:
                    print(token, end="", flush=True)
            print("\n")
        except Exception as e:
            print()  # newline after partial output
            if cfg.DEBUG:
                import traceback

                traceback.print_exc()
            print(f"[!] Error: {e}\n")


if __name__ == "__main__":
    main()
