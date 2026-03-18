"""
Tool registry — registers tools and routes the model's tool calls to them.

Each tool is a plain Python function decorated with @registry.tool().
The registry builds the JSON schema Ollama needs and dispatches calls.

Alias learning:
  When the model hallucinates a tool name (e.g. "pc.startups" instead of
  "pc.startup_programs"), the brain calls learn_alias(). The registry finds
  the closest real tool by string similarity, registers the mapping, and
  persists it to SQLite. Future schemas include alias names so the model can
  call either form — both route to the same function.
"""
from __future__ import annotations
import copy
import difflib
import math
from datetime import datetime
from typing import Any, Callable
from kai.config import DEBUG


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, dict] = {}          # name → {fn, schema}
        self._aliases: dict[str, str] = {}         # alias → real tool name
        self._aliases_loaded: bool = False         # lazy-loaded after tools register

    # ── Alias persistence ─────────────────────────────────────────────────────

    def _ensure_aliases_loaded(self) -> None:
        """Load alias table from DB once, after all tools are registered."""
        if self._aliases_loaded:
            return
        self._aliases_loaded = True
        try:
            from kai.db import get_conn
            conn = get_conn()
            rows = conn.execute("SELECT alias, target FROM tool_aliases").fetchall()
            for alias, target in rows:
                self._aliases[alias] = target  # filter stale entries at use-time
        except Exception:
            if DEBUG:
                import traceback; traceback.print_exc()

    def _persist_alias(self, alias: str, target: str, similarity: float) -> None:
        try:
            from kai.db import get_conn
            conn = get_conn()
            conn.execute("""
                INSERT INTO tool_aliases (alias, target, similarity, seen_count, created_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(alias) DO UPDATE SET seen_count = seen_count + 1
            """, (alias, target, similarity, datetime.now().isoformat()))
            conn.commit()
        except Exception:
            if DEBUG:
                import traceback; traceback.print_exc()

    # ── Alias learning ────────────────────────────────────────────────────────

    def learn_alias(self, hallucinated_name: str, threshold: float = 0.55) -> str | None:
        """
        Find the closest real tool to hallucinated_name by string similarity.
        Prefers tools in the same namespace (same prefix before the dot).
        If similarity >= threshold, register and persist the alias.
        Returns the target tool name on success, None otherwise.
        """
        self._ensure_aliases_loaded()

        if hallucinated_name in self._tools:
            return hallucinated_name  # it's already a real tool

        if hallucinated_name in self._aliases:
            target = self._aliases[hallucinated_name]
            if target in self._tools:
                self._persist_alias(hallucinated_name, target, 1.0)  # bump seen_count
                return target

        real_names = list(self._tools.keys())
        ns = hallucinated_name.split(".")[0] if "." in hallucinated_name else ""
        # Prefer same-namespace candidates; fall back to all tools
        candidates = [n for n in real_names if n.startswith(ns + ".")] or real_names

        best_name, best_score = None, 0.0
        for candidate in candidates:
            score = difflib.SequenceMatcher(None, hallucinated_name, candidate).ratio()
            if score > best_score:
                best_score, best_name = score, candidate

        if best_name and best_score >= threshold:
            self._aliases[hallucinated_name] = best_name
            self._persist_alias(hallucinated_name, best_name, best_score)
            if DEBUG:
                print(f"[alias] learned: {hallucinated_name!r} → {best_name!r} "
                      f"(score={best_score:.2f})")
            return best_name

        if DEBUG:
            print(f"[alias] no match for {hallucinated_name!r} "
                  f"(best={best_name!r}, score={best_score:.2f})")
        return None

    # ── Alias schema helpers ──────────────────────────────────────────────────

    def _alias_schemas(self, for_names: set[str] | None = None) -> list[dict]:
        """
        Build schemas for known aliases.
        If for_names is given, only include aliases whose target is in that set.
        """
        schemas = []
        for alias, target in self._aliases.items():
            if target not in self._tools:
                continue  # stale alias, target was removed
            if for_names is not None and target not in for_names:
                continue
            schema = copy.deepcopy(self._tools[target]["schema"])
            schema["function"]["name"] = alias
            schemas.append(schema)
        return schemas

    def tool(self, name: str, description: str, parameters: dict | None = None):
        """
        Decorator to register a function as a tool.

        @registry.tool(
            name="time.now",
            description="Return the current date and time.",
        )
        def get_time() -> str:
            ...
        """
        def decorator(fn: Callable) -> Callable:
            self._tools[name] = {
                "fn": fn,
                "schema": _build_schema(name, description, parameters or {}),
            }
            return fn
        return decorator

    def get_schema(self) -> list[dict]:
        """Return the list of tool schemas to pass to Ollama, including aliases."""
        self._ensure_aliases_loaded()
        return [t["schema"] for t in self._tools.values()] + self._alias_schemas()

    def execute(self, name: str, args: dict) -> Any:
        """Call a registered tool by name with the given arguments."""
        if name in self._tools:
            return self._tools[name]["fn"](**args)
        self._ensure_aliases_loaded()
        target = self._aliases.get(name)
        if target and target in self._tools:
            return self._tools[target]["fn"](**args)
        raise KeyError(f"Unknown tool: {name!r}")

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def build_category_index(
        self, embed_batch_fn: Callable[[list[str]], list[list[float]]]
    ) -> dict[str, list[float]]:
        """
        Embed all category descriptions in one batch call.
        Returns {category_name: embedding}. Call once at startup; cache in Brain.
        """
        names = list(_TOOL_CATEGORIES.keys())
        descs = [_TOOL_CATEGORIES[n]["description"] for n in names]
        vecs = embed_batch_fn(descs)
        return dict(zip(names, vecs))

    def select_tools_by_category(
        self,
        query_embedding: list[float],
        category_index: dict[str, list[float]],
        top_k: int = 2,
    ) -> list[dict]:
        """
        Rank the 10 categories by cosine similarity, return every tool that belongs
        to the top-k categories. This guarantees related tools always arrive as a
        complete set — e.g. all system_health tools together — not scattered picks.
        Falls back to the full schema if the index is empty.
        """
        if not category_index:
            return self.get_schema()
        scores = sorted(
            ((cat, _cosine(query_embedding, emb)) for cat, emb in category_index.items()),
            key=lambda t: t[1],
            reverse=True,
        )
        selected: set[str] = set()
        for cat_name, score in scores[:top_k]:
            if score < 0.15:   # category doesn't match at all — stop collecting
                break
            selected.update(_TOOL_CATEGORIES.get(cat_name, {}).get("tools", []))
        if not selected:
            return self.get_schema()
        # search.web is always included regardless of category — it's the universal
        # fallback when a system tool returns an error code and the model needs to look it up.
        if "search.web" in self._tools:
            selected.add("search.web")
        if DEBUG:
            chosen = [(c, f"{s:.2f}") for c, s in scores[:top_k]]
            print(f"[tool select] categories={chosen}  tools={len(selected)}")
        schemas = [t["schema"] for name, t in self._tools.items() if name in selected]
        # Include alias schemas for selected tools so the model can call either form
        schemas += self._alias_schemas(for_names=selected)
        return schemas


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


