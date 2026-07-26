"""
Unit tests for kai.core.lifecycle — the canonical graceful shutdown.

Fast — no Ollama, no model loading. The sleep cycle, HQ re-embed, and module
pool teardown are stubbed so the test exercises ordering and idempotency only.

Run with:
    python -m pytest tests/test_lifecycle.py -v

Covers:
  - graceful_shutdown drains each brain (wait=True), never hard-cancels
  - shutdown order: drain → sleep cycle → HQ re-embed
  - idempotency: a second call is a no-op
  - is_shutting_down() flips while the ritual runs
"""

import os

os.environ.setdefault("KAI_TEST_MODE", "1")

import pytest

from kai.core import lifecycle


class FakeBrain:
    def __init__(self, log):
        self._log = log
        self.drained = False
        self.hard_shutdown = False

    def drain(self):
        self.drained = True
        self._log.append("drain")

    def shutdown(self):
        self.hard_shutdown = True
        self._log.append("shutdown")


@pytest.fixture(autouse=True)
def _reset_lifecycle(monkeypatch):
    """Reset module state and stub the heavy/side-effecting steps."""
    lifecycle._shutdown_started.clear()
    lifecycle._soft_restarting.clear()
    lifecycle._progress.update(
        {
            "phase": "idle",
            "detail": "",
            "pct": 0,
            "active": False,
            "done": False,
            "mode": "",
        }
    )
    # Stub the steps that would touch Ollama / global pools.
    import kai.core.sleep as sleep_mod
    import kai.llm.embed as embed_mod

    monkeypatch.setattr(sleep_mod, "run_sleep_cycle", lambda ollama, brain: None)
    monkeypatch.setattr(embed_mod, "shutdown_reembed", lambda progress_cb=None: None)
    monkeypatch.setattr(lifecycle, "_close_module_pools", lambda: None)
    yield
    lifecycle._shutdown_started.clear()


def test_graceful_shutdown_drains_not_cancels():
    log = []
    brain = FakeBrain(log)
    assert not lifecycle.is_shutting_down()

    lifecycle.graceful_shutdown(ollama=object(), brains=[brain], reason="test")

    assert brain.drained is True
    assert brain.hard_shutdown is False  # drain (wait=True), never cancel
    assert lifecycle.is_shutting_down() is True
    prog = lifecycle.get_progress()
    assert prog["done"] is True
    assert prog["phase"] == "done"


def test_shutdown_runs_drain_before_reembed(monkeypatch):
    order = []
    brain = FakeBrain(order)
    import kai.core.sleep as sleep_mod
    import kai.llm.embed as embed_mod

    monkeypatch.setattr(sleep_mod, "run_sleep_cycle", lambda ollama, b: order.append("sleep"))
    monkeypatch.setattr(
        embed_mod, "shutdown_reembed", lambda progress_cb=None: order.append("reembed")
    )

    lifecycle.graceful_shutdown(ollama=object(), brains=[brain], reason="test")

    assert order == ["drain", "sleep", "reembed"]


def test_graceful_shutdown_is_idempotent():
    log = []
    b1 = FakeBrain(log)
    lifecycle.graceful_shutdown(ollama=object(), brains=[b1], reason="first")

    b2 = FakeBrain(log)
    lifecycle.graceful_shutdown(ollama=object(), brains=[b2], reason="second")

    assert b1.drained is True
    assert b2.drained is False  # second call short-circuits
