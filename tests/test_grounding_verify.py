"""
Post-answer grounding verifier (Part 3): cerebellum.verify_answer + the
buffer-verify-retry wiring in Brain.run_stream.

The failure it closes (event log, 2026-07-07): a tool ran and succeeded, but the
final answer hedged/denied ("I don't have Boise weather, want me to check?")
instead of using/redoing the tool. verify_answer flags that on a tool turn; the
turn buffers the answer, silently retries once, and reveals only the fixed reply.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("KAI_ENTRYPOINT", "test")
os.environ.setdefault("KAI_TEST_MODE", "1")

import kai.config as cfg

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
cfg.DB_PATH = Path(_tmp.name)

from kai.store.db import _reset_for_tests

_reset_for_tests()

from kai.memory.cerebellum import Verdict, verify_answer

_HEDGE = (
    "I don't have the specific weather data for Boise. The last check provided "
    "information for Denver, Colorado instead. If you want me to try again "
    "specifically for Boise, let me know."
)


# ── Detector precision ────────────────────────────────────────────────────────


def test_flags_hedge_when_tools_ran():
    assert verify_answer(_HEDGE, "weather in boise", ["weather.current"]).verdict == Verdict.FLAG


def test_flags_couldnt_complete():
    for ans in (
        "I couldn't find that file in the workspace.",
        "I don't have access to that information right now.",
        "The last search returned results for Denver, not Boise.",
    ):
        assert verify_answer(ans, "q", ["files.list"]).verdict == Verdict.FLAG, ans


def test_clear_for_grounded_answers():
    for ans in (
        "Boise, Idaho: 78F, clear skies.",
        "The disk is 82% full; /home is the biggest user.",
        "Your CPU is at 52C. Want me to check the GPU temp too?",
    ):  # follow-up offer, not a hedge
        assert verify_answer(ans, "q", ["system.temps"]).verdict == Verdict.CLEAR, ans


def test_clear_on_chat_turn_even_if_it_denies():
    # No tools ran → "I don't have that" is a legitimate answer, not a hedge.
    assert verify_answer("I don't have that information.", "q", []).verdict == Verdict.CLEAR


def test_clear_on_empty():
    assert verify_answer("", "q", ["weather.current"]).verdict == Verdict.CLEAR


# ── Wiring: hedge is buffered and silently replaced ───────────────────────────


def _multi_stream(*responses):
    """chat_stream side_effect returning a different reply on each call."""
    it = iter(responses)

    def _stream(*_a, **_k):
        yield next(it), False, {}
        yield "", True, {}

    return _stream


def _tool_turn_brain(*stream_responses):
    from kai.core.brain import Brain
    from kai.llm.ollama import OllamaClient
    from kai.memory.manager import MemoryManager

    mock = MagicMock(spec=OllamaClient)
    mock.embed.return_value = [0.0] * 2560
    mock.installed_models.return_value = [cfg.CHAT_MODEL]
    mock.chat_stream.side_effect = _multi_stream(*stream_responses)
    reg = MagicMock()
    reg.list_tools.return_value = ["time.now"]  # real list → fast-path fires
    reg.execute.return_value = "2026-07-08 09:00"
    reg.get_schema.return_value = [{"type": "function", "function": {"name": "time.now"}}]
    reg.risk_for.return_value = "safe"
    memory = MemoryManager(embed_fn=lambda t: [0.0] * 2560)
    return mock, Brain(memory=memory, tool_registry=reg, ollama=mock)


def test_hedge_on_tool_turn_is_retried_and_replaced():
    # A tool ran (time.now via fast-path); the draft answer hedges → buffered,
    # verified, retried once, and only the corrected reply reaches the user.
    mock, brain = _tool_turn_brain(
        "I couldn't get the time. Want me to try again?",  # draft (buffered)
        "It's 9:00 AM.",  # silent retry
    )
    out = brain.run("what time is it?")
    assert out == "It's 9:00 AM."  # the hedge was never revealed
    assert "Want me to try again" not in out
    assert mock.chat_stream.call_count == 2  # draft + one silent retry


def test_good_tool_answer_is_not_retried():
    mock, brain = _tool_turn_brain("It's 9:00 AM.")  # clean first draft
    out = brain.run("what time is it?")
    assert out == "It's 9:00 AM."
    assert mock.chat_stream.call_count == 1  # verified clean → no retry