# ── Tool categories ────────────────────────────────────────────────────────
# Descriptions are written in user-query language so embeddings match what people ask,
# not sysadmin jargon. Brain embeds these (10 calls) instead of all 43 tool descriptions.
# top_k=2 categories covers most queries; error escalation handles edge cases.
_TOOL_CATEGORIES: dict[str, dict] = {
    "system_health": {
        "description": (
            "Check how my PC is doing: CPU load, GPU usage, RAM, temperatures, fan speeds, "
            "crash logs, GPU crash history, game crashes, Windows event logs, deep system scan. "
            "Is my PC okay? What is running hot? Hardware status and diagnostics."
        ),
        "tools": [
            "system.info", "system.temps", "system.crashes", "system.gpu_crashes",
            "system.game_crashes", "pc.event_logs", "pc.deep_scan",
        ],
    },
    "system_control": {
        "description": (
            "Fix, clean, and optimize my PC: clear temp files, run disk cleanup, "
            "kill background processes, create a system restore point, repair Windows files. "
            "Speed up PC, gaming time, pre-game prep, free up RAM and memory."
        ),
        "tools": [
            "system.clear_temp_files", "system.run_disk_cleanup", "system.kill_process",
            "system.create_restore_point", "system.repair_files",
        ],
    },
    "startup_and_updates": {
        "description": (
            "Manage startup programs: what runs on boot, disable slow startup apps, "
            "check for Windows updates, install updates, what is slowing my boot time."
        ),
        "tools": [
            "pc.startup_programs", "system.disable_startup_program", "pc.windows_updates",
        ],
    },
    "disk_analysis": {
        "description": (
            "Analyze disk and storage space: how much space is free, what is taking up space, "
            "find the largest files, find old unused files, see recently changed files, "
            "disk usage breakdown by folder."
        ),
        "tools": [
            "files.disk_usage", "files.find_large", "files.find_old", "files.recent",
        ],
    },
    "file_operations": {
        "description": (
            "Read, write, create, edit, append to, and list files and folders. "
            "Open a file, view its contents, modify a script or config file, "
            "browse a directory, save text output to a file."
        ),
        "tools": [
            "files.read", "files.write", "files.append", "files.edit", "files.list",
        ],
    },
    "network": {
        "description": (
            "Network and internet connectivity: ping a server, run traceroute, "
            "check IP address, Wi-Fi and ethernet details, test my internet connection, "
            "diagnose high ping, latency, packet loss, full network diagnostic."
        ),
        "tools": [
            "network.ping", "network.traceroute", "network.full_diagnostic", "pc.network_info",
        ],
    },
    "search_and_info": {
        "description": (
            "Search the web for current news, game updates, prices, guides, tutorials, "
            "documentation, or anything needing live or recent information. "
            "Also: current date, time, and local weather forecast."
        ),
        "tools": ["search.web", "weather.current", "time.now"],
    },
    "notes_and_memory": {
        "description": (
            "Save, find, and read personal notes and reminders. "
            "Look up things I asked Kai to remember across sessions. "
            "Read full transcripts of past conversations."
        ),
        "tools": ["notes.save", "notes.search", "notes.list", "memory.get_detail"],
    },
    "workspace_and_code": {
        "description": (
            "Work with git repositories and code: clone a repo, pull latest changes, "
            "list allowed repositories, manage code workspace files."
        ),
        "tools": [
            "workspace.git_clone", "workspace.git_pull", "workspace.git_list_allowed",
        ],
    },
    "docs_rag": {
        "description": (
            "Search through uploaded documents, PDFs, Word files, text files, code, and CSV files. "
            "Find information inside uploaded files, list what documents have been uploaded, "
            "or remove a document. Use when the user asks about content in a file they gave me."
        ),
        "tools": ["docs.search", "docs.list", "docs.delete"],
    },
    "campaign_dm": {
        "description": (
            "D&D campaign and dungeon master mode: save NPCs, log session events, "
            "update quests, recall campaign lore and history, check active campaign status."
        ),
        "tools": [
            "campaign.npc_save", "campaign.event_log", "campaign.quest_update",
            "campaign.recall", "campaign.status",
        ],
    },
}


def _build_schema(name: str, description: str, parameters: dict) -> dict:
    """Build an Ollama-compatible tool schema."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": parameters,
                "required": [k for k, v in parameters.items()
                             if v.get("required", False)],
            },
        },
    }


# ── Default registry (used by cli.py) ──────────────────────────────────────────

registry = ToolRegistry()
