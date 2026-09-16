"""
User-configurable model registry.

Stores model definitions in models.json so users can add/remove models
without touching code. Ships with sensible defaults from config.py.
"""

import json

import kai.config as cfg

_MODELS_PATH = cfg.MEMORY_DIR / "models.json"

# Provider ids understood by the registry. "ollama" is the local default; the
# rest are cloud and resolved via kai/llm/client.py adapters (Phase 1+).
PROVIDERS = ("ollama", "openai", "anthropic", "gemini")

BUILTIN_MODEL_NAME = "Kai"

_VRAM_HEADROOM = 1 << 30  # context, compute buffers and the desktop need room too


def _vram_total_bytes() -> int | None:
    """VRAM of the largest GPU, from AMD sysfs or nvidia-smi. None if neither can be read."""
    import glob
    import pathlib
    import shutil
    import subprocess

    totals = []
    for path in glob.glob("/sys/class/drm/card*/device/mem_info_vram_total"):
        try:
            totals.append(int(pathlib.Path(path).read_text().strip()))
        except (OSError, ValueError):
            pass
    if not totals and shutil.which("nvidia-smi"):
        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            totals = [int(x) << 20 for x in res.stdout.split() if x.isdigit()]
        except Exception:
            pass
    return max(totals) if totals else None


def fits_beside(ollama, model: str, resident: str) -> tuple[bool, str] | None:
    """Whether `model` fits in VRAM next to the resident chat model, and if not, why.

    None when sizes or VRAM can't be read; callers then only check that it's installed. The
    Light level put an 8.9 GB model beside the 7.6 GB chat model on a 16 GB card, and every
    turn timed out after 300 s (T2-30, T1-36).
    """
    if model == resident:
        return True, ""
    total = _vram_total_bytes()
    try:
        sizes = ollama.model_sizes()
        loaded = ollama.loaded_models()
    except Exception:
        return None
    if not total or not isinstance(sizes, dict) or model not in sizes:
        return None
    resident_bytes = (loaded.get(resident) if isinstance(loaded, dict) else 0) or sizes.get(
        resident, 0
    )
    if sizes[model] + resident_bytes + _VRAM_HEADROOM <= total:
        return True, ""

    def gb(b: int) -> str:
        return f"{b / 2**30:.1f} GB"

    return False, (
        f"{model} ({gb(sizes[model])}) won't fit next to {resident} ({gb(resident_bytes)}) "
        f"in {gb(total)} of VRAM"
    )


def _default_caps(provider: str) -> dict:
    """Sensible per-provider capability defaults (overridable per model).

    Used by the per-agent assignment guardrails — e.g. don't let a tool-only
    agent be pointed at a model that can't call tools. `local` flags whether the
    brain runs on this machine (drives the privacy / 🌐 indicators).
    """
    return {
        "ollama": {"tools": True, "vision": True, "thinking": True, "local": True},
        "openai": {"tools": True, "vision": True, "thinking": False, "local": False},
        "anthropic": {"tools": True, "vision": True, "thinking": True, "local": False},
        "gemini": {"tools": True, "vision": True, "thinking": False, "local": False},
    }.get(provider, {"tools": False, "vision": False, "thinking": False, "local": False})


def _normalize(m: dict) -> dict:
    """Backfill provider/base_url/capabilities on entries written before cloud
    support existed (old models.json had only name/ollama_id/think/builtin)."""
    m.setdefault("provider", "ollama")
    m.setdefault("base_url", "")
    m.setdefault("conn_id", "")  # which keystore connection holds this model's key (cloud only)
    m.setdefault("capabilities", _default_caps(m["provider"]))
    return m


def _defaults() -> list[dict]:
    """Built-in model entries (always present, can't be deleted).

    There is a single built-in model now (cfg.CHAT_MODEL). Thinking and creativity
    are controlled by generation presets (see config.GEN_PRESETS), not by swapping
    to a separate (weaker) model — the chat model supports native thinking. Users
    can still add extra models (a dedicated vision model, or a cloud brain) via
    add_model().
    """
    return [
        _normalize(
            {
                "name": BUILTIN_MODEL_NAME,
                "ollama_id": cfg.CHAT_MODEL,
                "think": False,
                "builtin": True,
            }
        ),
    ]


def _load() -> list[dict]:
    """Load models from disk, merging with defaults."""
    defaults = _defaults()
    builtin_ids = {m["name"] for m in defaults}

    if _MODELS_PATH.exists():
        try:
            data = json.loads(_MODELS_PATH.read_text(encoding="utf-8"))
            user_models = [
                _normalize(m) for m in data.get("models", []) if m.get("name") not in builtin_ids
            ]
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


def add_model(
    name: str,
    ollama_id: str,
    think: bool = False,
    provider: str = "ollama",
    base_url: str = "",
    capabilities: dict | None = None,
    conn_id: str = "",
) -> dict:
    """Add a user model. Raises ValueError on duplicate name or unknown provider.

    For local models, `ollama_id` is the Ollama tag (e.g. "gemma4:26b"). For
    cloud models it's the provider's model id (e.g. "gpt-4o-mini") — the field
    name is kept for backward compatibility with existing models.json + the
    /settings/models routes. `base_url` lets one OpenAI-compatible adapter reach
    OpenAI, OpenRouter, Together, a local server, etc.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider '{provider}'. Choose from: {list(PROVIDERS)}")
    models = _load()
    for m in models:
        if m["name"].lower() == name.lower():
            raise ValueError(f"A model named '{m['name']}' already exists")

    entry = _normalize(
        {
            "name": name,
            "ollama_id": ollama_id,
            "think": think,
            "builtin": False,
            "provider": provider,
            "base_url": base_url,
            "conn_id": conn_id or (provider if provider != "ollama" else ""),
            "capabilities": capabilities or _default_caps(provider),
        }
    )
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
