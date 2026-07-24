"""
kai/memory/tool_docs.py — Auto-generated tool documentation in the memory tree.

Every registered tool gets a node at tools/<namespace>/<tool_name> describing what
it does, its call signature, parameters, return value, and one example call —
generated from the tool's registry schema + Python signature, never hand-authored,
so it can't drift from the code.

sync_tool_docs() is idempotent (upsert + stale-leaf cleanup) and runs once per user
at Brain construction. render_tool_index() reads the synced tree paths at turn time
and returns the [TOOLS] block injected into the system prompt right after [PROCEDURAL]
— a compact map so the model always knows its full toolbox, even on turns where the
tool gate only arms a subset of schemas.

Nodes are written with source="seed": excluded from count_facts() (tree.py), from
[MEMORY CONTEXT] scoring (scorer.select_for_context), and from tree.find (no embedding)
— reference scaffolding, not facts about the user.
"""

from __future__ import annotations

import inspect

from . import tree as _tree
from .tree import Node

# Per-process guard: sync runs once per user per process lifetime (write is an
# upsert, so a duplicate sync is harmless — the guard just avoids the round-trips).
_TOOL_DOCS_SYNCED: set[str] = set()

# Per-process cache of the rendered [TOOLS] block, keyed by user_id. The block is
# static once tool docs are synced (the registry doesn't change at runtime), so
# rebuilding it every turn — a full tools/ subtree read with an np.frombuffer +
# Node construction per tool — is pure waste. Invalidated by sync_tool_docs().
_INDEX_CACHE: dict[str, str] = {}

# One example value per JSON Schema type, used to synthesize a sample call.
_TYPE_PLACEHOLDERS: dict[str, str] = {
    "string": '"..."',
    "integer": "1",
    "number": "1.0",
    "boolean": "true",
    "array": "[]",
    "object": "{}",
}


# ── Node content ──────────────────────────────────────────────────────────────


def _placeholder_for(prop: dict) -> str:
    """One example value for a parameter, derived from its JSON type."""
    return _TYPE_PLACEHOLDERS.get(prop.get("type", ""), '"..."')


