"""
HistoryManager — the rolling in-session conversation history.

Extracted from Brain (which was a god-object) so the message list, its lock, the
turn counters, and the compression *choreography* live in one cohesive place.
The actual LLM summarization is NOT here — the Brain owns the model and passes a
summary string in — so this stays a pure data/concurrency component with no
config or LLM imports.

Compression race-safety (preserved from the original in-Brain logic): the old
messages are never trimmed until a summary is ready, so concurrent readers always
see a complete history during the slow summarize call. A `_compressing` flag
prevents overlapping compressions.
"""
from __future__ import annotations

import threading


class HistoryManager:
    def __init__(self) -> None:
        self._messages: list[dict] = []
        self._lock = threading.Lock()
        self._turn_order: int = 0      # message ordering for DB persistence
        self._turn_count: int = 0      # monotonic turn counter (learn-rate gating)
        self._compressing: bool = False
        self._compress_split_idx: int = 0

    # ── basic ops ────────────────────────────────────────────────────────────

    def append(self, role: str, content: str) -> None:
        with self._lock:
            self._messages.append({"role": role, "content": content})

    def extend(self, turns: list[dict]) -> None:
        """Append several turns under a single lock (atomic for readers)."""
        with self._lock:
            self._messages.extend(turns)

    def snapshot(self) -> list[dict]:
        """Thread-safe copy of the full history."""
        with self._lock:
            return list(self._messages)

    def window(self, cap: int) -> list[dict]:
        """Thread-safe copy of the last `cap` messages."""
        with self._lock:
            return list(self._messages[-cap:])

    def clear(self) -> None:
        """Drop all history and reset the turn counters."""
        with self._lock:
            self._messages.clear()
        self._turn_order = 0
        self._turn_count = 0

    def replace(self, messages: list[dict]) -> int:
        """Replace history with a saved session's messages. Returns the count."""
        with self._lock:
            self._messages = [
                {"role": m["role"], "content": m["content"]} for m in messages
            ]
        self._turn_order = len(messages)
        return len(messages)

    # ── turn counters ────────────────────────────────────────────────────────

    @property
    def turn_order(self) -> int:
        return self._turn_order

    def advance_turn_order(self, by: int = 2) -> None:
        self._turn_order += by

    @property
    def turn_count(self) -> int:
        return self._turn_count

    def bump_turn_count(self) -> int:
        """Increment and return the per-session turn count."""
        with self._lock:
            self._turn_count += 1
            return self._turn_count

    # ── compression choreography ─────────────────────────────────────────────

    def begin_compression(self, char_limit: int, keep_n: int) -> list[dict] | None:
        """Decide, under lock, whether to compress.

        Returns the (non-system) prefix to summarize and marks compression
        in-progress WITHOUT trimming — so concurrent readers still see the full
        history during the slow summarize call. Returns None when no compression
        is warranted (already compressing / under the char limit / too short).
        """
        with self._lock:
            if self._compressing:
                return None
            total_chars = sum(len(m.get("content") or "") for m in self._messages)
            if total_chars <= char_limit:
                return None
            hist_len = len(self._messages)
            if hist_len <= keep_n:
                return None
            self._compressing = True
            self._compress_split_idx = hist_len - keep_n
            return [
                m for m in self._messages[:self._compress_split_idx]
                if m.get("role") != "system"
            ]

    def commit_compression(self, summary: str) -> None:
        """Atomic swap: drop the compressed prefix, inject the summary as a system
        message, and clear the in-progress flag. No-ops (but still clears the flag)
        if history was cleared mid-compression."""
        with self._lock:
            try:
                if len(self._messages) < self._compress_split_idx:
                    return  # history cleared during compression — bail
                self._messages = self._messages[self._compress_split_idx:]
                self._messages.insert(0, {
                    "role": "system",
                    "content": f"[Earlier in this conversation: {summary}]",
                })
            finally:
                self._compressing = False

    def abort_compression(self) -> None:
        """Clear the in-progress flag without changing history (idempotent)."""
        with self._lock:
            self._compressing = False
