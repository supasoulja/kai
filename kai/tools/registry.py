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
from datetime import datetime
from typing import Any, Callable
import kai.config as cfg
from kai.llm.vecmath import cosine as _cosine


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
            from kai.store.db import get_conn
            conn = get_conn()
            rows = conn.execute("SELECT alias, target FROM tool_aliases").fetchall()
            for alias, target in rows:
                self._aliases[alias] = target  # filter stale entries at use-time
        except Exception:
            if cfg.DEBUG:
                import traceback; traceback.print_exc()

    def _persist_alias(self, alias: str, target: str, similarity: float) -> None:
        try:
            from kai.store.db import get_conn
            conn = get_conn()
            conn.execute("""
                INSERT INTO tool_aliases (alias, target, similarity, seen_count, created_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(alias) DO UPDATE SET seen_count = seen_count + 1
            """, (alias, target, similarity, datetime.now().isoformat()))
            conn.commit()
        except Exception:
            if cfg.DEBUG:
                import traceback; traceback.print_exc()

    # ── Alias learning ────────────────────────────────────────────────────────

    def learn_alias(self, hallucinated_name: str, threshold: float = 0.55,
                    args: dict | None = None) -> str | None:
        """
        Find the closest real tool to hallucinated_name by string similarity.
        Prefers tools in the same namespace (same prefix before the dot).
        If similarity >= threshold, register and persist the alias.

        When `args` is given, the target must also accept them: every provided
        argument name has to exist in the target's schema. A close name with
        incompatible args is a different intent, not a misspelling — e.g. a
        hallucinated "system.execute_command" must not redirect to
        system.temps. Rejected matches are never persisted.

        Returns the target tool name on success, None otherwise.
        """
        self._ensure_aliases_loaded()

        if hallucinated_name in self._tools:
            return hallucinated_name  # it's already a real tool

        if hallucinated_name in self._aliases:
            target = self._aliases[hallucinated_name]
            if target in self._tools and self._args_fit(target, args):
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
            if not self._args_fit(best_name, args):
                if cfg.DEBUG:
                    print(f"[alias] rejected {hallucinated_name!r} → {best_name!r} "
                          f"(score={best_score:.2f}) — args don't fit the target schema")
                return None
            self._aliases[hallucinated_name] = best_name
            self._persist_alias(hallucinated_name, best_name, best_score)
            if cfg.DEBUG:
                print(f"[alias] learned: {hallucinated_name!r} → {best_name!r} "
                      f"(score={best_score:.2f})")
            return best_name

        if cfg.DEBUG:
            print(f"[alias] no match for {hallucinated_name!r} "
                  f"(best={best_name!r}, score={best_score:.2f})")
        return None

    def _args_fit(self, tool_name: str, args: dict | None) -> bool:
        """True when every provided argument name exists in the tool's schema."""
        if not args:
            return True
        props = (self._tools[tool_name]["schema"]["function"]
                 .get("parameters", {}).get("properties", {}))
        return all(k in props for k in args)

    # ── Alias schema helpers ──────────────────────────────────────────────────

    def _alias_schemas(self, for_names: set[str] | None = None,
                       exclude: set[str] | None = None) -> list[dict]:
        """
        Build schemas for known aliases.
        If for_names is given, only include aliases whose target is in that set.
        If exclude is given, drop aliases whose target is turned off — so a
        disabled tool can't sneak back in under an alias name.
        """
        schemas = []
        for alias, target in self._aliases.items():
            if target not in self._tools:
                continue  # stale alias, target was removed
            if for_names is not None and target not in for_names:
                continue
            if exclude and target in exclude:
                continue
            schema = copy.deepcopy(self._tools[target]["schema"])
            schema["function"]["name"] = alias
            schemas.append(schema)
        return schemas

    def tool(self, name: str, description: str, parameters: dict | None = None,
             *, category: str | None = None, label: str | None = None,
             risk: str | None = None, category_description: str | None = None):
        """
        Decorator to register a function as a tool.

        Metadata (category / label / risk) may be declared INLINE so a new tool
        is self-describing — no need to also hand-edit the central category,
        label, and risk tables. Inline values are reflected into those tables at
        registration (see _register_metadata), so every existing consumer — the
        confirm gate, semantic tool-selection, and the metadata audit — keeps
        working unchanged. Omit them and the tool falls back to whatever the
        central tables say, exactly as before. This is the seam that makes tools
        (and, later, marketplace packs) pluggable without touching core files.

        @registry.tool(
            name="time.now",
            description="Return the current date and time.",
        )
        def get_time() -> str:
            ...

        @registry.tool(
            name="acme.deploy_check",
            description="Run pre-deploy health checks.",
            parameters={"env": {"type": "string", "required": True}},
            category="workspace_and_code",   # existing category, or a brand-new one
            label="Running deploy checks",    # shown in the UI while it runs
            risk="caution",                   # safe | caution | destructive
        )
        def deploy_check(env: str) -> str:
            ...
        """
        def decorator(fn: Callable) -> Callable:
            entry: dict[str, Any] = {
                "fn": fn,
                "schema": _build_schema(name, description, parameters or {}),
            }
            # Keep inline metadata on the entry too, so future per-tool lookups
            # (e.g. pack ownership / entitlement filtering) have a single home.
            if category is not None:
                entry["category"] = category
            if label is not None:
                entry["label"] = label
            if risk is not None:
                entry["risk"] = risk
            self._tools[name] = entry
            self._register_metadata(
                name, category=category, label=label, risk=risk,
                category_description=category_description,
            )
            return fn
        return decorator

    @staticmethod
    def _register_metadata(name: str, *, category: str | None = None,
                           label: str | None = None, risk: str | None = None,
                           category_description: str | None = None) -> None:
        """Reflect a tool's inline metadata into the central tables.

        This is what lets a tool ship its own category/label/risk instead of
        forcing an author to edit three separate dicts. TOOL_LABELS, _TOOL_RISK,
        and _TOOL_CATEGORIES stay the runtime source of truth and are simply
        also-populated here. A tool that names a new category creates its bucket;
        category_description feeds the semantic tool-selection embedding, so a
        pack should supply a real one.
        """
        if label is not None:
            TOOL_LABELS[name] = label
        if risk is not None:
            if risk not in _RISK_TIERS:
                raise ValueError(
                    f"tool {name!r}: risk must be one of {sorted(_RISK_TIERS)}, got {risk!r}"
                )
            _TOOL_RISK[name] = risk
        if category is not None:
            bucket = _TOOL_CATEGORIES.get(category)
            if bucket is None:
                _TOOL_CATEGORIES[category] = {
                    "description": category_description or category.replace("_", " "),
                    "tools": [name],
                }
            else:
                if category_description and not bucket.get("description"):
                    bucket["description"] = category_description
                if name not in bucket.setdefault("tools", []):
                    bucket["tools"].append(name)

    def get_schema(self, exclude: set[str] | None = None) -> list[dict]:
        """Return the list of tool schemas to pass to Ollama, including aliases.

        `exclude` drops those tool names (and any alias pointing at them) — used
        to hide tools a user has turned off in Settings.
        """
        self._ensure_aliases_loaded()
        tools = [t["schema"] for n, t in self._tools.items()
                 if not (exclude and n in exclude)]
        return tools + self._alias_schemas(exclude=exclude)

    def schema_for(self, names, exclude: set[str] | None = None) -> list[dict]:
        """Return schemas for just the named tools (+ their aliases).

        Used by the crew to narrow a specialist's tool schema to its slice.
        Unknown names are silently skipped — the slice is authoritative.
        `exclude` drops turned-off tools (and their aliases) from the slice.
        """
        self._ensure_aliases_loaded()
        sel = set(names)
        if exclude:
            sel -= exclude
        schemas = [t["schema"] for n, t in self._tools.items() if n in sel]
        return schemas + self._alias_schemas(for_names=sel, exclude=exclude)

    @staticmethod
    def category_tool_map() -> dict[str, list[str]]:
        """category → tool names, from _TOOL_CATEGORIES. Lets the crew derive a
        specialist's tool slice without importing the category table directly."""
        return {cat: list(info.get("tools", [])) for cat, info in _TOOL_CATEGORIES.items()}

    @staticmethod
    def rank_categories(
        query_embedding: list[float],
        category_index: dict[str, list[float]],
        top_k: int = 3,
    ) -> list[tuple[str, float]]:
        """Rank categories by cosine similarity → [(category, score)] desc, top_k.
        Feeds the crew triage's DOMAIN-SPREAD node (kai/core/crew.py)."""
        if not category_index:
            return []
        scores = sorted(
            ((cat, _cosine(query_embedding, emb)) for cat, emb in category_index.items()),
            key=lambda t: t[1],
            reverse=True,
        )
        return scores[:top_k]

    def execute(self, name: str, args: dict) -> Any:
        """Call a registered tool by name with the given arguments."""
        if name in self._tools:
            return self._tools[name]["fn"](**args)
        self._ensure_aliases_loaded()
        target = self._aliases.get(name)
        if target and target in self._tools:
            return self._tools[target]["fn"](**args)
        raise KeyError(f"Unknown tool: {name!r}")

    def resolve_name(self, name: str) -> str:
        """Return the real tool name for `name`, resolving a learned alias.

        Returns `name` unchanged if it's already a real tool or has no known
        alias. Callers use this to key enablement/risk decisions on the true
        target, so a disabled tool can't be reached under an alias.
        """
        if name in self._tools:
            return name
        self._ensure_aliases_loaded()
        return self._aliases.get(name, name)

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def describe_catalog(self) -> list[dict]:
        """Grouped tool inventory for the Settings → Tools UI.

        One group per category, each with its tools' name/label/risk. Per-user
        on/off state is layered on by the caller (it isn't the registry's job).
        Only currently-registered tools are included, so a stale category entry
        never shows a phantom tool.
        """
        registered = set(self._tools)
        groups: list[dict] = []
        for cat, info in _TOOL_CATEGORIES.items():
            tools = [
                {"name": n, "label": self.label_for(n), "risk": self.risk_for(n)}
                for n in info.get("tools", []) if n in registered
            ]
            if tools:
                groups.append({
                    "category": cat,
                    "description": info.get("description", ""),
                    "tools": sorted(tools, key=lambda t: t["name"]),
                })
        return groups

    # ── Tool metadata (labels + categories) ────────────────────────────────────

    def risk_for(self, name: str) -> str:
        """Risk tier for a tool — 'safe', 'caution', or 'destructive'.

        Resolves aliases to their target first, so a hallucinated alias inherits
        the real tool's tier. Unlisted tools are 'safe' (the read-only default).
        """
        real = name if name in self._tools else self._aliases.get(name, name)
        return _TOOL_RISK.get(real, _RISK_DEFAULT)

    def label_for(self, name: str) -> str:
        """UI status label for a tool. Falls back to a humanized name."""
        if name in TOOL_LABELS:
            return TOOL_LABELS[name]
        # e.g. "system.kill_process" → "Kill process"
        leaf = name.split(".")[-1].replace("_", " ").strip()
        return leaf.capitalize() if leaf else name

    def audit_metadata(self) -> dict[str, list[str]]:
        """Report registered tools that are missing a label or a category.

        Returns {"missing_label": [...], "uncategorized": [...]}. Both empty means
        every tool has UI + routing metadata — call this at startup (and in the
        tools test) so adding a tool without its metadata fails loudly instead of
        silently degrading.
        """
        categorized: set[str] = set()
        for cat in _TOOL_CATEGORIES.values():
            categorized.update(cat.get("tools", []))
        registered = set(self._tools)
        return {
            "missing_label": sorted(n for n in registered if n not in TOOL_LABELS),
            "uncategorized": sorted(n for n in registered if n not in categorized),
            # Risk tiers default to "safe", so missing entries aren't an error —
            # but a risk key naming a tool that no longer exists is drift worth flagging.
            "stale_risk": sorted(n for n in _TOOL_RISK if n not in registered),
        }

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
        exclude: set[str] | None = None,
    ) -> list[dict]:
        """
        Rank the 10 categories by cosine similarity, return every tool that belongs
        to the top-k categories. This guarantees related tools always arrive as a
        complete set — e.g. all system_health tools together — not scattered picks.
        Falls back to the full schema if the index is empty.
        `exclude` drops tools the user has turned off (and their aliases).
        """
        if not category_index:
            return self.get_schema(exclude=exclude)
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
            return self.get_schema(exclude=exclude)
        # search.web is always included regardless of category — it's the universal
        # fallback when a system tool returns an error code and the model needs to look it up.
        if "search.web" in self._tools:
            selected.add("search.web")
        if exclude:                       # a turned-off tool is never offered
            selected -= exclude
        if cfg.DEBUG:
            chosen = [(c, f"{s:.2f}") for c, s in scores[:top_k]]
            print(f"[tool select] categories={chosen}  tools={len(selected)}")
        schemas = [t["schema"] for name, t in self._tools.items() if name in selected]
        # Include alias schemas for selected tools so the model can call either form
        schemas += self._alias_schemas(for_names=selected, exclude=exclude)
        return schemas