def _py_defaults(fn) -> dict:
    """Concrete default values from the Python signature, keyed by param name.

    Only includes params that actually have a default — used to render optional
    params as `name: type = <default>` instead of `= ...`.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {}
    return {
        name: p.default
        for name, p in sig.parameters.items()
        if p.default is not inspect.Parameter.empty
    }


def _return_type_str(fn) -> str:
    """Return-type annotation as a string. Defaults to 'string' when absent."""
    try:
        ann = inspect.signature(fn).return_annotation
    except (TypeError, ValueError):
        return "string"
    if ann is inspect.Signature.empty:
        return "string"
    # `from __future__ import annotations` makes annotations strings already;
    # plain types render via __name__.
    text = ann if isinstance(ann, str) else getattr(ann, "__name__", str(ann))
    return text.strip("'\"") or "string"


def _signature_line(
    name: str, props: dict, required: list, py_defaults: dict, return_type: str
) -> str:
    parts = []
    for pname, prop in props.items():
        ptype = prop.get("type", "any")
        if pname in required:
            parts.append(f"{pname}: {ptype}")
        elif pname in py_defaults:
            parts.append(f"{pname}: {ptype} = {py_defaults[pname]!r}")
        else:
            parts.append(f"{pname}: {ptype} = ...")
    return f"{name}({', '.join(parts)}) -> {return_type}"


def _example_call(name: str, props: dict, required: list) -> str:
    """A sample call using the required params with type-based placeholder values."""
    args = [f"{p}={_placeholder_for(props[p])}" for p in required if p in props]
    return f"{name}({', '.join(args)})"


def build_node_value(name: str, schema: dict, fn) -> str:
    """Render the doc body for one tool's tree node from its schema + signature.

    Required/optional comes from the schema's `required` list (the LLM-facing
    contract), which can diverge from Python defaults — e.g. system.kill_process
    declares no default but schema-required is empty.
    """
    func = schema["function"]
    description = (func.get("description") or "").strip()
    params = func.get("parameters", {})
    props: dict = params.get("properties", {})
    required: list = params.get("required", [])
    py_defaults = _py_defaults(fn)
    return_type = _return_type_str(fn)

    lines = [
        description,
        "",
        f"Signature: {_signature_line(name, props, required, py_defaults, return_type)}",
    ]

    if props:
        lines.append("")
        lines.append("Params:")
        for pname, prop in props.items():
            ptype = prop.get("type", "any")
            pdesc = (prop.get("description") or "").strip()
            if pname in required:
                lines.append(f"- {pname} ({ptype}, required): {pdesc}")
            elif pname in py_defaults:
                lines.append(
                    f"- {pname} ({ptype}, optional, default: {py_defaults[pname]!r}): {pdesc}"
                )
            else:
                lines.append(f"- {pname} ({ptype}, optional): {pdesc}")

    lines.append("")
    lines.append(f"Returns: {return_type}")
    lines.append("")
    lines.append(f"Example: {_example_call(name, props, required)}")

    return "\n".join(lines)


# ── Sync ────────────────────────────────────────────────────────────────────


def sync_tool_docs(user_id) -> dict:
    """Upsert a tree node for every registered tool at tools/<namespace>/<tool_name>,
    plus a folder node at tools/<namespace> per namespace in use. Deletes leaf/folder
    nodes under tools/ that no longer match a registered tool/namespace. Idempotent.

    Returns {"created", "updated", "deleted"} for logging/tests (created vs updated is
    based on pre-existence; the write itself is always an upsert).
    """
    from kai.tools.registry import registry  # lazy: keep kai.memory <-> kai.tools decoupled

    uid = str(user_id)
    created = updated = deleted = 0
    live_namespaces: set[str] = set()
    live_leaf_paths: set[str] = set()

    for name, entry in registry._tools.items():
        namespace, _, tool_name = name.partition(".")
        if not tool_name:
            continue  # defensive — every registered tool name is "namespace.tool"
        live_namespaces.add(namespace)
        leaf_path = f"tools/{namespace}/{tool_name}"
        live_leaf_paths.add(leaf_path)

        existing = _tree.read(uid, leaf_path)
        _tree.write(
            uid,
            Node(
                path=leaf_path,
                value=build_node_value(name, entry["schema"], entry["fn"]),
                confidence=1.0,
                importance=0.2,
                specificity=0.5,
                source="seed",
                decays=False,
                domain="tools",
            ),
        )
        if existing is None:
            created += 1
        else:
            updated += 1

    for namespace in live_namespaces:
        folder_path = f"tools/{namespace}"
        if _tree.read(uid, folder_path) is None:
            _tree.write(
                uid,
                Node(
                    path=folder_path,
                    value=f"(folder) {namespace}.* tools",
                    confidence=1.0,
                    importance=0.2,
                    specificity=0.0,
                    source="seed",
                    decays=False,
                    domain="tools",
                ),
            )
            created += 1

    # Stale cleanup — drop docs for tools/namespaces no longer in the registry.
    for node in _tree.subtree(uid, "tools"):
        parts = node.path.split("/")
        if len(parts) == 3 and node.path not in live_leaf_paths:
            _tree.delete(uid, node.path)
            deleted += 1
        elif len(parts) == 2 and parts[1] not in live_namespaces:
            _tree.delete(uid, node.path)
            deleted += 1

    _INDEX_CACHE.pop(uid, None)  # tree changed — drop the cached [TOOLS] block
    return {"created": created, "updated": updated, "deleted": deleted}


# ── Render ──────────────────────────────────────────────────────────────────


def render_tool_index(user_id) -> str:
    """Build the [TOOLS] block: one line per documented tool,
    `namespace.tool_name() — tree: tools/namespace/tool_name`.

    Reads tools/* from the tree (not registry directly) so the index reflects
    what's actually navigable via tree.read right now. Returns "" if nothing is
    synced yet (sync hasn't run or failed) so the block is silently absent.
    """
    uid = str(user_id)
    cached = _INDEX_CACHE.get(uid)
    if cached is not None:
        return cached

    leaves = sorted(
        (n for n in _tree.subtree(uid, "tools") if len(n.path.split("/")) == 3),
        key=lambda n: n.path,
    )
    if not leaves:
        return ""  # not synced yet — don't cache an empty block; sync fills it in

    lines = [
        "[TOOLS — your full toolbox. Read a tool's tree path with tree.read for the full contract.]"
    ]
    for node in leaves:
        _tools_lit, namespace, tool_name = node.path.split("/")
        lines.append(f"{namespace}.{tool_name}() — tree: {node.path}")
    block = "\n".join(lines)
    _INDEX_CACHE[uid] = block
    return block


def ensure_tool_docs_synced(user_id) -> None:
    """Run sync_tool_docs() once per user per process. Call from Brain.__init__.

    Failures are silent (DEBUG-prints only) — the [TOOLS] block degrades to empty,
    never blocking Brain construction.
    """
    uid = str(user_id)
    if uid in _TOOL_DOCS_SYNCED:
        return
    _TOOL_DOCS_SYNCED.add(uid)
    try:
        sync_tool_docs(uid)
    except Exception:
        import kai.config as cfg

        if getattr(cfg, "DEBUG", False):
            import traceback

            traceback.print_exc()
