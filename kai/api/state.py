"""
Per-user Brain registry + shared runtime singletons.

Extracted from web.py so route modules can obtain a Brain without importing the
web entrypoint — the first step toward collapsing web.py to router registration.

Lifecycle:
  - web.py's _init() builds the shared singletons once at startup and assigns
    them here (`state.ollama`, `state.shared_tool_index`, `state.shared_domain_index`).
  - Routes call `brain_for(request)` (raises 503 until Ollama is ready) or
    `get_or_create_brain(uid)` (DB-only paths that don't need Ollama).
Access the mutable singletons through the module (`state.ollama`), never via
`from ... import ollama`, so reassignment at init time is visible everywhere.
"""
import json
import threading

from fastapi import HTTPException, Request

import kai.config as cfg
from kai.api.deps import get_user
from kai.core.brain import Brain
from kai.llm.ollama import OllamaClient
from kai.memory.manager import MemoryManager
from kai.memory.procedural import seed_defaults
from kai.tools import registry as tool_registry

# ── Shared singletons (set by web.py _init at startup) ───────────────────────
ollama: OllamaClient | None = None
shared_tool_index: dict[str, list[float]] = {}
shared_domain_index: dict[str, list[float]] = {}

# ── Per-user Brain + MemoryManager instances ─────────────────────────────────
user_brains: dict[int, Brain] = {}
user_brains_lock = threading.Lock()


def custom_preset_temps(memory) -> dict[str, float]:
    """The user's saved Advanced preset temperatures (empty if none/invalid)."""
    raw = memory.get_fact("gen_preset_temps")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {k: float(v) for k, v in data.items() if k in cfg.GEN_PRESETS}
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}


def get_or_create_brain(user_id: int) -> Brain:
    """Get (or lazily create) a per-user Brain instance. Thread-safe."""
    brain = user_brains.get(user_id)
    if brain is not None:
        return brain
    with user_brains_lock:
        # Double-check after acquiring lock
        brain = user_brains.get(user_id)
        if brain is not None:
            return brain
        from kai.llm.embed import embed as _fast_embed
        memory = MemoryManager(embed_fn=_fast_embed, user_id=user_id)
        # Copy shared indexes so we don't re-embed per user
        memory._domain_index = dict(shared_domain_index)
        seed_defaults(user_id=user_id)
        brain = Brain(
            memory=memory,
            model=cfg.CHAT_MODEL,
            ollama=ollama,
            tool_registry=tool_registry,
            think=True,
            user_id=user_id,
        )
        brain.prime_indexes(shared_tool_index, router_ready=bool(shared_domain_index))
        # Restore the user's saved generation preset (think + temperature).
        _active = memory.get_fact("gen_preset") or cfg.DEFAULT_PRESET
        if _active not in cfg.GEN_PRESETS:
            _active = cfg.DEFAULT_PRESET
        brain.apply_preset(_active, custom_preset_temps(memory))
        # Restore the user's saved tool-model level (which model runs tool rounds).
        _tl = memory.get_fact("tool_level") or cfg.DEFAULT_TOOL_LEVEL
        if _tl not in cfg.TOOL_MODEL_LEVELS:
            _tl = cfg.DEFAULT_TOOL_LEVEL
        brain.apply_tool_level(_tl)
        user_brains[user_id] = brain
        return brain


def brain_for(request: Request) -> Brain:
    """Get the Brain for the authenticated user. Raises 503 if Ollama not ready."""
    if not ollama:
        raise HTTPException(status_code=503, detail="Not initialized")
    user = get_user(request)
    uid = user["user_id"] if user else 0
    return get_or_create_brain(uid)
