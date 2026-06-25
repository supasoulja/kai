"""
Wave 6 — HistoryManager extracted from the Brain god-object.
Run with: python -m pytest tests/test_history.py -v
"""
from kai.core.history import HistoryManager


def test_append_and_snapshot():
    h = HistoryManager()
    h.append("user", "hi")
    h.append("assistant", "hello")
    assert h.snapshot() == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_snapshot_is_a_copy():
    h = HistoryManager()
    h.append("user", "hi")
    snap = h.snapshot()
    snap.append({"role": "user", "content": "mutated"})
    assert len(h.snapshot()) == 1  # internal list untouched


def test_extend_is_atomic_multi_append():
    h = HistoryManager()
    h.extend([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
    assert len(h.snapshot()) == 2


def test_window_returns_last_n():
    h = HistoryManager()
    for i in range(5):
        h.append("user", str(i))
    assert [m["content"] for m in h.window(2)] == ["3", "4"]


def test_clear_resets_messages_and_counters():
    h = HistoryManager()
    h.append("user", "hi")
    h.bump_turn_count()
    h.advance_turn_order(2)
    h.clear()
    assert h.snapshot() == []
    assert h.turn_count == 0
    assert h.turn_order == 0


def test_replace_sets_messages_and_turn_order():
    h = HistoryManager()
    n = h.replace([{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}])
    assert n == 2
    assert h.turn_order == 2
    assert [m["content"] for m in h.snapshot()] == ["x", "y"]


def test_turn_counters():
    h = HistoryManager()
    assert h.bump_turn_count() == 1
    assert h.bump_turn_count() == 2
    assert h.turn_count == 2
    h.advance_turn_order()  # default +2
    assert h.turn_order == 2


# ── compression choreography ─────────────────────────────────────────────────

def _bulk(h, n, size=200):
    for i in range(n):
        h.append("user" if i % 2 == 0 else "assistant", "x" * size)


def test_begin_compression_returns_none_when_under_limit():
    h = HistoryManager()
    h.append("user", "short")
    assert h.begin_compression(char_limit=10_000, keep_n=4) is None


def test_begin_compression_returns_prefix_and_marks_in_progress():
    h = HistoryManager()
    _bulk(h, 10, size=300)  # 3000 chars > limit
    prefix = h.begin_compression(char_limit=1000, keep_n=4)
    assert prefix is not None and len(prefix) == 6  # 10 - keep_n(4)
    # In-progress: a second call is refused until commit/abort.
    assert h.begin_compression(char_limit=1000, keep_n=4) is None
    # History is NOT trimmed yet — readers still see all 10.
    assert len(h.snapshot()) == 10


def test_commit_compression_swaps_in_summary():
    h = HistoryManager()
    _bulk(h, 10, size=300)
    h.begin_compression(char_limit=1000, keep_n=4)
    h.commit_compression("we discussed the parser")
    snap = h.snapshot()
    assert snap[0]["role"] == "system"
    assert "we discussed the parser" in snap[0]["content"]
    assert len(snap) == 5  # 1 summary + the kept 4
    # Flag cleared — can compress again.
    _bulk(h, 8, size=300)
    assert h.begin_compression(char_limit=1000, keep_n=4) is not None


def test_abort_compression_clears_flag_without_changing_history():
    h = HistoryManager()
    _bulk(h, 10, size=300)
    h.begin_compression(char_limit=1000, keep_n=4)
    h.abort_compression()
    assert len(h.snapshot()) == 10  # unchanged
    assert h.begin_compression(char_limit=1000, keep_n=4) is not None  # not stuck


def test_commit_bails_if_history_cleared_midway():
    h = HistoryManager()
    _bulk(h, 10, size=300)
    h.begin_compression(char_limit=1000, keep_n=4)
    h.clear()  # history wiped during the "summarize" window
    h.commit_compression("summary")  # must not crash or resurrect content
    assert h.snapshot() == []