# ── Tool categories ────────────────────────────────────────────────────────
# Descriptions are written in user-query language so embeddings match what people ask,
# not sysadmin jargon. Brain embeds one vector per category (a handful of calls)
# instead of one per tool. top_k=2 categories covers most queries; error escalation
# handles edge cases.
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
            "browse a directory, save text output to a file. "
            "Also move, copy, rename, delete, and organize files (proposed first, "
            "then approved before they run), and review what file changes were made."
        ),
        "tools": [
            "files.read", "files.write", "files.append", "files.edit", "files.list",
            "sandbox.copy_to_workspace", "sandbox.propose_move", "sandbox.propose_delete",
            "sandbox.propose_rename", "sandbox.approve", "sandbox.history",
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
            "Remember durable facts about me — who I am, my hardware, health, "
            "preferences, habits, decisions — in the long-term memory tree. "
            "What do you know about me, remember this, don't forget. "
            "Read full transcripts of past conversations. "
            "Search past conversation history. Self-reflection journal."
        ),
        "tools": [
            "tree.save", "tree.browse", "tree.read", "tree.find",
            "notes.save", "notes.search", "notes.list",
            "memory.get_detail", "memory.search_history", "memory.recent_sessions",
            "memory.reflect", "memory.read_reflections", "memory.sleep_notes",
        ],
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
    "goals_and_tasks": {
        "description": (
            "Track ongoing goals and multi-step tasks across sessions: start a new goal, "
            "list what I'm working on, log progress, mark a goal done, or drop one. "
            "What am I working on, remind me of my goals, I finished that."
        ),
        "tools": [
            "goals.create", "goals.list", "goals.update", "goals.complete", "goals.abandon",
        ],
    },
    "self_inspection": {
        "description": (
            "Look at my own source code, tools, and persona: read how I work internally, "
            "check my persona for gaps, draft and apply updates to my own identity, "
            "and review what changed about me recently. How do you work, update your persona."
        ),
        "tools": [
            "self.inspect", "self.list_tools", "self.check_persona",
            "self.propose_persona_update", "self.apply_persona_update",
            "self.recent_changes",
        ],
    },
    "remote_cluster": {
        "description": (
            "Check on other paired machines (the cluster): list connected nodes, "
            "see a node's status, run a diagnostic scan on one node or broadcast a scan "
            "to all of them, and fetch the results. How is my other PC, scan the server."
        ),
        "tools": [
            "cluster.list_nodes", "cluster.node_status", "cluster.node_scan",
            "cluster.broadcast_scan", "cluster.get_result",
        ],
    },
    "study_library": {
        "description": (
            "Find and study academic papers and books: search for research papers, "
            "search for books, find a free legal open-access copy, get a download link, "
            "and ask questions about what's saved in my study library."
        ),
        "tools": [
            "study.search_papers", "study.search_books", "study.find_free",
            "study.get_book_url", "study.ask_library",
        ],
    },
    "web_content": {
        "description": (
            "Open and read a specific web page or URL, take a screenshot of a page, "
            "fetch the contents of a link the user gave me, or save a page to my "
            "library to refer back to and search later. "
            "Read this page, what does this URL say, grab that article, save this for me."
        ),
        "tools": ["browser.read_page", "browser.screenshot",
                  "research.fetch_url", "research.add_to_library"],
    },
    "containers": {
        "description": (
            "Manage LXD/Incus system containers and virtual machines on this Linux box: "
            "list containers, see their status and IP, create a new container or VM, "
            "start, stop, and delete them. Spin up a VM, make a container, tear it down."
        ),
        "tools": [
            "lxc.list", "lxc.info", "lxc.create",
            "lxc.start", "lxc.stop", "lxc.delete",
        ],
    },
    "media_understanding": {
        "description": (
            "Understand images and audio: describe what's in a picture or screenshot, "
            "and transcribe spoken audio into text. What's in this image, transcribe this."
        ),
        "tools": ["vision.describe", "audio.transcribe"],
    },
}


