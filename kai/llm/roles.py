"""
Role → model map (Part D / 3f) — one surface to change which model runs each role.

Defaults live in ROLE_MODELS; runtime overrides persist in var/memory/roles.json
(mirrors the models.json pattern) so a model can be swapped without code changes.
Precedence, highest first:

    crew member override  (roles.json "crew"[name])
    crew role default     (roles.json "roles"["crew"])
    ROLE_MODELS default

Today this is wired for the CREW (the genuinely new per-agent capability — see
Brain._run_specialist / _otto_decide). The voice/memory/embed roles are declared
here for completeness and the future Settings → Brains matrix, but their runtime
wiring still flows through the existing mechanisms (CHAT_MODEL, set_active_brain,
the embed configs); unifying those is Phase 4 (per-agent brains).
"""

import json

import kai.config as cfg

_ROLES_PATH = cfg.MEMORY_DIR / "roles.json"

# Role defaults (from Part D). crew = the shared local tool model (Otto + all
# specialists); voice = Kai's user-facing answer; memory = the semantic-read loop.
ROLE_MODELS: dict[str, str] = {
    "voice": cfg.CHAT_MODEL,
    "crew": cfg.TOOL_MODEL_LEVELS.get("balanced", {}).get("model") or cfg.CHAT_MODEL,
    "memory": cfg.MEMORY_MODEL,
    "embed_fast": cfg.FAST_EMBED_MODEL,
    "embed_hq": cfg.HQ_EMBED_MODEL,
}


def _load() -> dict:
    """Read roles.json → {"roles": {...}, "crew": {...}}. Empty/missing/corrupt → {}."""
    if not _ROLES_PATH.exists():
        return {}
    try:
        data = json.loads(_ROLES_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    _ROLES_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def model_for(role: str) -> str | None:
    """Effective model id for a role: roles.json override → ROLE_MODELS default."""
    override = _load().get("roles", {}).get(role)
    return override or ROLE_MODELS.get(role)


def crew_model_for(specialist: str) -> str | None:
    """Effective model for a crew member (Gus/Dewey/…/Otto): per-agent override →
    crew role default → ROLE_MODELS["crew"]."""
    data = _load()
    per_agent = data.get("crew", {}).get(specialist)
    return per_agent or data.get("roles", {}).get("crew") or ROLE_MODELS.get("crew")


def set_role_model(role: str, model_id: str) -> None:
    """Persist a role→model override to roles.json (the hot-swap)."""
    data = _load()
    data.setdefault("roles", {})[role] = model_id
    _save(data)


def set_crew_model(specialist: str, model_id: str) -> None:
    """Persist a per-crew-member model override to roles.json."""
    data = _load()
    data.setdefault("crew", {})[specialist] = model_id
    _save(data)


def clear_override(role: str) -> None:
    """Drop a role override so it falls back to the ROLE_MODELS default."""
    data = _load()
    if role in data.get("roles", {}):
        del data["roles"][role]
        _save(data)


def snapshot() -> dict:
    """Current effective model per role + any per-crew overrides — for inspection
    (the :model listing / Settings → Brains)."""
    return {
        "roles": {role: model_for(role) for role in ROLE_MODELS},
        "crew": dict(_load().get("crew", {})),
    }
