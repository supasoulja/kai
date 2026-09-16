"""Every answer path is grounded and checked: regressions from the 2026-09-10 pass.

P1-1: on the default tool level a tool round's own prose shipped as the reply, with no
grounding and no checks. These drive a Brain with a mocked model to pin the new paths.
"""

import json
from unittest.mock import MagicMock

import pytest

import kai.config as cfg
from kai.core.brain import CONFIRM_WAITING_TEXT, Brain, _executed_results
from kai.llm.ollama import OllamaClient
from kai.memory import cerebellum as cb
from kai.memory.manager import MemoryManager


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    import kai.llm.embed as ke
    from kai.memory import pending

    def _off(*a, **k):
        raise RuntimeError("embeddings disabled in this test")

    monkeypatch.setattr(ke, "embed", _off)
    monkeypatch.setattr(ke, "embed_batch", _off)
    for row_id, *_ in pending.peek(0, limit=1000):
        pending.delete(row_id)


def _stream(*texts):
    replies = iter(texts)

    def _s(*a, **k):
        words = next(replies).split(" ")
        for i, word in enumerate(words):
            yield (word if i == 0 else " " + word), False, {}
        yield "", True, {}

    return _s


def _ollama(chat_replies, *stream_texts):
    mock = MagicMock(spec=OllamaClient)
    mock.installed_models.return_value = [cfg.CHAT_MODEL]
    mock.chat.side_effect = list(chat_replies)
    mock.chat_stream.side_effect = _stream(*stream_texts)
    return mock


def _registry(tools, output):
    reg = MagicMock()
    reg.list_tools.return_value = list(tools)
    reg.get_schema.return_value = [{"type": "function", "function": {"name": t}} for t in tools]
    reg.schema_for.return_value = []
    reg.resolve_name.side_effect = lambda n: n
    reg.risk_for.return_value = "safe"
    reg.execute.return_value = output
    return reg


def _brain(ollama, reg):
    return Brain(
        memory=MemoryManager(embed_fn=lambda t: [0.0] * 384), tool_registry=reg, ollama=ollama
    )


def _call(name, args=None):
    return {
        "message": {
            "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": args or {}}}],
        }
    }


def test_tool_round_prose_is_discarded_and_the_answer_checked():
    """P1-1 / P1-1b: invented disk numbers used to ship straight from the tool round."""
    tool_out = '{"disk_root_filesystem_only": "55.3% used (163.7/312.1 GB)"}'
    ollama = _ollama(
        [
            _call("system.info"),
            {"message": {"content": "Your disk is 91.4% full: 285.3 GB used of 312.1 GB."}},
            {"message": {"content": "- disk 55.3% used (163.7/312.1 GB)"}},
        ],
        "Your disk is 91.4% full: 285.3 GB used of 312.1 GB.",
        "Your disk is 55.3% used (163.7 of 312.1 GB).",
    )
    brain = _brain(ollama, _registry(["system.info"], tool_out))
    out = brain.run("check my system, how full is my disk")
    assert out == "Your disk is 55.3% used (163.7 of 312.1 GB)."
    assert ollama.chat_stream.call_count == 2, "the invented numbers were not caught and retried"


def test_no_tool_reply_is_streamed_with_the_presets_thinking():
    """T2-16 / T2-20: Thinking never reached the model and replies arrived in one piece."""
    reply = "SSDs store data in flash cells while hard drives use spinning platters."
    ollama = _ollama([{"message": {"content": "round prose, discarded"}}], reply)
    brain = _brain(ollama, _registry(["system.info"], ""))
    brain.apply_preset("thinking")
    tokens = [
        tok
        for tok, done, _ in brain.run_stream(
            "explain step by step how the ssd and the hdd in my computer store data differently"
        )
        if not done and tok
    ]
    assert len(tokens) > 1, "the reply arrived as one chunk"
    assert "".join(tokens) == reply
    assert ollama.chat_stream.call_args.kwargs["think"] is True


def test_zero_tool_system_claim_is_sent_back_before_it_ships():
    """T1-02: "up to date" with no tool call, while 15 updates were pending."""
    ollama = _ollama(
        [
            {
                "message": {
                    "content": "Your system is currently up to date; no pending updates were found."
                }
            },
            _call("pc.updates"),
            {"message": {"content": "prose, discarded"}},
            {"message": {"content": "- apt: 15 upgradable"}},
        ],
        "There are 15 package updates available.",
    )
    reg = _registry(["pc.updates"], "apt: 15 upgradable")
    brain = _brain(ollama, reg)
    out = brain.run("are there any system updates available for my machine?")
    reg.execute.assert_called_once()
    assert out == "There are 15 package updates available."


def test_parked_reply_that_claims_work_is_replaced():
    """T1-15: with a destructive call parked, Kai said it was executing."""
    ollama = _ollama(
        [_call("pc.deep_scan")], "I have received authorization. I am executing the scan now."
    )
    reg = _registry(["pc.deep_scan"], "must never run")
    brain = _brain(ollama, reg)
    out = brain.run("run a full deep scan of my pc")
    reg.execute.assert_not_called()
    label = brain._pending_confirm["label"]
    assert out == CONFIRM_WAITING_TEXT.format(label=label, name="pc.deep_scan")


def test_confirmed_tool_reply_is_checked_against_what_it_returned():
    """T1-43 class: the confirmed-tool path streamed its reply unchecked."""
    ollama = _ollama(
        [], "Done. The file has been deleted.", "That proposal had expired, so nothing was deleted."
    )
    reg = _registry(
        ["sandbox.approve"],
        "Proposal 'p1' not found or expired (proposals last 5 minutes). Create a new one.",
    )
    brain = _brain(ollama, reg)
    brain._pending_confirm = {
        "name": "sandbox.approve",
        "args": {"proposal_id": "p1"},
        "label": "Executing approved operation",
        "trace_id": "t0",
    }
    out = brain.run("go ahead")
    reg.execute.assert_called_once()
    assert out == "That proposal had expired, so nothing was deleted."
    assert ollama.chat_stream.call_count == 2


def test_two_invented_numbers_are_enough_to_flag():
    """P1-1b: two invented values passed when the limit was three."""
    evidence = '{"disk_root_filesystem_only": "55.3% used (163.7/312.1 GB)"}'
    assert (
        cb.check_grounded("Your root drive is 91.4% full, 285.3 GB used.", evidence).verdict
        >= cb.Verdict.FLAG
    )
    assert (
        cb.check_grounded("About 148 GB is free of 312.1 GB.", evidence).verdict < cb.Verdict.FLAG
    )


def test_recipe_steps_count_as_executed_tools():
    """T1-21(c): a recipe really deleted a doc, and the claim about it was flagged."""
    msgs = [
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "success": True,
                    "output": "[docs.delete] Document 'x' deleted successfully.",
                    "_source": "skill.qa-doc-wipe · run 2026-09-10 12:00:00 · args {}",
                }
            ),
        }
    ]
    executed = _executed_results(msgs)
    assert ("docs.delete", "Document 'x' deleted successfully.") in executed
    assert (
        cb.check_claims_backed("The QA document has been removed.", executed).verdict
        < cb.Verdict.FLAG
    )
