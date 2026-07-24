"""
Process lifecycle — one canonical graceful shutdown + restart, funnelled
through by every exit path (atexit, desktop quit, admin endpoint).

Why this exists: Kai's end-of-session ritual (drain background memory work →
write the welcome-back note → HQ re-embed new memories into the Qwen shadow
tables) used to be reachable only via web.py's atexit hook, which the desktop
app skips entirely (it exits with os._exit). And even when it ran, it cancelled
in-flight work instead of letting it finish. This module makes the ritual a
single idempotent function that always waits for the embedding to finish, and
adds soft/hard restart on top of it.

Ordering matters: drain FIRST (so every episodic/RAG entry exists), then the
sleep cycle, then the HQ re-embed.

Progress is exposed via get_progress() so the dashboard can show a
"saving session — finishing embeddings" overlay and the user won't force-kill
mid re-embed.
"""

from __future__ import annotations

import os
import sys
import threading

from kai.util import log

# ── State ─────────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_shutdown_started = threading.Event()  # terminal shutdown — set once, never cleared
_soft_restarting = threading.Event()  # soft restart in progress — set then cleared

_progress: dict = {
    "phase": "idle",  # idle | starting | draining | sleep-cycle | hq-reembed | closing | done
    "detail": "",
    "pct": 0,
    "active": False,  # True while a shutdown/restart is running
    "done": False,  # True once the ritual has fully completed
    "mode": "",  # "" | shutdown | soft-restart | hard-restart
}


def is_shutting_down() -> bool:
    """True while a terminal shutdown or a soft restart is in progress.

    The chat turn endpoints check this and refuse new turns with 503 so we
    don't start work we're about to abandon.
    """
    return _shutdown_started.is_set() or _soft_restarting.is_set()


def get_progress() -> dict:
    """Snapshot of the current shutdown/restart progress (for the UI overlay)."""
    with _lock:
        return dict(_progress)


def _report(phase: str, detail: str = "", pct: int | None = None) -> None:
    with _lock:
        _progress["phase"] = phase
        _progress["detail"] = detail
        _progress["active"] = True
        if pct is not None:
            _progress["pct"] = int(pct)
    log.info(f"[lifecycle] {phase}: {detail}" if detail else f"[lifecycle] {phase}")


# ── Resolution helpers ────────────────────────────────────────────────────────


def _resolve_ollama(ollama):
    if ollama is not None:
        return ollama
    from kai.api import state

    return state.ollama


def _collect_brains(brains):
    if brains is not None:
        return list(brains)
    from kai.api import state

    with state.user_brains_lock:
        return list(state.user_brains.values())


def _close_module_pools() -> None:
    """Drain the module-global thread pools that otherwise leak at exit."""
    import importlib

    for mod_name, attr in (
        ("kai.memory.context", "_retrieval_pool"),
        ("kai.memory.patterns", "_bg"),
        ("kai.memory.cerebellum", "_bg"),
    ):
        try:
            pool = getattr(importlib.import_module(mod_name), attr, None)
            if pool is not None:
                pool.shutdown(wait=True)
        except Exception:
            pass


# ── The canonical graceful shutdown ───────────────────────────────────────────


def graceful_shutdown(ollama=None, brains=None, *, reason: str = "") -> None:
    """Run the full end-of-session ritual exactly once.

    Idempotent: a second call (e.g. atexit firing after a signal already ran it)
    returns immediately. Each step is wrapped so one failure never blocks the
    rest. ollama/brains default to the live web registry (kai.api.state); the
    CLI passes its single brain explicitly.
    """
    with _lock:
        if _shutdown_started.is_set():
            return
        _shutdown_started.set()
        _progress["active"] = True
        _progress["done"] = False
        if not _progress["mode"]:
            _progress["mode"] = "shutdown"

    ollama = _resolve_ollama(ollama)
    brains = _collect_brains(brains)
    _report("starting", reason or "saving session")

    # 1. Stop the daily-job scheduler so it can't fire mid-shutdown.
    try:
        from kai.memory.scheduler import get_scheduler

        get_scheduler().stop()
    except Exception as exc:
        log.warn(f"Scheduler stop failed: {exc}")

    # 2. Drain in-flight background memory work (wait=True) — must finish before
    #    the re-embed so every entry it produced exists.
    _report("draining", "finishing in-flight memory work")
    for brain in brains:
        try:
            brain.drain()
        except Exception as exc:
            log.warn(f"Brain drain failed: {exc}")

    # 3. Sleep cycle — write each brain's welcome-back note.
    _report("sleep-cycle", "writing welcome-back note")
    for brain in brains:
        try:
            from kai.core.sleep import run_sleep_cycle

            run_sleep_cycle(ollama, brain)
        except Exception as exc:
            log.warn(f"Sleep cycle failed: {exc}")

    # 4. HQ re-embed — the embedding the user explicitly wants completed.
    _report("hq-reembed", "embedding new memories")
    try:
        from kai.llm.embed import shutdown_reembed

        shutdown_reembed(progress_cb=_report)
    except Exception as exc:
        log.warn(f"HQ re-embed failed: {exc}")

    # 5. Release the leaking module-global pools.
    _report("closing", "releasing resources")
    _close_module_pools()

    with _lock:
        _progress["phase"] = "done"
        _progress["detail"] = "session saved"
        _progress["pct"] = 100
        _progress["done"] = True
    log.ok("[lifecycle] shutdown complete — session saved")


