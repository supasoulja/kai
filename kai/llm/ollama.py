"""
Ollama HTTP client — the request plumbing shared by every Ollama API call.

Extracted from brain.py so the HTTP wrapper lives apart from the turn-
orchestration logic in Brain.  Owns: payload building, streaming/non-streaming
chat, embeddings, and liveness/model-list probes.
"""
import json
import urllib.request
from collections.abc import Generator

import kai.config as cfg
from kai.config import (
    CHAT_MODEL, EMBED_MODEL,
    OLLAMA_BASE_URL, CONTEXT_WINDOW,
    TEMPERATURE_TOOL, TEMPERATURE_FINAL,
)


class OllamaClient:
    def __init__(self, base_url: str = OLLAMA_BASE_URL):
        self.base_url = base_url.rstrip("/")

    def _base_payload(
        self, model: str, messages: list, think: bool, tools=None,
        temperature: float = TEMPERATURE_FINAL, keep_alive: "str | int" = "10m",
    ) -> dict:
        p: dict = {
            "model": model,
            "messages": messages,
            "keep_alive": keep_alive,
            "think": think,
            "options": {
                "num_ctx": CONTEXT_WINDOW,
                "temperature": temperature,
                "repeat_penalty": 1.15,   # prevent degenerate repetition loops
                "repeat_last_n": 128,
            },
        }
        if tools:
            p["tools"] = tools
        return p

    def _post(self, path: str, payload: dict, timeout: int = 300):
        """POST JSON to the Ollama API and return the open HTTP response.
        Single home for the request plumbing every endpoint shares."""
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=timeout)

    def _post_json(self, path: str, payload: dict, timeout: int = 300) -> dict:
        """POST and parse a (non-streaming) JSON response."""
        with self._post(path, payload, timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str = CHAT_MODEL,
        think: bool = False,
        temperature: float = TEMPERATURE_TOOL,
        keep_alive: str = "10m",
    ) -> dict:
        """Non-streaming chat. Used for tool-call rounds."""
        payload = self._base_payload(model, messages, think, tools, temperature, keep_alive)
        payload["stream"] = False
        return self._post_json("/api/chat", payload)

    def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str = CHAT_MODEL,
        think: bool = False,
        temperature: float = TEMPERATURE_FINAL,
        keep_alive: "str | int" = "10m",
    ) -> Generator[tuple[str, bool, dict], None, None]:
        """
        Streaming chat. Yields (token, done, final_message).
        - token: the text chunk to print
        - done: True on the last chunk
        - final_message: full message dict (on done=True only)

        keep_alive defaults to the warm "10m" for the main chat model — only the
        user-facing model should stay resident. Secondary callers (a separate tool
        model, embeddings) pass 0 so their model unloads right after use.
        """
        payload = self._base_payload(model, messages, think, tools, temperature, keep_alive)
        payload["stream"] = True
        with self._post("/api/chat", payload) as resp:
            in_think  = False
            think_buf: list[str] = []
            think_chars = 0   # running size of this reasoning trace (loop guard)
            blank_streak = 0  # consecutive whitespace-only tokens (output loop guard)
            for raw_line in resp:
                line = raw_line.strip()
                if not line:
                    continue
                chunk = json.loads(line)
                done = chunk.get("done", False)
                msg = chunk.get("message", {})
                token = msg.get("content", "")

                if done:
                    yield "", True, msg
                    return

                # Ollama 0.6+ with think=True sends thinking in a separate
                # message.thinking field (not embedded as <think> tags in content).
                # Older builds / some models still use <think> tags in content.
                # Handle both.
                thinking_chunk = msg.get("thinking", "")
                if thinking_chunk:
                    think_buf.append(thinking_chunk)
                    think_chars += len(thinking_chunk)
                    # Stream the reasoning live so the UI can show it as it happens
                    # (and the user can tell a long think apart from a stuck loop).
                    yield "", False, {"think_token": thinking_chunk}
                    if think_chars > cfg.THINK_CHAR_CAP:
                        # Runaway reasoning loop — flush what we have and bail so the
                        # caller forces a direct, think-off answer instead of spinning.
                        yield "", False, {"think_block": "".join(think_buf).strip(),
                                          "think_runaway": True}
                        return
                    continue

                # Legacy: <think>...</think> tags inside content stream
                if "<think>" in token:
                    in_think = True
                    after = token.split("<think>", 1)[1]
                    if after:
                        think_buf.append(after)
                        think_chars += len(after)
                        yield "", False, {"think_token": after}
                    continue

                if in_think:
                    if "</think>" in token:
                        in_think = False
                        before = token.split("</think>", 1)[0]
                        if before:
                            think_buf.append(before)
                            yield "", False, {"think_token": before}
                        yield "", False, {"think_block": "".join(think_buf).strip()}
                        think_buf = []
                    else:
                        think_buf.append(token)
                        think_chars += len(token)
                        yield "", False, {"think_token": token}
                        if think_chars > cfg.THINK_CHAR_CAP:
                            yield "", False, {"think_block": "".join(think_buf).strip(),
                                              "think_runaway": True}
                            return
                    continue

                # If we accumulated thinking chunks via message.thinking and
                # are now receiving content, flush the think buffer first.
                if think_buf and not in_think:
                    yield "", False, {"think_block": "".join(think_buf).strip()}
                    think_buf = []

                if token.strip():
                    blank_streak = 0
                else:
                    blank_streak += 1
                    if blank_streak >= 30:
                        return  # degenerate output loop — model stuck on whitespace
                yield token, False, {}

    def embed(self, text: str, model: str = EMBED_MODEL,
              keep_alive: "str | int" = 0) -> list[float]:
        return self.embed_batch([text], model, keep_alive=keep_alive)[0]

    def embed_batch(self, texts: list[str], model: str = EMBED_MODEL,
                    keep_alive: "str | int" = 0) -> list[list[float]]:
        """Embed a list of strings in one HTTP call. Returns one vector per input.

        keep_alive defaults to 0 — the embed model (a *secondary* model) unloads
        immediately after use so it never holds a runner/VRAM beside the warm chat
        model. Only the user-facing chat model stays resident between turns.
        """
        result = self._post_json(
            "/api/embed",
            {"model": model, "input": texts, "keep_alive": keep_alive},
            timeout=120,
        )
        return result["embeddings"]

    def is_alive(self) -> bool:
        try:
            urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=3)
            return True
        except Exception:
            return False

    def installed_models(self) -> list[str]:
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=5) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            return [m["name"] for m in result.get("models", [])]
        except Exception:
            return []
