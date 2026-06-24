"""
Provider-agnostic LLM client surface.

Today every model call runs through the local Ollama client
(kai/llm/ollama.py). This module defines the common surface every provider —
local or cloud — implements, plus a small factory so a model-registry entry can
be turned into the right client. Cloud adapters (OpenAI-compatible, Anthropic,
Gemini) register themselves under kai/llm/providers/.

Phase 0 note: this is purely additive. Brain still constructs OllamaClient
directly; nothing here changes existing behavior until Brain is rewired to
resolve clients through the registry. OllamaClient already satisfies LLMClient,
so it is registered as the built-in "ollama" provider.
"""
from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Capabilities:
    """What a brain can do — drives the per-agent assignment guardrails
    (e.g. a tool-only agent can't be pointed at a no-tools model)."""
    tools: bool = False       # supports function / tool calling
    vision: bool = False      # accepts images
    streaming: bool = True    # supports token streaming
    thinking: bool = False    # native chain-of-thought
    local: bool = True        # runs on this machine — no data leaves the box


@runtime_checkable
class LLMClient(Protocol):
    """The methods Brain depends on. OllamaClient already matches this shape;
    cloud adapters implement the same surface. runtime_checkable means
    isinstance() verifies the methods exist (handy in tests)."""

    def chat(self, messages: list[dict], tools: list[dict] | None = ...,
             model: str = ..., think: bool = ..., temperature: float = ...) -> dict: ...

    def chat_stream(self, messages: list[dict], tools: list[dict] | None = ...,
                    model: str = ..., think: bool = ..., temperature: float = ...
                    ) -> Generator[tuple[str, bool, dict], None, None]: ...

    def installed_models(self) -> list[str]: ...

    def is_alive(self) -> bool: ...


# ── Provider factory ────────────────────────────────────────────────────────
# Adapters register a builder here. get_client() turns (provider, opts) into a
# live client. Keeps Brain ignorant of which provider it's talking to.

_BUILDERS: dict[str, Callable[..., LLMClient]] = {}


def register_provider(provider_id: str, builder: Callable[..., LLMClient]) -> None:
    """Register a builder for a provider id (e.g. 'ollama', 'openai')."""
    _BUILDERS[provider_id] = builder


def available_providers() -> list[str]:
    return sorted(_BUILDERS)


def get_client(provider: str = "ollama", **opts) -> LLMClient:
    """Build a client for a provider. Extra kwargs (base_url, api_key, …) pass
    straight to the adapter's builder."""
    builder = _BUILDERS.get(provider)
    if builder is None:
        raise ValueError(
            f"Unknown LLM provider: {provider!r}. Known: {available_providers()}"
        )
    return builder(**opts)


# ── Built-in: local Ollama ──────────────────────────────────────────────────
def _ollama_builder(base_url: str | None = None, **_ignored) -> LLMClient:
    # Imported lazily so this module stays import-cheap and cycle-free.
    from kai.llm.ollama import OllamaClient
    return OllamaClient(base_url) if base_url else OllamaClient()


register_provider("ollama", _ollama_builder)
