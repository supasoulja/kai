"""
Phase 2 tests — Brain logic with mocked Ollama.
No real Ollama connection needed.
Run with: python -m pytest tests/test_brain.py -v
"""
import os
import json
import tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("KAI_TEST_MODE", "1")

import kai.config as cfg
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
cfg.DB_PATH = Path(_tmp.name)

from kai.db import _reset_for_tests
_reset_for_tests()

from kai.brain import Brain, OllamaClient, _strip_thinking
from kai.memory.manager import MemoryManager


# ── Helpers ─────────────────────────────────────────────────────────────────────

def make_mock_stream(response_text: str):
    """Return a side_effect callable that yields streaming tokens like chat_stream."""
    def _stream(*args, **kwargs):
        yield response_text, False, {}
        yield "", True, {}
    return _stream


def make_mock_ollama(response_text: str = "Hello from Kai.", tool_calls: list | None = None):
    """Build a mock OllamaClient that returns a preset response."""
    mock = MagicMock(spec=OllamaClient)
    mock.is_alive.return_value = True
    mock.installed_models.return_value = [cfg.CHAT_MODEL, cfg.EMBED_MODEL]
    mock.embed.return_value = [0.0] * 2560  # match episodic_vec schema dimensions

    msg = {"content": response_text}
    if tool_calls:
        msg["tool_calls"] = tool_calls

    mock.chat.return_value = {"message": msg}
    # chat_stream is used for the final streamed answer (no-tools path)
    mock.chat_stream.side_effect = make_mock_stream(response_text)
    return mock


def make_brain(response_text: str = "Hello.", tool_calls=None):
    memory = MemoryManager(embed_fn=lambda t: [0.0] * 2560)
    ollama = make_mock_ollama(response_text, tool_calls)
    return Brain(memory=memory, ollama=ollama), memory


# ── _strip_thinking ──────────────────────────────────────────────────────────────

def test_strip_thinking_extracts_think_tags():
    thinking, clean = _strip_thinking("<think>internal reasoning</think>The answer is 42.")
    assert thinking == "internal reasoning"
    assert clean == "The answer is 42."


def test_strip_thinking_no_tags():
    thinking, clean = _strip_thinking("Just a plain response.")
    assert thinking == ""
    assert clean == "Just a plain response."


def test_strip_thinking_multiline():
    text = "<think>\nline one\nline two\n</think>\nFinal answer."
    thinking, clean = _strip_thinking(text)
    assert "line one" in thinking
    assert clean == "Final answer."


# ── Basic conversation ──────────────────────────────────────────────────────────

def test_brain_returns_response():
    brain, _ = make_brain("Hey, what's up?")
    result = brain.run("Hello Kai")
    assert result == "Hey, what's up?"


def test_brain_commits_to_memory():
    import time
    brain, memory = make_brain("Got it.")
    brain.run("My name is Alex")
    # commit_turn runs in a daemon thread — give it time to finish
    time.sleep(0.5)
    episodes = memory.recent_episodes(limit=1)
    assert len(episodes) == 1
    assert "Alex" in episodes[0].content


def test_brain_extracts_facts_from_user_input():
    brain, memory = make_brain("Noted.")
    brain.run("My name is Alex")
    assert memory.get_fact("user_name") == "Alex"


def test_brain_context_injected_into_system_prompt():
    brain, memory = make_brain("Sure.")
    memory.set_fact("user_name", "Alex")
    brain.run("What do you know about me?")
    # No tools → chat_stream is called directly (chat is not called)
    call_args = brain.ollama.chat_stream.call_args
    messages = call_args[0][0]  # first positional arg
    system_msg = messages[0]
    assert system_msg["role"] == "system"
    assert "Alex" in system_msg["content"]


# ── Tool call flow ──────────────────────────────────────────────────────────────

def test_brain_executes_tool_and_finalizes():
    """Round 1: chat() returns tool_calls → tool executed. Round 2: chat() returns final answer."""
    tool_call_response = {
        "message": {
            "content": "",
            "tool_calls": [{
                "function": {"name": "time.now", "arguments": {}}
            }]
        }
    }
    # Round 2: no tool_calls → early-exit with this content
    final_response = {"message": {"content": "It is Tuesday."}}

    mock_ollama = MagicMock(spec=OllamaClient)
    mock_ollama.embed.return_value = [0.0] * 2560
    mock_ollama.chat.side_effect = [tool_call_response, final_response]

    mock_registry = MagicMock()
    mock_registry.get_schema.return_value = [{"type": "function", "function": {"name": "time.now"}}]
    mock_registry.execute.return_value = "2026-02-20 14:00"

    memory = MemoryManager(embed_fn=lambda t: [0.0] * 2560)
    brain = Brain(memory=memory, tool_registry=mock_registry, ollama=mock_ollama)

    result = brain.run("What time is it?")
    assert result == "It is Tuesday."
    mock_registry.execute.assert_called_once_with("time.now", {})


def test_brain_handles_no_tool_registry():
    """Without a registry, no tools are offered; brain streams a normal response."""
    mock_ollama = MagicMock(spec=OllamaClient)
    mock_ollama.embed.return_value = [0.0] * 2560
    mock_ollama.chat_stream.side_effect = make_mock_stream("I can't save notes without a tool.")

    memory = MemoryManager(embed_fn=lambda t: [0.0] * 2560)
    brain = Brain(memory=memory, tool_registry=None, ollama=mock_ollama)

    result = brain.run("Save a note")
    assert result == "I can't save notes without a tool."
    # chat() should never be called — tool rounds are skipped entirely
    mock_ollama.chat.assert_not_called()


def test_brain_thinking_stripped_from_response():
    brain, _ = make_brain("<think>let me think about this...</think>Here's my answer.")
    result = brain.run("Question")
    assert "<think>" not in result
    assert result == "Here's my answer."


# ── OllamaClient ───────────────────────────────────────────────────────────────

def test_ollama_client_is_alive_false_when_down():
    client = OllamaClient(base_url="http://localhost:9999")  # nothing here
    assert client.is_alive() is False


# ── Cleanup ─────────────────────────────────────────────────────────────────────

def teardown_module(module):
    try:
        os.unlink(_tmp.name)
    except Exception:
        pass
