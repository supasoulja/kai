"""
Brain ↔ cloud-brain wiring: _chat / _chat_stream routing + fail→local fallback,
and set_active_brain. Built via object.__new__(Brain) so no DB / MemoryManager
is needed — these test the wrapper logic in isolation.
"""
import os
os.environ.setdefault("KAI_TEST_MODE", "1")

import pytest

from kai.core.brain import Brain
from kai.config import CHAT_MODEL


class FakeOllama:
    """Stand-in local client (Brain.ollama)."""
    def __init__(self):
        self.chat_calls = []
        self.stream_calls = []

    def chat(self, messages, tools=None, model=None, think=False, temperature=None):
        self.chat_calls.append(model)
        return {"message": {"content": f"local:{model}"}}

    def chat_stream(self, messages, tools=None, model=None, think=False, temperature=None):
        self.stream_calls.append(model)
        yield "local", False, {}
        yield "", True, {"role": "assistant", "content": "local"}


class FakeCloud:
    def __init__(self, fail=False):
        self.fail = fail

    def chat(self, messages, tools=None, model=None, think=False, temperature=None):
        if self.fail:
            raise RuntimeError("cloud boom")
        return {"message": {"content": f"cloud:{model}"}}

    def chat_stream(self, messages, tools=None, model=None, think=False, temperature=None):
        if self.fail:
            raise RuntimeError("cloud boom")
        yield "cloud", False, {}
        yield "", True, {"role": "assistant", "content": "cloud"}


def _brain():
    b = object.__new__(Brain)
    b.ollama = FakeOllama()
    b._chat_client = b.ollama
    b._chat_model = CHAT_MODEL
    b._final_temp = 0.4
    b.user_id = 0
    b.model = CHAT_MODEL
    return b


# ── _chat ────────────────────────────────────────────────────────────────────

def test_chat_uses_local_by_default():
    b = _brain()
    assert b._chat([{"role": "user", "content": "hi"}])["message"]["content"] == f"local:{CHAT_MODEL}"


def test_chat_uses_cloud_when_active():
    b = _brain()
    b._chat_client = FakeCloud()
    b._chat_model = "gpt-4o-mini"
    assert b._chat([{"role": "user", "content": "hi"}])["message"]["content"] == "cloud:gpt-4o-mini"


def test_chat_falls_back_to_local_on_cloud_failure():
    b = _brain()
    b._chat_client = FakeCloud(fail=True)
    b._chat_model = "gpt-4o-mini"
    out = b._chat([{"role": "user", "content": "hi"}])
    assert out["message"]["content"] == f"local:{CHAT_MODEL}"   # fell back
    assert b.ollama.chat_calls == [CHAT_MODEL]


def test_chat_local_failure_propagates():
    b = _brain()
    def boom(*a, **k):
        raise RuntimeError("local down")
    b.ollama.chat = boom
    with pytest.raises(RuntimeError):
        b._chat([{"role": "user", "content": "hi"}])


# ── _chat_stream ─────────────────────────────────────────────────────────────

def test_stream_local_by_default():
    b = _brain()
    toks = [t for t, done, m in b._chat_stream([{"role": "user", "content": "x"}],
                                               think=False, temperature=0.4) if not done]
    assert toks == ["local"]


def test_stream_cloud_when_active():
    b = _brain()
    b._chat_client = FakeCloud()
    b._chat_model = "claude-opus-4-8"
    toks = [t for t, done, m in b._chat_stream([{"role": "user", "content": "x"}],
                                               think=False, temperature=0.4) if not done]
    assert toks == ["cloud"]


def test_stream_falls_back_when_cloud_fails_pretoken():
    b = _brain()
    b._chat_client = FakeCloud(fail=True)   # raises before any token
    b._chat_model = "claude-opus-4-8"
    toks = [t for t, done, m in b._chat_stream([{"role": "user", "content": "x"}],
                                               think=False, temperature=0.4) if not done]
    assert toks == ["local"]                # fell back to local stream
    assert b.ollama.stream_calls == [CHAT_MODEL]


# ── set_active_brain ─────────────────────────────────────────────────────────

def test_set_active_brain_local():
    b = _brain()
    b.memory = type("M", (), {"set_fact": lambda *a, **k: None})()
    res = b.set_active_brain({"provider": "ollama", "ollama_id": "gemma4:26b", "name": "Kai"})
    assert b._chat_client is b.ollama
    assert b._chat_model == "gemma4:26b"
    assert res["provider"] == "ollama"


def test_set_active_brain_cloud(monkeypatch):
    b = _brain()
    b.memory = type("M", (), {"set_fact": lambda *a, **k: None})()
    fake = FakeCloud()
    import kai.llm.resolve as resolve
    monkeypatch.setattr(resolve, "resolve_client", lambda entry, uid: fake)
    res = b.set_active_brain(
        {"provider": "openai", "ollama_id": "gpt-4o-mini", "conn_id": "openai", "name": "GPT"})
    assert b._chat_client is fake
    assert b._chat_model == "gpt-4o-mini"
    assert res["provider"] == "openai"
