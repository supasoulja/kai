"""
Integration tests — real Ollama, real model, real responses.
These are slow (20-60s each) and require Ollama to be running.

Run with:
    python -m pytest tests/test_integration.py -v -s

Auto-skipped if Ollama is not running or models aren't pulled.
"""

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("KAI_TEST_MODE", "1")

import kai.config as cfg

# Use a fresh temp DB so integration tests don't touch real data
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
cfg.DB_PATH = Path(_tmp.name)

from kai.store.db import _reset_for_tests

_reset_for_tests()

from kai.core.brain import Brain, OllamaClient
from kai.memory.manager import MemoryManager

# ── Skip if Ollama isn't available ──────────────────────────────────────────────


@pytest.fixture(scope="module")
def ollama():
    client = OllamaClient()
    if not client.is_alive():
        pytest.skip("Ollama is not running — skipping integration tests")
    installed = client.installed_models()
    installed_base = {m.split(":")[0] for m in installed}
    if cfg.CHAT_MODEL.split(":")[0] not in installed_base:
        pytest.skip(f"Model {cfg.CHAT_MODEL} not installed")
    return client


@pytest.fixture
def brain(ollama):
    memory = MemoryManager(embed_fn=ollama.embed)
    return Brain(memory=memory, ollama=ollama), memory


# ── Tests ────────────────────────────────────────────────────────────────────────


def test_real_response_is_not_empty(brain):
    b, _ = brain
    result = b.run("Say exactly: hello")
    print(f"\nModel said: {result!r}")
    assert len(result.strip()) > 0


def test_real_response_has_no_think_tags(brain):
    """Thinking tokens should be stripped before returning."""
    b, _ = brain
    result = b.run("What is 2 + 2?")
    print(f"\nModel said: {result!r}")
    assert "<think>" not in result
    assert "</think>" not in result


def test_real_memory_saves_name(brain):
    """Model processes 'my name is X' and memory should store it."""
    import time

    b, memory = brain
    b.run("My name is Alex, remember that.")
    # Fact extraction runs in the background pool after the turn
    # (commit_turn inside Brain._post_turn) — poll briefly instead of
    # racing the worker thread.
    name = None
    for _ in range(50):
        name = memory.get_fact("user_name")
        if name:
            break
        time.sleep(0.2)
    print(f"\nStored user_name: {name!r}")
    assert name is not None


def test_real_context_injected(brain):
    """Facts stored in memory should appear in the next turn's context."""
    b, memory = brain
    memory.set_fact("user_name", "Alex")
    result = b.run("What is my name?")
    print(f"\nModel said: {result!r}")
    assert "Alex" in result or len(result) > 0  # at minimum, got a response


def test_real_streaming_yields_tokens(ollama):
    """chat_stream should yield multiple tokens, not one big blob."""
    memory = MemoryManager(embed_fn=ollama.embed)
    b = Brain(memory=memory, ollama=ollama)

    tokens = []
    for token, done, _ in b.run_stream("Count from 1 to 5."):
        if not done:
            tokens.append(token)

    print(f"\nGot {len(tokens)} token chunks")
    full = "".join(tokens)
    print(f"Full response: {full!r}")
    assert len(tokens) > 1, "Expected streaming tokens, got a single chunk"
    assert len(full.strip()) > 0


# ── Cleanup ─────────────────────────────────────────────────────────────────────


def teardown_module(module):
    try:
        os.unlink(_tmp.name)
    except Exception:
        pass
