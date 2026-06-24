"""
Shared startup/shutdown steps for the two entry points.

cli.py (single-user REPL) and web.py (multi-user server) each have their own
boot flow, but several steps are identical — checking a model is installed,
running migrations + seeding, and the shutdown ritual (sleep cycle + HQ
re-embed). Those live here so a change to one can't silently drift from the
other.
"""
from __future__ import annotations

import time
from typing import Iterable

from kai.util import log


def ensure_ollama_running(ollama, timeout: float = 15.0) -> bool:
    """Make sure the Ollama server is reachable, starting it if needed.

    Used at startup, and again mid-session if a chat request finds Ollama
    unreachable — lets the server recover from an Ollama crash without a
    manual restart. Returns True once /api/tags responds, False if it
    couldn't be reached (including a missing or failed `ollama serve`).
    """
    if ollama.is_alive():
        return True

    import shutil
    import subprocess

    ollama_path = shutil.which("ollama")
    if not ollama_path:
        log.warn("Ollama is not running and 'ollama' is not on PATH.")
        return False

    log.info("Ollama is not running — starting it...")
    subprocess.Popen(
        [ollama_path, "serve"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if ollama.is_alive():
            log.ok("Ollama started.")
            return True

    log.warn(f"Ollama failed to start within {timeout:.0f}s.")
    return False


def is_model_installed(ollama, model: str) -> bool:
    """True if `model` is installed in Ollama, matching with or without the tag."""
    installed = ollama.installed_models()
    installed_full = set(installed)
    installed_base = {m.split(":")[0] for m in installed}
    return model in installed_full or model.split(":")[0] in installed_base


def run_migrations_and_seed() -> None:
    """Run system-level DB migrations and seed default procedural rules (user 0)."""
    from kai.memory import semantic as _semantic
    from kai.memory.procedural import seed_defaults
    from kai.core.sleep import promote_checkpoint_on_startup
    _semantic.migrate()
    seed_defaults()
    # If the last run crashed, turn its leftover checkpoint into a recall trail.
    promote_checkpoint_on_startup()


def run_shutdown(ollama, brains: Iterable, *, call_brain_shutdown: bool = False) -> None:
    """Run the end-of-session ritual: drain → sleep cycle → HQ shadow re-embed.

    Thin wrapper kept for the CLI/web call-sites; the real (idempotent) logic
    lives in kai.core.lifecycle so every exit path shares one implementation.
    call_brain_shutdown is accepted for backwards compatibility but ignored —
    lifecycle drains each brain (wait=True) so in-flight work always finishes.
    """
    from kai.core import lifecycle
    lifecycle.graceful_shutdown(ollama, list(brains), reason="atexit")
