"""
Cloud LLM provider adapters.

Importing this package registers every adapter with the factory in
kai/llm/client.py (each module calls register_provider at import). Code that
needs a cloud client imports this package first (the resolver does), so the
providers are available via client.get_client(<provider>).

Adapters normalize each provider's request/response/streaming into the same
shape Brain already consumes from OllamaClient, so Brain stays provider-agnostic.
"""
from kai.llm.providers import openai     # noqa: F401  (registers "openai")
from kai.llm.providers import anthropic  # noqa: F401  (registers "anthropic")
