"""User settings — response mode, generation presets, temperature, tool-model
level, and model management.

Presets and tool-level share one shape (validate against a cfg dict → apply →
persist the fact), so they go through ``_apply_choice`` (M2). Response mode is
kept bespoke: it persists a display *label* and also writes a procedural rule.
"""

import json

from fastapi import APIRouter, HTTPException, Request

import kai.config as cfg
from kai.api import state as _state
from kai.api.models import (
    AddModelRequest,
    ModeRequest,
    PresetRequest,
    PresetTempsRequest,
    RecipeRequest,
    TemperatureRequest,
    ToolLevelRequest,
    ToolToggleRequest,
)
from kai.api.state import brain_for, custom_preset_temps, reload_skills

router = APIRouter()


def _apply_choice(brain, fact_key: str, choices: dict, value: str, apply_fn, noun: str) -> dict:
    """Shared validate → apply → persist for a choice-style setting.

    Rejects a value not in ``choices`` (400), runs ``apply_fn(value)`` for the
    live effect, persists ``value`` under ``fact_key``, and returns whatever
    ``apply_fn`` resolved (temps, model ids, …) for the response body.
    """
    if value not in choices:
        raise HTTPException(status_code=400, detail=f"Invalid {noun}. Choose from: {list(choices)}")
    resolved = apply_fn(value)
    brain.memory.set_fact(fact_key, value, source="user_setting")
    return resolved


# ── Response mode ────────────────────────────────────────────────────────────

_MODE_LABELS = {
    "short": "Short answers",
    "long": "Long answers",
    "chat": "Just chatting",
    "research": "Research",
}

_MODE_RULES = {
    "short": "brief and direct. use bullets and short sentences. skip preamble and conclusions.",
    "long": "thorough and detailed. explain reasoning, give examples, cover edge cases. don't truncate.",
    "chat": "conversational and casual. no structure or bullet points needed. talk like a person.",
    "research": "comprehensive and well-structured. include context, comparisons, organize with headers where helpful.",
}


@router.get("/settings/mode")
async def get_mode(request: Request):
    memory = brain_for(request).memory
    label = memory.get_fact("response_mode") or "Short answers"
    label_to_key = {v: k for k, v in _MODE_LABELS.items()}
    mode = label_to_key.get(label, "short")
    return {"mode": mode, "label": label}


@router.post("/settings/mode")
async def set_mode(req: ModeRequest, request: Request):
    if req.mode not in _MODE_RULES:
        raise HTTPException(
            status_code=400, detail=f"Invalid mode. Choose from: {list(_MODE_RULES)}"
        )
    memory = brain_for(request).memory
    from kai.memory import procedural as _proc

    _proc.set_rule("response_length", _MODE_RULES[req.mode], user_id=memory.user_id)
    memory.set_fact("response_mode", _MODE_LABELS[req.mode], source="user_setting")
    return {"ok": True, "mode": req.mode, "label": _MODE_LABELS[req.mode]}


# ── Generation presets (think + temperature) ─────────────────────────────────


def _preset_list(memory) -> list[dict]:
    """Presets with the effective temp (user override or default) for the UI."""
    custom = custom_preset_temps(memory)
    return [
        {
            "key": key,
            "label": p["label"],
            "think": p["think"],
            "temp": custom.get(key, p["temp"]),
            "default_temp": p["temp"],
        }
        for key, p in cfg.GEN_PRESETS.items()
    ]


@router.get("/settings/preset")
async def get_preset(request: Request):
    brain = brain_for(request)
    key = brain.memory.get_fact("gen_preset") or cfg.DEFAULT_PRESET
    if key not in cfg.GEN_PRESETS:
        key = cfg.DEFAULT_PRESET
    return {
        "preset": key,
        "temperature": brain.final_temperature,
        "temp_min": cfg.TEMP_MIN,
        "temp_max": cfg.TEMP_MAX,
        "presets": _preset_list(brain.memory),
    }


@router.post("/settings/preset")
async def set_preset(req: PresetRequest, request: Request):
    brain = brain_for(request)
    resolved = _apply_choice(
        brain,
        "gen_preset",
        cfg.GEN_PRESETS,
        req.preset,
        lambda v: brain.apply_preset(v, custom_preset_temps(brain.memory)),
        noun="preset",
    )
    return {"ok": True, "preset": req.preset, **resolved}


@router.post("/settings/temperature")
async def set_temperature(req: TemperatureRequest, request: Request):
    """Per-thread temperature override (this session only — not persisted)."""
    brain = brain_for(request)
    temp = brain.set_temperature(req.temperature)
    return {"ok": True, "temperature": temp}


@router.get("/settings/preset-temps")
async def get_preset_temps(request: Request):
    brain = brain_for(request)
    return {
        "presets": _preset_list(brain.memory),
        "temp_min": cfg.TEMP_MIN,
        "temp_max": cfg.TEMP_MAX,
    }


@router.post("/settings/preset-temps")
async def set_preset_temps(req: PresetTempsRequest, request: Request):
    """Save custom per-preset temperatures (Advanced — persisted per user)."""
    brain = brain_for(request)
    cleaned = {
        k: max(cfg.TEMP_MIN, min(cfg.TEMP_MAX, float(v)))
        for k, v in req.temps.items()
        if k in cfg.GEN_PRESETS
    }
    brain.memory.set_fact("gen_preset_temps", json.dumps(cleaned), source="user_setting")
    # Re-apply the active preset so the new value takes effect immediately.
    active = brain.memory.get_fact("gen_preset") or cfg.DEFAULT_PRESET
    if active in cfg.GEN_PRESETS:
        brain.apply_preset(active, cleaned)
    return {"ok": True, "presets": _preset_list(brain.memory)}