# ── Public triggers (return immediately; work runs on a worker thread) ─────────


def request_shutdown(exit_code: int = 0) -> None:
    """Run the graceful shutdown, then terminate the process."""
    with _lock:
        _progress["mode"] = "shutdown"

    def _worker() -> None:
        try:
            graceful_shutdown(reason="admin shutdown")
        finally:
            _terminate(exit_code)

    threading.Thread(target=_worker, name="kai-shutdown", daemon=False).start()


def request_restart(mode: str = "hard") -> None:
    """Restart Kai. mode='soft' rebuilds in place; 'hard' re-execs the process."""
    if mode == "soft":
        with _lock:
            _progress["mode"] = "soft-restart"
        threading.Thread(target=_soft_restart, name="kai-soft-restart", daemon=True).start()
    else:
        with _lock:
            _progress["mode"] = "hard-restart"
        threading.Thread(target=_hard_restart, name="kai-hard-restart", daemon=False).start()


# ── Termination / relaunch ────────────────────────────────────────────────────


def _flush() -> None:
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass


def _terminate(exit_code: int = 0) -> None:
    # The ritual already committed everything to disk, so a hard exit here is
    # safe and avoids waiting on uvicorn's own connection-drain.
    _flush()
    os._exit(exit_code)


def _hard_restart() -> None:
    graceful_shutdown(reason="admin hard restart")
    _relaunch()


def _relaunch() -> None:
    """Replace this process with a fresh one (picks up code + config + models)."""
    _flush()
    entry = os.environ.get("KAI_ENTRYPOINT", "web")
    if entry == "app":
        # pywebview's GTK/WebKit main loop can't be re-exec'd in place — spawn a
        # detached fresh instance, then exit this one. The single-instance lock
        # in app.py makes the new process wait/bind cleanly once we're gone.
        import subprocess

        import kai.config as cfg

        app_py = str(cfg.ROOT_DIR / "app.py")
        try:
            subprocess.Popen(
                [sys.executable, app_py],
                cwd=str(cfg.ROOT_DIR),
                start_new_session=True,
            )
        except Exception as exc:
            log.warn(f"Relaunch spawn failed: {exc}")
        os._exit(0)
    else:
        # web / cli: re-exec in place.
        os.execv(sys.executable, [sys.executable, *sys.argv])


def _soft_restart() -> None:
    """Rebuild brains + shared indexes in place without killing the process.

    Flushes in-flight memory work and writes welcome-back notes (continuity is
    preserved), then drops the per-user Brain cache so each is recreated fresh
    on the next request. Models stay loaded in VRAM. Does NOT run the heavy HQ
    re-embed (that's an end-of-life step) and does NOT pick up Python code
    changes — use a hard restart for those.
    """
    _soft_restarting.set()
    try:
        from kai.api import state

        _report("draining", "finishing in-flight memory work")
        with state.user_brains_lock:
            brains = list(state.user_brains.values())
        for brain in brains:
            try:
                brain.drain()
            except Exception as exc:
                log.warn(f"Brain drain failed: {exc}")

        _report("sleep-cycle", "writing welcome-back note")
        for brain in brains:
            try:
                from kai.core.sleep import run_sleep_cycle

                run_sleep_cycle(state.ollama, brain)
            except Exception as exc:
                log.warn(f"Sleep cycle failed: {exc}")

        _report("rebuilding", "reloading brains and indexes")
        with state.user_brains_lock:
            state.user_brains.clear()
        _rebuild_shared_indexes()
        try:
            from kai.core import bootstrap

            bootstrap.run_migrations_and_seed()
        except Exception as exc:
            log.warn(f"Migrate/seed on soft restart failed: {exc}")

        with _lock:
            _progress.update(
                {
                    "phase": "done",
                    "detail": "restarted",
                    "pct": 100,
                    "done": True,
                    "active": False,
                    "mode": "soft-restart",
                }
            )
        log.ok("[lifecycle] soft restart complete")
    finally:
        _soft_restarting.clear()


def _rebuild_shared_indexes() -> None:
    """Rebuild the shared domain + tool indexes (mirrors web._init)."""
    from kai.api import state
    from kai.llm.embed import embed_batch as fast_embed_batch
    from kai.memory import router as _router
    from kai.tools import registry as tool_registry

    try:
        state.shared_domain_index = _router.build_domain_index(fast_embed_batch)
    except Exception:
        pass
    try:
        state.shared_tool_index = tool_registry.build_category_index(fast_embed_batch)
    except Exception:
        pass