# ── Tool status labels ──────────────────────────────────────────────────────
# Short present-tense labels shown in the web UI while a tool runs. Co-located
# with the categories above so all per-tool metadata lives in one module. Keep
# in sync with the registered tools — audit_metadata() / the tools test enforce
# that every tool has both a label and a category.
TOOL_LABELS: dict[str, str] = {
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
    "files.write":                   "Writing file",
    "files.append":                  "Appending to file",
    "files.edit":                    "Editing file",
    "workspace.git_clone":           "Cloning repository",
    "workspace.git_pull":            "Updating repository",
    "workspace.git_list_allowed":    "Listing allowed repos",
    "memory.get_detail":             "Reading full memory transcript",
    "memory.search_history":         "Searching past conversations",
    "memory.recent_sessions":        "Recalling recent sessions",
    "memory.reflect":                "Writing a reflection",
    "memory.read_reflections":       "Reading past reflections",
    "memory.sleep_notes":            "Reading sleep journal",
    "tree.save":                     "Filing a memory",
    "tree.browse":                   "Browsing memory tree",
    "tree.read":                     "Reading memory branch",
    "tree.find":                     "Searching memory tree",
    "docs.search":                   "Searching documents",
    "docs.list":                     "Listing documents",
    "self.inspect":                  "Reading my own source code",
    "self.list_tools":               "Listing my tools",
    "self.check_persona":            "Reviewing my self-knowledge",
    "self.propose_persona_update":   "Drafting persona update",
    "self.apply_persona_update":     "Updating persona",
    "docs.delete":                   "Removing document",
    "sandbox.copy_to_workspace":     "Copying file to workspace",
    "sandbox.propose_move":          "Proposing file move",
    "sandbox.propose_delete":        "Proposing file deletion",
    "sandbox.propose_rename":        "Proposing file rename",
    "sandbox.approve":               "Executing approved operation",
    "sandbox.history":               "Checking sandbox history",
    "goals.create":                  "Creating goal",
    "goals.list":                    "Loading active goals",
    "goals.update":                  "Updating goal progress",
    "goals.complete":                "Completing goal",
    "goals.abandon":                 "Abandoning goal",
    # ── Backfilled: newer tools that previously had no UI label ──
    "audio.transcribe":              "Transcribing audio",
    "browser.read_page":             "Reading web page",
    "browser.screenshot":            "Taking screenshot",
    "cluster.list_nodes":            "Listing cluster nodes",
    "cluster.node_status":           "Checking node status",
    "cluster.node_scan":             "Scanning remote node",
    "cluster.broadcast_scan":        "Scanning all nodes",
    "cluster.get_result":            "Fetching node result",
    "research.fetch_url":            "Fetching URL",
    "research.add_to_library":       "Filing page to library",
    "self.recent_changes":           "Reviewing recent changes",
    "study.search_papers":           "Searching papers",
    "study.search_books":            "Searching books",
    "study.find_free":               "Finding a free copy",
    "study.get_book_url":            "Getting book link",
    "study.ask_library":             "Asking the study library",
    "vision.describe":               "Looking at the image",
    "lxc.list":                      "Listing containers",
    "lxc.info":                      "Checking container status",
    "lxc.create":                    "Creating container",
    "lxc.start":                     "Starting container",
    "lxc.stop":                      "Stopping container",
    "lxc.delete":                    "Deleting container",
}


