"""
Anthropic (Claude) provider adapter.

Speaks Claude's native Messages API (POST /v1/messages) and normalizes both
directions into the Ollama message shape Brain consumes — so Brain never knows
it isn't talking to Ollama. Implemented over stdlib urllib (like OllamaClient
and the OpenAI adapter) to keep this privacy-first app dependency-light and the
three cloud adapters uniform; the wire format itself follows Anthropic's spec.

Format differences this adapter bridges (vs the Ollama/OpenAI shape):
  • system prompt is a TOP-LEVEL `system` field, not a message → hoisted out;
  • tools use {name, description, input_schema} (no function wrapper);
  • tool calls are `tool_use` content blocks; tool results are `tool_result`
    content blocks inside a user message (matched back by tool_use_id);
  • `max_tokens` is required;
  • thinking is adaptive only on current models (budget_tokens 400s) and
    `temperature` is rejected on Opus 4.7/4.8 → we send adaptive thinking and
    never forward temperature.

Default model is claude-opus-4-8 (current most-capable Opus) when the registry
entry doesn't pin one.
"""

from __future__ import annotations

import json
from collections.abc import Generator

from kai.llm.client import register_provider
from kai.llm.providers._http import BaseHTTPProvider, ProviderError  # noqa: F401  (re-exported)

_DEFAULT_BASE = "https://api.anthropic.com"
_DEFAULT_MODEL = "claude-opus-4-8"
_API_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 8192


class AnthropicClient(BaseHTTPProvider):
    def __init__(
        self,
        api_key: str,
        base_url: str = _DEFAULT_BASE,
        default_model: str = _DEFAULT_MODEL,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ):
        self.api_key = api_key or ""
        self.base_url = (base_url or _DEFAULT_BASE).rstrip("/")
        self.default_model = default_model or _DEFAULT_MODEL
        self.max_tokens = max_tokens

    # ── HTTP seams (transport in BaseHTTPProvider; only auth headers differ) ─
    def _headers(self) -> dict:
        return {
            "content-type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": _API_VERSION,
        }

    # ── Request normalization (Ollama-shaped → Anthropic) ──────────────────
    @staticmethod
    def _tool_to_anthropic(t: dict) -> dict:
        fn = t.get("function", t)
        return {
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        }

    @staticmethod
    def _coerce_args(args) -> dict:
        if isinstance(args, str):
            try:
                return json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                return {}
        return args or {}

    def _build(self, messages: list[dict], tools, model, think, stream) -> dict:
        system_parts: list[str] = []
        conv: list[dict] = []
        pending_ids: list[str] = []
        for m in messages:
            role = m.get("role")
            content = m.get("content")
            if role == "system":
                if content:
                    system_parts.append(str(content))
                continue
            if role == "assistant":
                blocks = []
                if content:
                    blocks.append({"type": "text", "text": content})
                pending_ids = []
                for i, tc in enumerate(m.get("tool_calls") or []):
                    fn = tc.get("function", {})
                    tcid = tc.get("id") or f"toolu_{i}"
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tcid,
                            "name": fn.get("name", ""),
                            "input": self._coerce_args(fn.get("arguments")),
                        }
                    )
                    pending_ids.append(tcid)
                conv.append(
                    {"role": "assistant", "content": blocks or [{"type": "text", "text": " "}]}
                )
            elif role == "tool":
                tcid = pending_ids.pop(0) if pending_ids else "toolu_0"
                conv.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": tcid, "content": content or ""}
                        ],
                    }
                )
            else:  # user
                conv.append({"role": "user", "content": content or " "})

        payload: dict = {
            "model": model or self.default_model,
            "max_tokens": self.max_tokens,
            "messages": conv,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if tools:
            payload["tools"] = [self._tool_to_anthropic(t) for t in tools]
        if think:
            # Adaptive is the only supported on-mode for current models;
            # "summarized" so reasoning can be surfaced to the UI.
            payload["thinking"] = {"type": "adaptive", "display": "summarized"}
        if stream:
            payload["stream"] = True
        # temperature is intentionally never forwarded (400s on Opus 4.7/4.8).
        return payload

    # ── Response normalization (Anthropic → Ollama shape) ──────────────────
    @staticmethod
    def _to_ollama_message(data: dict) -> dict:
        text_parts, think_parts, tool_calls = [], [], []
        for block in data.get("content", []) or []:
            bt = block.get("type")
            if bt == "text":
                text_parts.append(block.get("text", ""))
            elif bt == "thinking":
                think_parts.append(block.get("thinking", ""))
            elif bt == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id"),
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": block.get("input", {}),
                        },
                    }
                )
        msg: dict = {"role": "assistant", "content": "".join(text_parts)}
        if think_parts:
            msg["thinking"] = "".join(think_parts)
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return msg

    # ── Public surface (matches LLMClient / OllamaClient) ───────────────────
    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str = "",
        think: bool = False,
        temperature: float = 0.0,
        keep_alive: str = "10m",
    ) -> dict:
        data = self._post_json("/v1/messages", self._build(messages, tools, model, think, False))
        return {"message": self._to_ollama_message(data)}

    def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str = "",
        think: bool = False,
        temperature: float = 0.0,
    ) -> Generator[tuple[str, bool, dict], None, None]:
        text_parts: list[str] = []
        tool_acc: dict[int, dict] = {}
        for line in self._post_stream(
            "/v1/messages", self._build(messages, tools, model, think, True)
        ):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if not data:
                continue
            try:
                evt = json.loads(data)
            except json.JSONDecodeError:
                continue
            et = evt.get("type")
            if et == "content_block_start":
                cb = evt.get("content_block", {})
                if cb.get("type") == "tool_use":
                    tool_acc[evt.get("index", 0)] = {
                        "id": cb.get("id"),
                        "name": cb.get("name", ""),
                        "json": "",
                    }
            elif et == "content_block_delta":
                delta = evt.get("delta", {})
                dt = delta.get("type")
                if dt == "text_delta":
                    txt = delta.get("text", "")
                    text_parts.append(txt)
                    yield txt, False, {}
                elif dt == "thinking_delta":
                    yield "", False, {"think_token": delta.get("thinking", "")}
                elif dt == "input_json_delta":
                    slot = tool_acc.get(evt.get("index", 0))
                    if slot is not None:
                        slot["json"] += delta.get("partial_json", "")
            elif et == "message_stop":
                break
        final: dict = {"role": "assistant", "content": "".join(text_parts)}
        if tool_acc:
            tcs = []
            for idx in sorted(tool_acc):
                s = tool_acc[idx]
                try:
                    args = json.loads(s["json"]) if s["json"].strip() else {}
                except json.JSONDecodeError:
                    args = {}
                tcs.append({"id": s["id"], "function": {"name": s["name"], "arguments": args}})
            final["tool_calls"] = tcs
        yield "", True, final

    def installed_models(self) -> list[str]:
        try:
            data = self._get_json("/v1/models")
            return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
        except Exception:
            return []

    def is_alive(self) -> bool:
        try:
            self._get_json("/v1/models")
            return True
        except Exception:
            return False


def _build_client(
    api_key: str | None = None,
    base_url: str | None = None,
    default_model: str | None = None,
    **_ignored,
) -> AnthropicClient:
    return AnthropicClient(
        api_key=api_key or "",
        base_url=base_url or _DEFAULT_BASE,
        default_model=default_model or _DEFAULT_MODEL,
    )


register_provider("anthropic", _build_client)
