"""Tests for the role→model map (kai/llm/roles.py, Part D / 3f)."""
import os
os.environ.setdefault("KAI_ENTRYPOINT", "test")

import kai.config as cfg
from kai.llm import roles


def test_model_for_default(monkeypatch):
    monkeypatch.setattr(roles, "_load", lambda: {})
    assert roles.model_for("crew") == roles.ROLE_MODELS["crew"]
    assert roles.model_for("voice") == cfg.CHAT_MODEL
    assert roles.model_for("nonexistent") is None


def test_model_for_override(monkeypatch):
    monkeypatch.setattr(roles, "_load", lambda: {"roles": {"crew": "qwen3.5:9b"}})
    assert roles.model_for("crew") == "qwen3.5:9b"
    # untouched roles still fall back to defaults
    assert roles.model_for("voice") == cfg.CHAT_MODEL


def test_crew_model_for_precedence(monkeypatch):
    monkeypatch.setattr(roles, "_load", lambda: {
        "roles": {"crew": "granite4.1:3b"},
        "crew": {"Otto": "qwen3.5:9b"},
    })
    assert roles.crew_model_for("Otto") == "qwen3.5:9b"   # per-agent override wins
    assert roles.crew_model_for("Gus") == "granite4.1:3b"  # crew role default
    monkeypatch.setattr(roles, "_load", lambda: {})
    assert roles.crew_model_for("Gus") == roles.ROLE_MODELS["crew"]  # hard default


def test_set_get_clear_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(roles, "_ROLES_PATH", tmp_path / "roles.json")
    roles.set_role_model("crew", "qwen3.5:9b")
    assert roles.model_for("crew") == "qwen3.5:9b"
    roles.set_crew_model("Gus", "granite4.1:3b")
    assert roles.crew_model_for("Gus") == "granite4.1:3b"
    # the role-level crew override is untouched by the per-agent one
    assert roles.crew_model_for("Dewey") == "qwen3.5:9b"
    roles.clear_override("crew")
    assert roles.model_for("crew") == roles.ROLE_MODELS["crew"]


def test_corrupt_roles_json_falls_back(tmp_path, monkeypatch):
    p = tmp_path / "roles.json"
    p.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setattr(roles, "_ROLES_PATH", p)
    assert roles.model_for("crew") == roles.ROLE_MODELS["crew"]  # no crash


def test_snapshot(monkeypatch):
    monkeypatch.setattr(roles, "_load", lambda: {"crew": {"Otto": "x"}})
    snap = roles.snapshot()
    assert set(snap["roles"]) == set(roles.ROLE_MODELS)
    assert snap["crew"] == {"Otto": "x"}
