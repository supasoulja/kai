"""
OpenAI-compatible provider adapter.

One adapter reaches OpenAI, OpenRouter, Together, Groq, Mistral's API, LM Studio,
and any other server that speaks the OpenAI /chat/completions API — the base_url
selects which. Covers the most ground for the least code, which is why it's the
first cloud adapter.

It normalizes both directions so Brain never knows it isn't talking to Ollama:
  • request:  Ollama-shaped messages → OpenAI messages (tool_calls get ids,
              tool results get their matching tool_call_id, arg dicts → JSON);
  • response: OpenAI choice → the Ollama message shape Brain reads
              ({"message": {"content", "thinking", "tool_calls":[{"function":
              {"name","arguments": <dict>}}]}}).

Tool *schemas* need no translation — Ollama already uses OpenAI's
{"type":"function","function":{…}} format.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Generator

from kai.config import TEMPERATURE_TOOL, TEMPERATURE_FINAL
from kai.llm.client import register_provider

_DEFAULT_BASE = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-mini"


class ProviderError(RuntimeError):
    """A cloud call failed (auth, rate limit, network). Carries an HTTP status
    when there is one so the caller can decide whether to fall back to local."""
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class OpenAIClient:
    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE,
                 default_model: str = _DEFAULT_MODEL):
        self.api_key = api_key or ""
        self.base_url = (base_url or _DEFAULT_BASE).rstrip("/")
        self.default_model = default_model or _DEFAULT_MODEL

    # ── HTTP seams (monkeypatched in tests) ────────────────────────────────
    def _headers(self) -> dict:
        return {"Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"}

    def _open(self, path: str, payload: dict | None, method: str, timeout: int):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=self._headers(), method=method,
        )
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8")[:500]
            except Exception:
                pass
            raise ProviderError(f"{self.base_url}{path} → HTTP {e.code}: {body}", status=e.code)
        except Exception as e:
            raise ProviderError(f"{self.base_url}{path} unreachable: {e}")

    def _post_json(self, path: str, payload: dict) -> dict:
        with self._open(path, payload, "POST", 120) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post_stream(self, path: str, payload: dict) -> Generator[str, None, None]:
        with self._open(path, payload, "POST", 300) as r:
            for raw in r:
                yield raw.decode("utf-8") if isinstance(raw, bytes) else raw

    def _get_json(self, path: str) -> dict:
        with self._open(path, None, "GET", 15) as r:
            return json.loads(r.read().decode("utf-8"))

    # ── Message normalization ──────────────────────────────────────────────
    @staticmethod
    def _to_openai_messages(messages: list[dict]) -> list[dict]:
        """Ollama-shaped messages → OpenAI messages.

        Repairs the two incompatibilities: assistant tool_calls need ids + JSON
        string arguments, and tool results need a tool_call_id. Brain appends one
        tool result per call in order, so pairing by a FIFO of the last
        assistant's ids reconnects them.
        """
        out: list[dict] = []
        pending_ids: list[str] = []
        for m in messages:
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                tcs = []
                pending_ids = []
                for i, tc in enumerate(m["tool_calls"]):
                    fn = tc.get("function", {})
                    tcid = tc.get("id") or f"call_{i}"
                    args = fn.get("arguments", {})
                    if isinstance(args, (dict, list)):
                        args = json.dumps(args)
                    tcs.append({"id": tcid, "type": "function",
                                "function": {"name": fn.get("name", ""),
                                             "arguments": args or "{}"}})
                    pending_ids.append(tcid)
                out.append({"role": "assistant", "content": m.get("content") or "",
                            "tool_calls": tcs})
            elif role == "tool":
                tcid = pending_ids.pop(0) if pending_ids else "call_0"
                out.append({"role": "tool", "tool_call_id": tcid,
                            "content": m.get("content", "")})
            else:
                out.append({"role": role, "content": m.get("content", "")})
        return out

    @staticmethod
    def _to_ollama_message(msg: dict) -> dict:
        """OpenAI choice message → the shape Brain reads from OllamaClient."""
        out: dict = {"role": "assistant", "content": msg.get("content") or ""}
        reasoning = msg.get("reasoning") or msg.get("reasoning_content")
        if reasoning:
            out["thinking"] = reasoning
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            conv = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                raw = fn.get("arguments")
                if isinstance(raw, str):
                    try:
                        args = json.loads(raw) if raw.strip() else {}
                    except json.JSONDecodeError:
                        args = {}
                else:
                    args = raw or {}
                conv.append({"id": tc.get("id"),
                             "function": {"name": fn.get("name", ""), "arguments": args}})
            out["tool_calls"] = conv
        return out

    def _payload(self, messages, tools, model, temperature, stream) -> dict:
        p = {
            "model": model or self.default_model,
            "messages": self._to_openai_messages(messages),
            "temperature": temperature,
            "stream": stream,
        }
        if tools:
            p["tools"] = tools  # already OpenAI-shaped
        return p

    # ── Public surface (matches LLMClient / OllamaClient) ───────────────────
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             model: str = "", think: bool = False,
             temperature: float = TEMPERATURE_TOOL, keep_alive: str = "10m") -> dict:
        data = self._post_json("/chat/completions",
                               self._payload(messages, tools, model, temperature, False))
        choices = data.get("choices") or [{}]
        return {"message": self._to_ollama_message(choices[0].get("message", {}))}

    def chat_stream(self, messages: list[dict], tools: list[dict] | None = None,
                    model: str = "", think: bool = False,
                    temperature: float = TEMPERATURE_FINAL
                    ) -> Generator[tuple[str, bool, dict], None, None]:
        content_parts: list[str] = []
        tool_acc: dict[int, dict] = {}
        for line in self._post_stream("/chat/completions",
                                      self._payload(messages, tools, model, temperature, True)):
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            if delta.get("content"):
                content_parts.append(delta["content"])
                yield delta["content"], False, {}
            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
            if reasoning:
                yield "", False, {"think_token": reasoning}
            for tc in delta.get("tool_calls") or []:
                slot = tool_acc.setdefault(tc.get("index", 0),
                                           {"id": None, "name": "", "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
        final: dict = {"role": "assistant", "content": "".join(content_parts)}
        if tool_acc:
            conv = []
            for idx in sorted(tool_acc):
                s = tool_acc[idx]
                try:
                    args = json.loads(s["args"]) if s["args"].strip() else {}
                except json.JSONDecodeError:
                    args = {}
                conv.append({"id": s["id"],
                             "function": {"name": s["name"], "arguments": args}})
            final["tool_calls"] = conv
        yield "", True, final

    def installed_models(self) -> list[str]:
        """Models the connection exposes (GET /models). Best-effort: [] on error."""
        try:
            data = self._get_json("/models")
            return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
        except Exception:
            return []

    def is_alive(self) -> bool:
        try:
            self._get_json("/models")
            return True
        except Exception:
            return False


def _build(api_key: str | None = None, base_url: str | None = None,
           default_model: str | None = None, **_ignored) -> OpenAIClient:
    return OpenAIClient(api_key=api_key or "",
                        base_url=base_url or _DEFAULT_BASE,
                        default_model=default_model or _DEFAULT_MODEL)


register_provider("openai", _build)
