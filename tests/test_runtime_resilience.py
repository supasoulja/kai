"""Runtime resilience: regressions from the 2026-09-10 hardening pass (packet F).

Compression keeps what earlier summaries held (T1-44), idle accounts' learning queues drain
(T1-30), and a tool model that won't fit beside the chat model isn't used (T2-30, T1-36).
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from kai.core.history import HistoryManager, is_summary


def _fill(h, n, size=300):
    for i in range(n):
        h.append("user" if i % 2 == 0 else "assistant", f"msg{i} " + "x" * size)


def test_the_previous_summary_is_compressed_again_not_dropped():
    """T1-44: each compression discarded the last summary, so the $460 budget vanished."""
    h = HistoryManager()
    _fill(h, 10)
    h.begin_compression(char_limit=1000, keep_n=4)
    h.commit_compression("the user's budget is $460; co-builder Ingrid")
    _fill(h, 8)
    prefix = h.begin_compression(char_limit=1000, keep_n=4)
    assert any(is_summary(m) and "$460" in m["content"] for m in prefix)


def test_the_summarizer_sees_the_earlier_summary_whole():
    from kai.core.brain import Brain

    b = object.__new__(Brain)
    b._history = HistoryManager()
    b._bg_pool = SimpleNamespace(submit=lambda *a, **k: None)
    b.memory = SimpleNamespace(archive_history=lambda s: None)
    prompts = []

    def chat_local(messages, **kw):
        prompts.append(messages[0]["content"])
        return {"message": {"content": "new summary"}}

    b._chat_local = chat_local
    earlier = "budget $460 " + "y" * 900  # longer than the 800 chars a message is cut to
    b._history.replace(
        [{"role": "system", "content": f"[Earlier in this conversation: {earlier}]"}]
    )
    _fill(b._history, 60)
    b._maybe_compress_history()
    assert prompts and "[earlier summary]" in prompts[0] and earlier in prompts[0]
    assert "word for word" in prompts[0]


def test_every_n_seconds_jobs_run_when_due():
    from kai.memory.scheduler import Scheduler

    s, calls = Scheduler(), []
    s.add_every(60, lambda: calls.append(1), name="drain")
    t0 = datetime(2026, 9, 11, 12, 0, 0)
    for dt in (0, 30, 61, 90, 125):
        s._run_due(t0 + timedelta(seconds=dt))
    assert len(calls) == 3


def test_daily_jobs_still_run_once_a_day():
    from kai.memory.scheduler import Scheduler

    s, calls = Scheduler(), []
    s.add_daily("09:00", lambda: calls.append(1), name="brief")
    for when in ((11, 8, 59), (11, 9, 1), (11, 17, 0), (12, 9, 0)):
        s._run_due(datetime(2026, 9, when[0], when[1], when[2]))
    assert len(calls) == 2


def test_idle_accounts_learning_queues_get_drained(monkeypatch):
    """T1-30: queued exchanges for an account that wasn't chatting never drained."""
    from kai.api import state
    from kai.memory import pending

    pending.enqueue(71, "I live in Duluth", "Noted.")
    started = []

    def brain(uid):
        return SimpleNamespace(
            user_id=uid,
            _bg_pool=SimpleNamespace(submit=lambda fn: started.append(uid)),
            _drain_pending_learning=lambda: None,
        )

    monkeypatch.setattr(state, "user_brains", {71: brain(71), 72: brain(72)})
    try:
        assert state.drain_idle_learning() == 1 and started == [71]
    finally:
        for row_id, _, _ in pending.peek(71, limit=50):
            pending.delete(row_id)


def test_a_tool_model_that_wont_fit_beside_the_chat_model_is_reported(monkeypatch):
    """T2-30 / T1-36: gemma4:e4b (8.9 GB) beside the resident 12b (7.6 GB) on a 16 GB card."""
    from kai.llm import models

    gib = 2**30
    ollama = SimpleNamespace(
        model_sizes=lambda: {"gemma4:e4b": int(8.9 * gib), "gemma4:12b": 7 * gib, "tiny:1b": gib},
        loaded_models=lambda: {"gemma4:12b": int(7.6 * gib)},
    )
    monkeypatch.setattr(models, "_vram_total_bytes", lambda: 16 * gib)
    ok, why = models.fits_beside(ollama, "gemma4:e4b", "gemma4:12b")
    assert ok is False and "won't fit next to gemma4:12b" in why
    assert models.fits_beside(ollama, "tiny:1b", "gemma4:12b") == (True, "")
    monkeypatch.setattr(models, "_vram_total_bytes", lambda: None)
    assert models.fits_beside(ollama, "gemma4:e4b", "gemma4:12b") is None


def test_rounds_use_the_chat_model_when_the_tool_model_wont_fit(monkeypatch):
    from kai.core.brain import Brain
    from kai.core.engine import TurnEngine
    from kai.llm import models

    monkeypatch.setattr(models, "fits_beside", lambda o, m, r: (False, "too big"))
    b = object.__new__(Brain)
    b.ollama = SimpleNamespace(installed_models=lambda: ["gemma4:e4b", "gemma4:12b"])
    b.model = "gemma4:12b"
    b._tool_level, b._tool_model, b._tool_model_resolved = "light", None, False
    assert TurnEngine(b)._resolve_tool_model() == (None, False)


def test_the_tool_level_list_says_when_a_level_wont_fit(monkeypatch):
    from kai.api import state
    from kai.api.routes import settings
    from kai.llm import models

    monkeypatch.setattr(
        state,
        "ollama",
        SimpleNamespace(installed_models=lambda: ["gemma4:e4b", "gemma4:12b", "gemma4:31b"]),
    )
    monkeypatch.setattr(
        models, "fits_beside", lambda o, m, r: (True, "") if m == r else (False, f"{m} won't fit")
    )
    levels = {lv["key"]: lv for lv in settings._tool_level_list()}
    assert (
        levels["light"]["fits"] is False
        and "tool rounds will use the chat model" in levels["light"]["note"]
    )
    assert levels["balanced"]["fits"] is True and levels["balanced"]["note"] == ""
