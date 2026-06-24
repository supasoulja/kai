"""
Kai's internal task scheduler.

A lightweight background thread that checks every 60 seconds whether any
registered daily jobs are due. Jobs fire once per calendar day at their
configured HH:MM time.

Usage:
    from kai.memory.scheduler import get_scheduler
    sched = get_scheduler()
    sched.add_daily("09:00", my_job, name="morning-briefing")
    sched.start()  # call once at server startup

Stopping: sched.stop()  — sets event and lets the thread exit cleanly
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable


class Scheduler:
    def __init__(self) -> None:
        self._jobs: list[dict] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_run: dict[str, str] = {}  # name → "YYYY-MM-DD"

    def add_daily(self, time_str: str, fn: Callable, name: str) -> None:
        """Register a function to run once per day at HH:MM local time."""
        self._jobs.append({"time": time_str, "fn": fn, "name": name})

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="kai-scheduler"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            for job in self._jobs:
                if self._last_run.get(job["name"]) == today:
                    continue
                h, m = map(int, job["time"].split(":"))
                if now.hour > h or (now.hour == h and now.minute >= m):
                    try:
                        job["fn"]()
                    except Exception as exc:
                        print(f"[scheduler] {job['name']} failed: {exc}")
                    self._last_run[job["name"]] = today
            self._stop.wait(60)  # check once per minute


_instance: Scheduler | None = None
_lock = threading.Lock()


def get_scheduler() -> Scheduler:
    """Return the process-wide scheduler singleton."""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = Scheduler()
    return _instance
