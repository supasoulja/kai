"""
Phase 0 foundation tests — provider-agnostic LLM client, encrypted key store,
and the extended model registry. These cover the additive base only; the Ollama
turn path is untouched and exercised elsewhere.
"""
import os
import tempfile
from pathlib import Path

os.environ.setdefault("KAI_TEST_MODE", "1")

# Override the DB path BEFORE any kai import touches it (same pattern as
# tests/test_security.py).
import kai.config as cfg

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
cfg.DB_PATH = Path(_tmp_db.name)

from kai.store.db import _reset_for_tests
_reset_for_tests()

import pytest

from kai.llm import keystore, models, client


# ── Key store ────────────────────────────────────────────────────────────────

@pytest.fixture
def fixed_device_key(monkeypatch):
    """Pin the machine key so the keystore cipher is deterministic and the real
    device.key file is never touched."""
    from kai.system import device
    monkeypatch.setattr(device, "_device_key", b"\x01" * 30)
    monkeypatch.setattr(keystore, "_cipher_cache", None)
    yield


def test_keystore_roundtrip(fixed_device_key):
    keystore.set_key(1, "openrouter", "sk-secret-123",
                     provider="openai", base_url="https://openrouter.ai/api/v1")
    assert keystore.get_secret(1, "openrouter") == "sk-secret-123"
    assert keystore.has_key(1, "openrouter") is True

    conns = keystore.list_connections(1)
    assert len(conns) == 1
    assert conns[0]["conn_id"] == "openrouter"
    assert conns[0]["provider"] == "openai"
    assert conns[0]["base_url"] == "https://openrouter.ai/api/v1"
    # list must never leak the secret
    assert "secret" not in conns[0]

    assert keystore.delete_key(1, "openrouter") is True
    assert keystore.get_secret(1, "openrouter") is None
    assert keystore.has_key(1, "openrouter") is False


def test_keystore_is_user_scoped(fixed_device_key):
    keystore.set_key(10, "anthropic", "key-A", provider="anthropic")
    keystore.set_key(20, "anthropic", "key-B", provider="anthropic")
    assert keystore.get_secret(10, "anthropic") == "key-A"
    assert keystore.get_secret(20, "anthropic") == "key-B"
    # one user's list never shows another's connections
    assert {c["conn_id"] for c in keystore.list_connections(10)} == {"anthropic"}


def test_keystore_encrypted_at_rest(fixed_device_key):
    keystore.set_key(2, "openai", "sk-plaintext-should-not-appear", provider="openai")
    from kai.store.db import get_conn
    row = get_conn().execute(
        "SELECT secret FROM provider_keys WHERE user_id = ? AND conn_id = ?",
        (2, "openai"),
    ).fetchone()
    assert row is not None
    assert b"sk-plaintext-should-not-appear" not in row[0]  # ciphertext, not plaintext


def test_keystore_fails_closed_on_wrong_machine_key(fixed_device_key, monkeypatch):
    keystore.set_key(3, "gemini", "g-secret", provider="gemini")
    # Simulate the DB moved to a different machine: different device key + fresh cipher.
    from kai.system import device
    monkeypatch.setattr(device, "_device_key", b"\x02" * 30)
    monkeypatch.setattr(keystore, "_cipher_cache", None)
    assert keystore.get_secret(3, "gemini") is None  # missing, not a crash


# ── Model registry ───────────────────────────────────────────────────────────

@pytest.fixture
def tmp_models(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "_MODELS_PATH", tmp_path / "models.json")
    yield tmp_path


def test_builtin_has_provider_and_caps(tmp_models):
    kai = next(m for m in models.list_models() if m["name"] == "Kai")
    assert kai["provider"] == "ollama"
    assert kai["capabilities"]["local"] is True
    assert kai["capabilities"]["tools"] is True


def test_registry_backfills_legacy_entries(tmp_models):
    # A models.json written before cloud support: no provider/base_url/caps.
    (tmp_models / "models.json").write_text(
        '{"models": [{"name": "Vision", "ollama_id": "llava", "think": false, "builtin": false}]}',
        encoding="utf-8",
    )
    vision = next(m for m in models.list_models() if m["name"] == "Vision")
    assert vision["provider"] == "ollama"
    assert vision["base_url"] == ""
    assert "capabilities" in vision and vision["capabilities"]["local"] is True


def test_add_cloud_model(tmp_models):
    entry = models.add_model("GPT", "gpt-4o-mini", provider="openai",
                             base_url="https://api.openai.com/v1")
    assert entry["provider"] == "openai"
    assert entry["base_url"] == "https://api.openai.com/v1"
    assert entry["capabilities"]["local"] is False
    # persisted + reloadable
    assert any(m["name"] == "GPT" and m["provider"] == "openai" for m in models.list_models())


def test_add_model_rejects_unknown_provider(tmp_models):
    with pytest.raises(ValueError):
        models.add_model("Bad", "x", provider="not-a-provider")


def test_add_model_backward_compatible_signature(tmp_models):
    # The old 3-arg call the /settings/models route uses must still work.
    entry = models.add_model("LocalExtra", "qwen:7b", True)
    assert entry["provider"] == "ollama"
    assert entry["think"] is True


# ── Client factory ───────────────────────────────────────────────────────────

def test_ollama_is_registered_and_conforms():
    assert "ollama" in client.available_providers()
    c = client.get_client("ollama")
    assert isinstance(c, client.LLMClient)          # has chat/chat_stream/installed_models/is_alive
    for method in ("chat", "chat_stream", "installed_models", "is_alive"):
        assert callable(getattr(c, method))


def test_get_client_unknown_provider_raises():
    with pytest.raises(ValueError):
        client.get_client("nope")


def test_capabilities_defaults():
    cap = client.Capabilities(tools=True, vision=False, local=False)
    assert cap.tools is True and cap.local is False
    assert cap.streaming is True  # default
