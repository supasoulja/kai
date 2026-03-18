"""
Kai — entry point.
Usage:
    python cli.py
    python cli.py --debug
    python cli.py --model heavy   (use qwen3:14b with thinking)
"""
import argparse
import sys

import kai.config as cfg
from kai.brain import Brain, OllamaClient
from kai.memory.manager import MemoryManager
from kai.memory.procedural import seed_defaults
from kai.memory import semantic as _semantic
from kai.identity import seed_founding_entry
from kai.tools import registry as tool_registry
from kai import trace as trace_log


# ── Startup checks ─────────────────────────────────────────────────────────────

def check_ollama(ollama: OllamaClient) -> bool:
    if not ollama.is_alive():
        print("[!] Ollama is not running or not reachable.")
        print(f"    Connecting to: {cfg.OLLAMA_BASE_URL}")
        print("    Start it with: ollama serve")
        return False
    return True


def check_models(ollama: OllamaClient, required: list[str]) -> bool:
    installed = ollama.installed_models()
    # Normalize: strip tags for comparison
    installed_base = {m.split(":")[0] for m in installed}
    installed_full = set(installed)

    missing = []
    for model in required:
        base = model.split(":")[0]
        if model not in installed_full and base not in installed_base:
            missing.append(model)

    if missing:
        print(f"[!] Missing models: {', '.join(missing)}")
        for m in missing:
            print(f"    ollama pull {m}")
        return False
    return True


def startup_report(memory: MemoryManager, model: str) -> str:
    """Build the brief status line shown on launch."""
    facts  = memory.list_facts()
    recent = memory.recent_episodes(limit=1)
    rules  = memory.list_rules()

    name = next((f.value for f in facts if f.key == "user_name"), None)
    greeting = f"Hey {name}." if name else "Hey."

    last_session = (
        f"Last seen: {recent[0].timestamp.strftime('%b %d')}"
        if recent else "First session."
    )

    return (
        f"{greeting} Model: {model} | "
        f"Facts: {len(facts)} | Episodes: {len(recent)} | {last_session}"
    )


# ── CLI commands ───────────────────────────────────────────────────────────────

HELP_TEXT = """
Commands:
  :memory       show all memory (facts, rules, episodes)
  :facts        show semantic facts only
  :forget <key> delete a semantic fact
  :rules        show procedural rules
  :history      show last 10 episodic entries
  :trace        show last 10 turn traces (timing, tools used)
  :tools        list registered tools
  :vector       show vector table stats (episodic + RAG embeddings)
  :model heavy  switch to qwen3:14b (thinking ON) for this session
  :model fast   switch back to qwen3:8b
  :debug        toggle debug mode
  :help         show this
  :quit / exit  exit
"""


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

    elif command == ":model":
        model = arg.strip().lower()
        if model == "heavy":
            brain.model = cfg.REASONING_MODEL
            brain._think = True   # enable chain-of-thought for reasoning model
            print(f"  Switched to: {cfg.REASONING_MODEL} (thinking ON)")
        elif model in ("fast", "default"):
            brain.model = cfg.CHAT_MODEL
            brain._think = False  # fast mode, no chain-of-thought
            print(f"  Switched to: {cfg.CHAT_MODEL} (thinking OFF)")
        else:
            print(f"  Unknown model alias '{model}'. Use 'heavy' or 'fast'.")

    elif command == ":trace":
        entries = trace_log.recent(limit=10)
        if not entries:
            print("  No trace entries yet.")
        else:
            for e in entries:
                tools = ", ".join(e.tool_calls) if e.tool_calls else "none"
                print(f"  [{e.trace_id}] {e.timestamp[:19]}  {e.elapsed_ms}ms  "
                      f"model={e.model.split(':')[0]}  tools=[{tools}]  "
                      f"ctx={e.context_len}ch  resp={e.response_len}ch")
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

    elif command == ":debug":
        cfg.DEBUG = not cfg.DEBUG
        print(f"  Debug mode: {'ON' if cfg.DEBUG else 'OFF'}")

    elif command == ":help":
        print(HELP_TEXT)

    else:
        return False  # unknown command — pass to brain

    return True


def _show_memory(memory: MemoryManager) -> None:
    facts  = memory.list_facts()
    rules  = memory.list_rules()
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
    from kai.db import get_conn, sqlite_vec_available

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
    parser = argparse.ArgumentParser(description="Kai — local AI agent")
    parser.add_argument("--debug",  action="store_true", help="Enable debug output")
    parser.add_argument("--model",  choices=["fast", "heavy"], default="fast",
                        help="Model to use: fast (qwen3:8b) or heavy (qwen3:14b, thinking ON)")
    args = parser.parse_args()

    if args.debug:
        cfg.DEBUG = True

    active_model = cfg.REASONING_MODEL if args.model == "heavy" else cfg.CHAT_MODEL
    required_models = [active_model, cfg.EMBED_MODEL]

    # ── Startup checks ─────────────────────────────────────────────────────────
    ollama = OllamaClient()

    if not check_ollama(ollama):
        sys.exit(1)

    if not check_models(ollama, required_models):
        print("\n[!] Pull missing models then restart.")
        sys.exit(1)

    # ── Initialize memory + identity ───────────────────────────────────────────
    embed_fn = ollama.embed  # real embeddings from qwen3-embedding:4b
    memory   = MemoryManager(embed_fn=embed_fn)
    _semantic.migrate()    # remove stale volatile sys_* keys from previous sessions

    seed_defaults()        # set procedural rules if first run
    seed_founding_entry()  # log the founding conversation if first run

    # ── Initialize brain ───────────────────────────────────────────────────────
    brain = Brain(memory=memory, model=active_model, ollama=ollama,
                  tool_registry=tool_registry)

    # ── Pre-warm: build indexes now so the first message has zero cold-start ──
    brain._ensure_memory_router()
    brain._ensure_tool_index()

    # ── Upgrade awareness ──────────────────────────────────────────────────────
    from kai.upgrade import check_for_upgrade
    upgrade_msg = check_for_upgrade(embed_fn=ollama.embed)
    if upgrade_msg:
        print(f"\n  [upgrade] {upgrade_msg[:100]}")

    # ── Startup report ─────────────────────────────────────────────────────────
    print()
    print(startup_report(memory, active_model))
    print("Type :help for commands. Ctrl+C or 'exit' to quit.\n")

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