# ── Tool risk tiers ───────────────────────────────────────────────────────────
# How much trust a tool needs before it runs unprompted. This is the single
# source of truth for the confirm gate (brain.py derives its set from here).
#   safe        → read-only / no side effects → Kai runs it without asking.
#   caution     → makes a change that's cheap to undo → run, but announce it.
#   destructive → irreversible OR high-impact (heavy/expensive) → confirm first.
# Anything not listed defaults to "safe" — the read-only majority. Only the risky
# minority is enumerated, so this stays small and auditable.
_RISK_DEFAULT = "safe"
_RISK_TIERS = {"safe", "caution", "destructive"}  # valid inline risk values (see tool())
_TOOL_RISK: dict[str, str] = {
    # ── destructive: irreversible, or heavy enough the user should opt in ──
    "pc.deep_scan":                  "destructive",  # ~2 min full scan
    "system.clear_temp_files":       "destructive",
    "system.run_disk_cleanup":       "destructive",
    "system.create_restore_point":   "destructive",
    "system.repair_files":           "destructive",
    "system.disable_startup_program":"destructive",
    "system.kill_process":           "destructive",  # ends a running process
    "lxc.delete":                    "destructive",  # tears down an instance + storage
    "docs.delete":                   "destructive",  # removes an indexed document
    "self.apply_persona_update":     "destructive",  # rewrites Kai's OWN identity —
    # self-modification must never apply silently; gate it behind explicit user OK.
    # (Full fix: a diff-preview window — see docs/BACKLOG.md top-priority item.)
    # ── caution: real changes, but reversible ──
    "files.write":                   "caution",
    "files.edit":                    "caution",
    "files.append":                  "caution",
    "lxc.create":                    "caution",
    "lxc.start":                     "caution",
    "lxc.stop":                      "caution",
    "workspace.git_clone":           "caution",
    "workspace.git_pull":            "caution",
    "research.add_to_library":       "caution",  # writes a doc to the RAG library (reversible via docs.delete)
}


def confirm_tool_names() -> set[str]:
    """Tools that must pass the confirm gate (destructive tier).

    Reads the static risk table only — no registered tools required — so callers
    can import it at module load without worrying about registration order.
    """
    return {name for name, tier in _TOOL_RISK.items() if tier == "destructive"}


def _build_schema(name: str, description: str, parameters: dict) -> dict:
    """Build an Ollama-compatible tool schema."""
    # Build clean properties — strip the non-standard "required" key from each
    # property dict so the emitted JSON Schema is valid.
    clean_props = {}
    for k, v in parameters.items():
        prop = {pk: pv for pk, pv in v.items() if pk != "required"}
        clean_props[k] = prop
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": clean_props,
                "required": [k for k, v in parameters.items()
                             if v.get("required", False)],
            },
        },
    }


# ── Default registry (used by cli.py) ──────────────────────────────────────────

registry = ToolRegistry()
