"""
In-memory console mirror — lets the dashboard show the same output you'd see in
the `python app.py` terminal.

Two sources feed one ring buffer:
  * a logging.Handler attached to the "kai" / "uvicorn" loggers (captures every
    log.ok/info/warn and uvicorn's own messages), and
  * a thin tee on sys.stdout (captures bare print() calls — e.g. the
    shutdown-phase prints in kai.llm.embed.shutdown_reembed).

The buffer keeps the last N lines, each tagged with a monotonically increasing
sequence number so the dashboard can poll incrementally (fetch only lines newer
than the last seq it saw). install() is idempotent and safe to call from both
the web and desktop-app entry points.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from collections import deque

_MAX_LINES = 1000

_lock = threading.Lock()
_buf: deque[tuple[int, float, str]] = deque(maxlen=_MAX_LINES)
_seq = 0
_installed = False


def _append(text: str) -> None:
    """Split incoming text into lines and push each onto the ring buffer."""
    if not text:
        return
    global _seq
    with _lock:
        for line in text.splitlines():
            if not line:
                continue
            _seq += 1
            _buf.append((_seq, time.time(), line))


def snapshot(after_seq: int = 0, limit: int = _MAX_LINES) -> dict:
    """Return buffered lines newer than after_seq, plus the latest seq.

    The dashboard passes back the last seq it received so each poll only
    transfers new output.
    """
    with _lock:
        rows = [r for r in _buf if r[0] > after_seq][-limit:]
        last = _buf[-1][0] if _buf else after_seq
    return {
        "lines": [{"seq": s, "ts": t, "text": x} for (s, t, x) in rows],
        "last_seq": last,
    }


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _append(self.format(record))
        except Exception:
            pass


class _Tee:
    """Wrap a stream so writes go to the real stream AND the ring buffer.

    _append does no I/O (just a deque), so there's no risk of recursing back
    into the stream we're wrapping.
    """

    def __init__(self, real):
        self._real = real

    def write(self, s):
        try:
            self._real.write(s)
        except Exception:
            pass
        _append(s)
        return len(s)

    def flush(self):
        try:
            self._real.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._real, name)


def install() -> None:
    """Start mirroring console output into the ring buffer. Idempotent."""
    global _installed
    if _installed:
        return
    _installed = True

    # Bare print() goes to stdout; the log.* helpers and uvicorn route through
    # logging (to stderr), so we capture stdout via the tee and logging via the
    # handler — no line gets double-counted.
    sys.stdout = _Tee(sys.stdout)

    handler = _BufferHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    for name in ("kai", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).addHandler(handler)
