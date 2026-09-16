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
from collections.abc import Callable
from datetime import datetime

from kai.util import log


class Scheduler:
    def __init__(self) -> None:
        self._jobs: list[dict] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_run: dict[str, object] = {}  # name → "YYYY-MM-DD" (daily) or datetime (every)

    def add_daily(self, time_str: str, fn: Callable, name: str) -> None:
        """Register a function to run once per day at HH:MM local time."""
        self._jobs.append({"time": time_str, "fn": fn, "name": name})

    def add_every(self, seconds: int, fn: Callable, name: str) -> None:
        """Register a function to run every `seconds` (the loop checks once a minute)."""
        self._jobs.append({"every": seconds, "fn": fn, "name": name})

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="kai-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run_due(self, now: datetime) -> None:
        today = now.strftime("%Y-%m-%d")
        for job in self._jobs:
            last = self._last_run.get(job["name"])
            if "every" in job:
                if isinstance(last, datetime) and (now - last).total_seconds() < job["every"]:
                    continue
                self._last_run[job["name"]] = now
            else:
                h, m = map(int, job["time"].split(":"))
                if last == today or not (now.hour > h or (now.hour == h and now.minute >= m)):
                    continue
                self._last_run[job["name"]] = today
            try:
                job["fn"]()
            except Exception as exc:
                log.warn(f"[scheduler] {job['name']} failed: {exc}")

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._run_due(datetime.now())
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
