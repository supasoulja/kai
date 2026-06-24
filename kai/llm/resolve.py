"""
Resolve a model-registry entry to a live LLM client.

This is the seam Brain will use (Phase 1 wiring) to talk to whichever brain an
agent/role is assigned: local Ollama, or a cloud provider whose API key lives in
the per-user keystore. Local needs no key; cloud looks its key up by the entry's
conn_id and builds the matching adapter via the provider factory.
"""
from __future__ import annotations

from kai.llm import keystore
from kai.llm.client import get_client, LLMClient


class LLMKeyMissing(RuntimeError):
    """A cloud model is selected but no API key is stored for its connection."""


def resolve_client(entry: dict, user_id: int) -> LLMClient:
    """Build the client for a registry entry. Raises LLMKeyMissing for a cloud
    entry with no stored key (callers fall back to local)."""
    import kai.llm.providers  # noqa: F401 — ensures cloud adapters are registered

    provider = entry.get("provider", "ollama")
    if provider == "ollama":
        return get_client("ollama", base_url=entry.get("base_url") or None)

    conn_id = entry.get("conn_id") or provider
    secret = keystore.get_secret(user_id, conn_id)
    if not secret:
        raise LLMKeyMissing(
            f"No API key stored for connection {conn_id!r} (provider {provider!r})."
        )
    return get_client(
        provider,
        api_key=secret,
        base_url=entry.get("base_url") or None,
        default_model=entry.get("ollama_id"),
    )
