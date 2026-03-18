"""
MemoryManager — single interface over all memory tiers.
This is what the Brain and CLI interact with.

Memory tiers:
  Semantic   — stable long-term facts (user name, preferences, hardware model)
  Episodic   — raw turns (temporary) + archived summaries (permanent, compressed by Brain)
  Procedural — behavioral rules (always injected)
  Session    — volatile runtime stats for this session only (CPU%, temps); never persisted

Memory routing:
  The router classifies queries and activates only relevant memory domains.
  Domain index is built once at startup and cached here.
"""
from typing import Callable

from kai.memory import semantic, procedural, episodic, extractor, context
from kai.memory import router
from kai.schema import SemanticFact, ProceduralRule, EpisodicEntry, ContextBlock

EmbedFn = Callable[[str], list[float]] | None


class MemoryManager:
    def __init__(self, embed_fn: EmbedFn = None):
        self.embed_fn = embed_fn
        # Volatile runtime stats for the current session (not persisted to DB)
        self._session_state: dict[str, str] = {}
        # Memory router domain index — built once via init_router()
        self._domain_index: dict[str, list[float]] = {}

    # ── Router ────────────────────────────────────────────────────────────────

    def init_router(
        self, embed_batch_fn: Callable[[list[str]], list[list[float]]]
    ) -> None:
        """
        Build the memory domain index at startup.
        Embeds 7 domain descriptions in one batch call (~30ms).
        Called by Brain alongside _ensure_tool_index().
        """
        if self._domain_index:
            return  # already built
        try:
            self._domain_index = router.build_domain_index(embed_batch_fn)
        except Exception:
            self._domain_index = {}  # fallback: routing disabled, inject everything

    # ── Semantic ───────────────────────────────────────────────────────────────

    def set_fact(self, key: str, value: str, source: str = "conversation") -> None:
        semantic.set_fact(key, value, source=source)

    def get_fact(self, key: str) -> str | None:
        return semantic.get_fact(key)

    def delete_fact(self, key: str) -> None:
        semantic.delete_fact(key)

    def list_facts(self) -> list[SemanticFact]:
        return semantic.list_facts()

    # ── Procedural ─────────────────────────────────────────────────────────────

    def set_rule(self, key: str, value: str) -> None:
        procedural.set_rule(key, value)

    def get_rule(self, key: str) -> str | None:
        return procedural.get_rule(key)

    def list_rules(self) -> list[ProceduralRule]:
        return procedural.list_rules()

    # ── Episodic ───────────────────────────────────────────────────────────────

    def add_episode(self, content: str, entry_type: str = "turn", metadata: dict | None = None) -> str:
        return episodic.add_entry(content, embed_fn=self.embed_fn, entry_type=entry_type, metadata=metadata)

    def search_episodes(self, query: str, top_k: int = 5) -> list[EpisodicEntry]:
        return episodic.search(query, embed_fn=self.embed_fn, top_k=top_k)

    def recent_episodes(self, limit: int = 5) -> list[EpisodicEntry]:
        return episodic.recent(limit=limit)

    def archive_history(self, summary_text: str) -> None:
        """
        Write a compressed history summary to episodic DB, then delete raw turns.
        Called by Brain when _session_history is compressed (token pressure) or at clear.
        Archives are retrieved only when semantically relevant — not injected every turn.
        The full verbatim transcript is preserved in episodic_transcripts for detail lookup.
        """
        # Capture full transcript BEFORE turns are deleted
        full_transcript = episodic.get_pending_turns_text()

        # Store the summary archive — returns the new entry ID
        archive_id = episodic.add_entry(
            content    = summary_text,
            embed_fn   = self.embed_fn,
            entry_type = "archive",
        )

        # Link the full transcript to this archive
        if full_transcript:
            episodic.save_transcript(archive_id, full_transcript)

        episodic.delete_turns()  # raw turns captured — clean them up

    def get_transcript(self, archive_id: str) -> str | None:
        """Retrieve the full verbatim transcript for a given archive entry ID."""
        return episodic.get_transcript(archive_id)

    # ── Session state (volatile, in-memory only) ───────────────────────────────

    def update_session_state(self, updates: dict[str, str]) -> None:
        """Merge runtime observations into the session cache. Not persisted."""
        self._session_state.update(updates)

    def get_session_state(self) -> dict[str, str]:
        return dict(self._session_state)

    # ── Context block ──────────────────────────────────────────────────────────

    def build_context(
        self,
        query: str = "",
        dm_mode: bool = False,
        query_embedding: list[float] | None = None,
    ) -> ContextBlock:
        return context.build(
            query=query,
            embed_fn=self.embed_fn,
            session_state=self._session_state,
            dm_mode=dm_mode,
            query_embedding=query_embedding,
            domain_index=self._domain_index or None,
        )

    def render_context(
        self,
        query: str = "",
        dm_mode: bool = False,
        query_embedding: list[float] | None = None,
    ) -> str:
        return self.build_context(
            query, dm_mode=dm_mode, query_embedding=query_embedding
        ).render()

    # ── Commit a conversation turn ─────────────────────────────────────────────

    def commit_turn(self, user_text: str, assistant_text: str) -> None:
        """
        After a turn completes:
        1. Extract stable facts from user message → semantic DB
        2. Extract stable observations from response → semantic DB
        3. Extract volatile runtime stats → session cache (not persisted)
        4. Store the raw turn in episodic DB (temporary staging)

        History compression is handled by Brain._maybe_compress_history(), which
        fires based on token pressure and writes archives via archive_history().
        """
        extractor.extract_and_save(user_text)
        extractor.extract_stable_observations(assistant_text)
        volatile = extractor.extract_volatile_observations(assistant_text)
        if volatile:
            self.update_session_state(volatile)

        content = f"User: {user_text}\nKai: {assistant_text}"
        self.add_episode(content, entry_type="turn")