# ── Tool-model level (which model runs tool-call rounds) ──────────────────────


def _tool_level_list() -> list[dict]:
    """Levels with availability so the UI can label models that need pulling."""
    try:
        installed = set(_state.ollama.installed_models()) if _state.ollama else set()
    except Exception:
        installed = set()
    return [
        {
            "key": key,
            "label": lv["label"],
            "model": lv["model"],
            "blurb": lv["blurb"],
            "installed": (lv["model"] is None) or (lv["model"] in installed),
        }
        for key, lv in cfg.TOOL_MODEL_LEVELS.items()
    ]


@router.get("/settings/tool-level")
async def get_tool_level(request: Request):
    brain = brain_for(request)
    key = brain.memory.get_fact("tool_level") or cfg.DEFAULT_TOOL_LEVEL
    if key not in cfg.TOOL_MODEL_LEVELS:
        key = cfg.DEFAULT_TOOL_LEVEL
    return {"level": key, "levels": _tool_level_list()}


@router.post("/settings/tool-level")
async def set_tool_level(req: ToolLevelRequest, request: Request):
    brain = brain_for(request)
    resolved = _apply_choice(
        brain,
        "tool_level",
        cfg.TOOL_MODEL_LEVELS,
        req.level,
        brain.apply_tool_level,
        noun="level",
    )
    return {"ok": True, **resolved, "levels": _tool_level_list()}


# ── Tools (per-user enable/disable) ───────────────────────────────────────────


@router.get("/settings/tools")
async def get_tools(request: Request):
    """Grouped tool inventory with each tool's on/off state for this user."""
    brain = brain_for(request)
    disabled = brain.disabled_tools
    groups = brain.tool_registry.describe_catalog()
    total = enabled = 0
    for g in groups:
        for t in g["tools"]:
            t["enabled"] = t["name"] not in disabled
            total += 1
            enabled += t["enabled"]
    return {"groups": groups, "total": total, "enabled": enabled}


@router.post("/settings/tools")
async def set_tool(req: ToolToggleRequest, request: Request):
    """Turn one tool on or off for this user. Takes effect on the next turn."""
    brain = brain_for(request)
    if req.name not in brain.tool_registry.list_tools():
        raise HTTPException(status_code=404, detail=f"Unknown tool: {req.name}")
    brain.set_tool_disabled(req.name, disabled=not req.enabled)
    return {"ok": True, "name": req.name, "enabled": req.enabled}


# ── Recipes (low-code: chain existing tools into a new skill.<name>) ───────────


@router.get("/settings/recipes")
async def get_recipes(request: Request):
    """List the user-authored recipes (SKILL.md workflows)."""
    brain_for(request)  # auth gate
    from kai.skills import recipes as _recipes

    return {"recipes": _recipes.list_recipes()}


@router.post("/settings/recipes")
async def add_recipe(req: RecipeRequest, request: Request):
    """Create (or replace) a recipe from existing tools, then make it live."""
    brain_for(request)  # auth gate
    from kai.skills import recipes as _recipes

    try:
        recipe = _recipes.create_recipe(req.name, req.description, req.triggers, req.steps)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    reload_skills()
    return {"ok": True, "recipe": recipe}


@router.delete("/settings/recipes/{name}")
async def delete_recipe(name: str, request: Request):
    """Delete a recipe and refresh the live skill set."""
    brain_for(request)  # auth gate
    from kai.skills import recipes as _recipes

    removed = _recipes.delete_recipe(name)
    reload_skills()
    return {"ok": removed}


# ── Model management ──────────────────────────────────────────────────────────


@router.get("/settings/models")
async def get_models(request: Request):
    """List all configured models + which one is active."""
    from kai.llm import models as _models

    brain = brain_for(request)
    all_models = _models.list_models()
    # Mark which one is currently active
    for m in all_models:
        m["active"] = m["ollama_id"] == brain.model
    return {"models": all_models}


@router.get("/settings/models/available")
async def get_available_models():
    """List models installed in Ollama (for the 'add model' dropdown)."""
    if not _state.ollama:
        return {"models": [], "error": "Not initialized"}
    try:
        installed = _state.ollama.installed_models()
        return {"models": installed}
    except Exception:
        return {"models": [], "error": "Could not reach Ollama"}


@router.post("/settings/models")
async def add_model(req: AddModelRequest, request: Request):
    from kai.llm import models as _models

    name = req.name.strip()
    ollama_id = req.ollama_id.strip()
    if not name or not ollama_id:
        raise HTTPException(status_code=400, detail="Name and model ID are required")
    if len(name) > 30:
        raise HTTPException(status_code=400, detail="Name must be 30 characters or fewer")
    try:
        entry = _models.add_model(name, ollama_id, req.think)
        return {"ok": True, "model": entry}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.delete("/settings/models/{name}")
async def delete_model(name: str, request: Request):
    from kai.llm import models as _models

    try:
        removed = _models.remove_model(name)
        if not removed:
            raise HTTPException(status_code=404, detail=f"Model '{name}' not found")
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/settings/models/active")
async def set_active_model(request: Request):
    """Switch the brain to a different configured model."""
    from kai.llm import models as _models

    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Model name is required")
    entry = _models.get_model(name)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Model '{name}' not found")
    brain = brain_for(request)
    # Routes local + cloud entries through the same path: cloud entries resolve
    # their client + stored key (LLMKeyMissing → 400 "connect the key first").
    from kai.llm.resolve import LLMKeyMissing

    try:
        resolved = brain.set_active_brain(entry)
    except LLMKeyMissing:
        raise HTTPException(
            status_code=400,
            detail=f"No API key stored for '{name}'. Connect this provider first.",
        ) from None
    return {"ok": True, "model": entry["ollama_id"], "think": entry.get("think", False), **resolved}
