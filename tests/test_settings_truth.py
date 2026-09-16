"""Settings tell the truth: regressions from the 2026-09-10 hardening pass (packet D).

Settings stored as facts stay out of recall (P1-5, T2-38), a chosen model survives a restart
and is tracked by name (P1-8, T1-41, T2-41), and picking a model leaves the preset's thinking
alone (T2-40). Stub brains; the conftest temp DB for facts.
"""

from types import SimpleNamespace

from kai.config import CHAT_MODEL
from kai.core.brain import Brain
from kai.memory import context, semantic


def _facts(uid):
    semantic.set_fact("tool_level", "off", source="user_setting", user_id=uid)
    semantic.set_fact("disabled_tools", '["network.ping"]', source="user_setting", user_id=uid)
    semantic.set_fact("kai_version", "0.9.0", source="system", user_id=uid)
    semantic.set_fact("privacy_usage_patterns", "off", source="privacy_setting", user_id=uid)
    semantic.set_fact("favorite_color", "teal", source="stated", user_id=uid)


def test_settings_are_not_recalled_as_facts():
    """P1-5 / T2-38: settings were injected into the prompt as facts Kai knows."""
    _facts(61)
    block = context.build(query="", user_id=61, include_welcome_back=False)
    keys = {f.key for f in block.semantic}
    assert "favorite_color" in keys
    assert not keys & semantic.SETTING_KEYS


def test_list_facts_can_leave_settings_out():
    _facts(62)
    assert {f.key for f in semantic.list_facts(user_id=62, include_settings=False)} == {
        "favorite_color"
    }
    assert "tool_level" in {f.key for f in semantic.list_facts(user_id=62)}


def _brain(saved=None):
    b = object.__new__(Brain)
    b.ollama = object()
    b.model = CHAT_MODEL
    b._chat_client = b.ollama
    b._chat_model = CHAT_MODEL
    b._active_brain_name = "Kai"
    b._think = True
    b.user_id = 0
    b.memory = SimpleNamespace(get_fact=lambda key: saved, set_fact=lambda *a, **k: None)
    return b


def test_a_chosen_local_model_is_restored_on_restart(monkeypatch):
    """P1-8 / T1-41: only cloud entries were restored, so a local choice was silently dropped."""
    from kai.llm import models

    entry = {"name": "QA Granite", "ollama_id": "granite4.1:3b", "provider": "ollama"}
    monkeypatch.setattr(models, "get_model", lambda name: entry if name == "QA Granite" else None)
    b = _brain(saved="QA Granite")
    b._restore_active_brain()
    assert b._chat_model == "granite4.1:3b"
    assert b.active_brain_name == "QA Granite"


def test_a_removed_model_falls_back_to_the_builtin(monkeypatch):
    from kai.llm import models

    monkeypatch.setattr(models, "get_model", lambda name: None)
    b = _brain(saved="gone")
    b._restore_active_brain()
    assert b._chat_model == CHAT_MODEL and b.active_brain_name == "Kai"


def test_choosing_a_model_keeps_the_presets_thinking():
    """T2-40: clicking a model row turned thinking off while Settings still said Thinking."""
    b = _brain()
    b.set_active_brain(
        {"name": "kai_plain", "ollama_id": CHAT_MODEL, "provider": "ollama", "think": False}
    )
    assert b._think is True
    assert b.active_brain_name == "kai_plain"
