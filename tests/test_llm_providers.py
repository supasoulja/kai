"""
Phase 1 tests — the OpenAI-compatible adapter (request/response/stream
normalization) and the registry-entry → client resolver. The HTTP seams are
monkeypatched, so these never touch the network.
"""
import os
import tempfile
from pathlib import Path

os.environ.setdefault("KAI_TEST_MODE", "1")

import kai.config as cfg

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
cfg.DB_PATH = Path(_tmp_db.name)

from kai.store.db import _reset_for_tests
_reset_for_tests()

import pytest

from kai.llm import keystore, resolve
from kai.llm.providers.openai import OpenAIClient
from kai.llm.ollama import OllamaClient


def _client():
    return OpenAIClient(api_key="sk-test", base_url="https://api.example.com/v1",
                        default_model="gpt-4o-mini")


# ── Request normalization (Ollama-shaped → OpenAI) ───────────────────────────

def test_tool_thread_repair():
    messages = [
        {"role": "user", "content": "check temps"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "system.temps", "arguments": {"unit": "c"}}}]},
        {"role": "tool", "content": '{"output": "70C"}'},
    ]
    out = OpenAIClient._to_openai_messages(messages)
    asst = out[1]
    assert asst["tool_calls"][0]["id"]                       # an id was synthesized
    assert asst["tool_calls"][0]["type"] == "function"
    # dict args were serialized to a JSON string for OpenAI
    assert asst["tool_calls"][0]["function"]["arguments"] == '{"unit": "c"}'
    # the tool result got the matching tool_call_id
    assert out[2]["tool_call_id"] == asst["tool_calls"][0]["id"]


# ── Response normalization (OpenAI → Ollama shape) ───────────────────────────

def test_chat_normalizes_content(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_post_json",
                        lambda p, payload: {"choices": [{"message": {"content": "hi there"}}]})
    resp = c.chat([{"role": "user", "content": "hey"}])
    assert resp["message"]["content"] == "hi there"


def test_chat_normalizes_tool_calls(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_post_json", lambda p, payload: {"choices": [{"message": {
        "content": None,
        "tool_calls": [{"id": "call_x", "type": "function",
                        "function": {"name": "weather.current",
                                     "arguments": '{"city": "NYC"}'}}],
    }}]})
    resp = c.chat([{"role": "user", "content": "weather?"}], tools=[{"type": "function"}])
    tc = resp["message"]["tool_calls"][0]
    assert tc["id"] == "call_x"
    assert tc["function"]["name"] == "weather.current"
    assert tc["function"]["arguments"] == {"city": "NYC"}    # JSON string → dict


def test_chat_handles_bad_tool_args(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_post_json", lambda p, payload: {"choices": [{"message": {
        "tool_calls": [{"id": "c1", "function": {"name": "x", "arguments": "not json"}}],
    }}]})
    resp = c.chat([{"role": "user", "content": "x"}])
    assert resp["message"]["tool_calls"][0]["function"]["arguments"] == {}


# ── Streaming ────────────────────────────────────────────────────────────────

def _sse(*chunks):
    import json
    lines = [f"data: {json.dumps(c)}" for c in chunks]
    lines.append("data: [DONE]")
    return lines


def test_stream_assembles_text(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_post_stream", lambda p, payload: iter(_sse(
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
    )))
    out = list(c.chat_stream([{"role": "user", "content": "hi"}]))
    tokens = [t for t, done, meta in out if not done]
    assert "".join(tokens) == "Hello"
    final = out[-1]
    assert final[1] is True and final[2]["content"] == "Hello"


def test_stream_assembles_tool_calls(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_post_stream", lambda p, payload: iter(_sse(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "weather.current", "arguments": '{"ci'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": 'ty": "NYC"}'}}]}}]},
    )))
    final = list(c.chat_stream([{"role": "user", "content": "weather"}]))[-1]
    assert final[1] is True
    tc = final[2]["tool_calls"][0]
    assert tc["function"]["name"] == "weather.current"
    assert tc["function"]["arguments"] == {"city": "NYC"}    # reassembled across fragments


def test_stream_surfaces_reasoning(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_post_stream", lambda p, payload: iter(_sse(
        {"choices": [{"delta": {"reasoning": "thinking..."}}]},
        {"choices": [{"delta": {"content": "answer"}}]},
    )))
    out = list(c.chat_stream([{"role": "user", "content": "q"}]))
    think = [meta["think_token"] for t, done, meta in out if meta.get("think_token")]
    assert think == ["thinking..."]


def test_installed_models(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_get_json", lambda p: {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]})
    assert c.installed_models() == ["gpt-4o", "gpt-4o-mini"]


# ── Resolver ─────────────────────────────────────────────────────────────────

@pytest.fixture
def fixed_device_key(monkeypatch):
    from kai.system import device
    monkeypatch.setattr(device, "_device_key", b"\x03" * 30)
    monkeypatch.setattr(keystore, "_cipher_cache", None)
    yield


def test_resolve_local_entry():
    c = resolve.resolve_client({"provider": "ollama", "base_url": ""}, user_id=1)
    assert isinstance(c, OllamaClient)


def test_resolve_cloud_entry_with_key(fixed_device_key):
    keystore.set_key(5, "openrouter", "sk-live", provider="openai",
                     base_url="https://openrouter.ai/api/v1")
    entry = {"provider": "openai", "base_url": "https://openrouter.ai/api/v1",
             "ollama_id": "gpt-4o-mini", "conn_id": "openrouter"}
    c = resolve.resolve_client(entry, user_id=5)
    assert isinstance(c, OpenAIClient)
    assert c.api_key == "sk-live"
    assert c.base_url == "https://openrouter.ai/api/v1"
    assert c.default_model == "gpt-4o-mini"


def test_resolve_cloud_entry_without_key_raises(fixed_device_key):
    entry = {"provider": "openai", "conn_id": "missing", "ollama_id": "gpt-4o-mini"}
    with pytest.raises(resolve.LLMKeyMissing):
        resolve.resolve_client(entry, user_id=999)
