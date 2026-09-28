"""Kokoro TTS loading: a missing model must fail fast, never deadlock on _kokoro_lock."""

import threading

import pytest

import kai.config as cfg
from kai import audio


@pytest.mark.parametrize("fn", ["synthesize", "synthesize_chunks"])
def test_missing_kokoro_files_raise_instead_of_hanging(fn, tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "AUDIO_MODELS_DIR", tmp_path)
    monkeypatch.setattr(audio, "_kokoro_ready", False)
    outcome = {}

    def call():
        try:
            result = getattr(audio, fn)("hello")
            if fn == "synthesize_chunks":
                list(result)
        except Exception as exc:
            outcome["exc"] = exc

    t = threading.Thread(target=call, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), f"{fn} deadlocked on _kokoro_lock"
    assert isinstance(outcome.get("exc"), FileNotFoundError)
    assert not audio._kokoro_lock.locked()
