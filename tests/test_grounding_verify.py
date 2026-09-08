"""
Post-answer grounding verifier (Part 3): cerebellum.verify_answer + the
buffer-verify-retry wiring in Brain.run_stream.

The failure it closes (event log, 2026-07-07): a tool ran and succeeded, but the
final answer hedged/denied ("I don't have Boise weather, want me to check?")
instead of using/redoing the tool. verify_answer flags that on a tool turn; the
turn buffers the answer, silently retries once, and reveals only the fixed reply.
"""

import json
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
    # Single-tool turns now run two-phase fact extraction too
    # (_FACT_EXTRACT_THRESHOLD dropped 2 -> 1), so the non-streaming chat used by
    # _ground_evidence has to return a real payload. Left as a bare MagicMock it
    # hands a MagicMock to strip_thinking.
    mock.chat.return_value = {"message": {"content": "- the time is 09:00"}}
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


# ── The opposite failure: asserting numbers no tool returned ──────────────────


def test_check_grounded_flags_the_invented_drives():
    """The real regression. Asked to find more drives, Kai was handed folder sizes
    by files.disk_usage and answered with three device paths and six capacities,
    none of which appear anywhere in the tool output. verify_answer could not see
    this — it only catches hedging DESPITE evidence, the opposite failure."""
    from kai.memory.cerebellum import Verdict, check_grounded

    evidence = (
        "Folder sizes in /:\n 88.71 GB /usr\n 62.17 GB /home\n 6.24 GB /var\n"
        " 886 MB /boot\n 376 MB /opt\n 18 MB /etc\n 5 MB /tmp\n 1 MB /root"
    )
    answer = (
        "*   **/dev/sda1 (Root):** ~32 GB used / 50 GB total\n"
        "*   **/dev/sdb1:** ~180 GB used / 250 GB total\n"
        "*   **/dev/sdc1:** ~40 GB used / 100 GB total"
    )
    v = check_grounded(answer, evidence)
    assert v.verdict >= Verdict.FLAG
    assert "32.0" in v.reason


def test_check_grounded_allows_arithmetic_on_tool_values():
    """A derived number is not a fabricated one. The same conversation's FIRST turn
    was correct: system.info gave 163.7 used of 312.1, and '148 GB free' follows
    from it while appearing in no tool result. Flagging that would train the model
    out of doing arithmetic, so a lone untraced value must pass."""
    from kai.memory.cerebellum import Verdict, check_grounded

    evidence = '{"disk": "55.3% used (163.7/312.1 GB)"}'
    answer = "You have about 148 GB of free space remaining out of a total capacity of 312 GB."
    assert check_grounded(answer, evidence).verdict < Verdict.FLAG


def test_check_grounded_ignores_prose_numbers_and_missing_evidence():
    from kai.memory.cerebellum import Verdict, check_grounded

    ev = "CPU: 44C  Load: 3%"
    # ordinals/counts in ordinary prose must not read as fabricated data
    assert check_grounded("There are 3 things to note, and 1 is urgent.", ev).verdict < Verdict.FLAG
    # nothing to check against
    assert check_grounded("Disk is 91% full.", "").verdict == Verdict.CLEAR
    assert check_grounded("", ev).verdict == Verdict.CLEAR


def test_fact_extraction_now_covers_single_tool_turns():
    """The threshold was 2, so the strongest guard was off for exactly the turn
    that fabricated — one tool, one bad answer."""
    from kai.core.brain import _FACT_EXTRACT_THRESHOLD

    assert _FACT_EXTRACT_THRESHOLD == 1


def test_evidence_extraction_reads_successful_tool_output():
    from kai.core.brain import _evidence_from

    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "content": json.dumps({"success": True, "output": "disk 55.3% used"})},
        {"role": "tool", "content": json.dumps({"success": False, "output": "boom"})},
        {"role": "tool", "content": "not json"},
    ]
    ev = _evidence_from(msgs)
    assert "55.3" in ev
    assert "boom" not in ev, "failed tool output is not evidence"


def test_grounding_check_reads_full_tool_output_not_the_fence_excerpt():
    """The citation fence is excerpted to keep the prompt small; the CHECK must
    not be. pc.deep_scan returns ~17k chars, so verifying against only the first
    500 flagged every correctly-quoted value past that point as fabricated —
    turning the guard into a false-positive generator that rewrites correct
    answers."""
    from kai.core.brain import _evidence_from
    from kai.memory.cerebellum import Verdict, check_grounded

    big = (
        "=== SCAN ===\ncpu 22.9%\n"
        + ("filler line\n" * 60)
        + "Disk: 55.3% used (163.7/312.1 GB)\nRAM: 7.4/31.2 GB"
    )
    msgs = [{"role": "tool", "content": json.dumps({"success": True, "output": big})}]
    answer = "Disk is 55.3% used (163.7 of 312.1 GB) and RAM is 7.4 of 31.2 GB."

    assert len(_evidence_from(msgs)) == 500, "fence stays excerpted"
    assert len(_evidence_from(msgs, limit=None)) > 500, "check sees all of it"
    assert check_grounded(answer, _evidence_from(msgs, limit=None)).verdict < Verdict.FLAG
    # and the truncated view is exactly what used to produce the false alarm
    assert check_grounded(answer, _evidence_from(msgs)).verdict >= Verdict.FLAG
