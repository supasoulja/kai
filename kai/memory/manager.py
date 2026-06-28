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
from kai.memory.knowledge import KnowledgeStore
from kai.store.schema import SemanticFact, ProceduralRule, EpisodicEntry, ContextBlock

EmbedFn = Callable[[str], list[float]] | None


class MemoryManager:
    def __init__(self, embed_fn: EmbedFn = None, user_id: int = 0):
        self.embed_fn = embed_fn
        self.user_id = user_id
        # Volatile runtime stats for the current session (not persisted to DB)
        self._session_state: dict[str, str] = {}
        # Memory router domain index — built once via init_router()
        self._domain_index: dict[str, list[float]] = {}
        # Per-user knowledge store (researcher-learned facts)
        self._knowledge: KnowledgeStore = KnowledgeStore(user_id)

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
        semantic.set_fact(key, value, source=source, user_id=self.user_id)

    def get_fact(self, key: str) -> str | None:
        return semantic.get_fact(key, user_id=self.user_id)

    def delete_fact(self, key: str) -> None:
        semantic.delete_fact(key, user_id=self.user_id)

    def list_facts(self) -> list[SemanticFact]:
        return semantic.list_facts(user_id=self.user_id)

    # ── Procedural ─────────────────────────────────────────────────────────────

    def set_rule(self, key: str, value: str) -> None:
        procedural.set_rule(key, value, user_id=self.user_id)

    def get_rule(self, key: str) -> str | None:
        return procedural.get_rule(key, user_id=self.user_id)

    def list_rules(self) -> list[ProceduralRule]:
        return procedural.list_rules(user_id=self.user_id)

    # ── Episodic ───────────────────────────────────────────────────────────────

    def add_episode(self, content: str, entry_type: str = "turn", metadata: dict | None = None) -> str:
        return episodic.add_entry(
            content, embed_fn=self.embed_fn, entry_type=entry_type,
            metadata=metadata, user_id=self.user_id,
        )

    def search_episodes(self, query: str, top_k: int = 5) -> list[EpisodicEntry]:
        return episodic.search(query, embed_fn=self.embed_fn, top_k=top_k, user_id=self.user_id)

    def recent_episodes(self, limit: int = 5) -> list[EpisodicEntry]:
        return episodic.recent(limit=limit, user_id=self.user_id)

    def archive_history(self, summary_text: str) -> None:
        """
        Write a compressed history summary to episodic DB, then delete raw turns.
        Called by Brain when _session_history is compressed (token pressure) or at clear.
        Archives are retrieved only when semantically relevant — not injected every turn.
        The full verbatim transcript is preserved in episodic_transcripts for detail lookup.
        """
        # Summary, transcript, and turn deletion happen in one transaction so a
        # crash can never leave raw turns + archive both present or orphan a
        # transcript. (See episodic.archive_and_clear_turns.)
        episodic.archive_and_clear_turns(
            summary_text,
            embed_fn = self.embed_fn,
            user_id  = self.user_id,
        )

    def get_transcript(self, archive_id: str) -> str | None:
        """Retrieve the full verbatim transcript for a given archive entry ID."""
        return episodic.get_transcript(archive_id, user_id=self.user_id)

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
        query_embedding: list[float] | None = None,
        include_welcome_back: bool = True,
    ) -> ContextBlock:
        return context.build(
            query=query,
            embed_fn=self.embed_fn,
            session_state=self._session_state,
            query_embedding=query_embedding,
            domain_index=self._domain_index or None,
            user_id=self.user_id,
            include_welcome_back=include_welcome_back,
        )

    def render_context(
        self,
        query: str = "",
        query_embedding: list[float] | None = None,
        include_welcome_back: bool = True,
    ) -> str:
        return self.build_context(
            query, query_embedding=query_embedding,
            include_welcome_back=include_welcome_back,
        ).render()

    # ── Knowledge store (researcher-learned facts) ─────────────────────────────

    def learn_knowledge(
        self, content: str, source: str = "researcher", topic: str | None = None
    ) -> None:
        """Save a fact the researcher discovered to this user's knowledge store."""
        self._knowledge.learn(content, embed_fn=self.embed_fn, source=source, topic=topic)

    def search_knowledge(self, query: str, top_k: int = 5,
                         query_embedding: list[float] | None = None) -> list[dict]:
        """Vector search the user's learned knowledge store.

        `query_embedding` (if given) reuses an embedding already computed this
        turn instead of re-embedding the query.
        """
        return self._knowledge.search(query, embed_fn=self.embed_fn, top_k=top_k,
                                      query_embedding=query_embedding)

    def knowledge_count(self) -> int:
        return self._knowledge.count()

    def recent_knowledge(self, limit: int = 10) -> list[dict]:
        return self._knowledge.recent(limit=limit)

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
        extractor.extract_and_save(user_text, user_id=self.user_id)
        extractor.extract_stable_observations(assistant_text, user_id=self.user_id)
        volatile = extractor.extract_volatile_observations(assistant_text)
        if volatile:
            self.update_session_state(volatile)

        content = f"User: {user_text}\nKai: {assistant_text}"
        self.add_episode(content, entry_type="turn")
