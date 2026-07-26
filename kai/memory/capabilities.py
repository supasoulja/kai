"""
kai/memory/capabilities.py — "new capabilities since last session" awareness.

Tools are auto-documented in the memory tree (tool_docs.sync_tool_docs) and
injected as the [TOOLS] block every turn — persona.md is identity/voice, not a
tool catalog. This module surfaces ONLY what changed: tools present in the live
registry that the user hasn't been shown yet, grouped by namespace, with each
description pulled straight from the registry schema. Nothing is hand-authored or
model-generated, so the awareness bubble can't claim a capability that doesn't
exist or misstate one — it's grounded by construction.

Persistence: a per-user JSON snapshot of acknowledged tool names under
STATE_DIR/capabilities_<user_id>.json. The FIRST time we look (no snapshot yet)
we seed it to the full current toolset and surface nothing — a fresh install
shouldn't announce all of Kai's tools as "new". Only tools added later appear.
"""

from __future__ import annotations

import json

from kai.config import STATE_DIR


def _snapshot_path(user_id):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"capabilities_{user_id}.json"


def _live_tools() -> dict[str, dict]:
    """Map tool name -> {namespace, tool, description} for every registered tool.

    Description comes verbatim from the tool's registry schema — the same source
    tool_docs.build_node_value uses — so it can never drift from the code.
    """
    from kai.tools.registry import registry  # lazy: keep kai.memory <-> kai.tools decoupled

    out: dict[str, dict] = {}
    for name, entry in registry._tools.items():
        namespace, _, tool = name.partition(".")
        if not tool:
            continue  # defensive — every registered tool name is "namespace.tool"
        func = (entry.get("schema") or {}).get("function", {})
        desc = (func.get("description") or "").strip()
        out[name] = {"namespace": namespace, "tool": tool, "description": desc}
    return out


def _load_snapshot(user_id) -> set[str]:
    path = _snapshot_path(user_id)
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return set()  # corrupt snapshot — treat as empty; next ack rewrites it


def _save_snapshot(user_id, names) -> None:
    _snapshot_path(user_id).write_text(json.dumps(sorted(names)), encoding="utf-8")


def new_capabilities(user_id) -> list[dict]:
    """Tools added to the registry since this user last acknowledged, grouped by namespace.

    Returns a list of {"namespace": str, "tools": [{"name", "tool", "description"}]}.
    Empty when nothing is new. On the first ever call (no snapshot), seeds the
    snapshot to the current toolset and returns [] so a fresh install doesn't
    surface every tool as "new".
    """
    live = _live_tools()
    if not _snapshot_path(user_id).exists():
        _save_snapshot(user_id, live.keys())
        return []

    known = _load_snapshot(user_id)
    new_names = [n for n in live if n not in known]
    if not new_names:
        return []

    groups: dict[str, list[dict]] = {}
    for name in sorted(new_names):
        info = live[name]
        groups.setdefault(info["namespace"], []).append(
            {
                "name": name,
                "tool": info["tool"],
                "description": info["description"],
            }
        )
    return [{"namespace": ns, "tools": tools} for ns, tools in sorted(groups.items())]


def acknowledge(user_id) -> None:
    """Mark the current toolset as seen — dismisses the awareness bubble."""
    _save_snapshot(user_id, _live_tools().keys())
