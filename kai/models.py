"""
User-configurable model registry.

Stores model definitions in models.json so users can add/remove models
without touching code. Ships with sensible defaults from config.py.
"""
import json
from pathlib import Path

import kai.config as cfg

_MODELS_PATH = cfg.MEMORY_DIR / "models.json"


def _defaults() -> list[dict]:
    """Built-in model entries (always present, can't be deleted)."""
    return [
        {
            "name": "Fast",
            "ollama_id": cfg.CHAT_MODEL,
            "think": False,
            "builtin": True,
        },
        {
            "name": "Heavy",
            "ollama_id": cfg.REASONING_MODEL,
            "think": True,
            "builtin": True,
        },
    ]


def _load() -> list[dict]:
    """Load models from disk, merging with defaults."""
    defaults = _defaults()
    builtin_ids = {m["name"] for m in defaults}

    if _MODELS_PATH.exists():
        try:
            data = json.loads(_MODELS_PATH.read_text(encoding="utf-8"))
            user_models = [m for m in data.get("models", []) if m.get("name") not in builtin_ids]
        except (json.JSONDecodeError, KeyError):
            user_models = []
    else:
        user_models = []

    return defaults + user_models


def _save(models: list[dict]) -> None:
    """Persist only user-added models (builtins are regenerated on load)."""
    user_models = [m for m in models if not m.get("builtin")]
    _MODELS_PATH.write_text(
        json.dumps({"models": user_models}, indent=2),
        encoding="utf-8",
    )


def list_models() -> list[dict]:
    """Return all configured models (builtins + user-added)."""
    return _load()


def get_model(name: str) -> dict | None:
    """Look up a model by its friendly name (case-insensitive)."""
    name_lower = name.lower()
    for m in _load():
        if m["name"].lower() == name_lower:
            return m
    return None


def add_model(name: str, ollama_id: str, think: bool = False) -> dict:
    """Add a user model. Raises ValueError on duplicate name."""
    models = _load()
    for m in models:
        if m["name"].lower() == name.lower():
            raise ValueError(f"A model named '{m['name']}' already exists")

    entry = {"name": name, "ollama_id": ollama_id, "think": think, "builtin": False}
    models.append(entry)
    _save(models)
    return entry


def remove_model(name: str) -> bool:
    """Remove a user-added model by name. Returns True if removed, False if not found.
    Raises ValueError if trying to remove a builtin."""
    models = _load()
    for m in models:
        if m["name"].lower() == name.lower():
            if m.get("builtin"):
                raise ValueError(f"'{m['name']}' is a built-in model and can't be removed")
            models.remove(m)
            _save(models)
            return True
    return False
