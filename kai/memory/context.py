"""
Assembles the full context block injected into every LLM call.
Format: [IDENTITY] [MEMORY DIRECTORY] [PROCEDURAL] [SEMANTIC] [EPISODIC] [SESSION] [UPLOADED FILES] [CAMPAIGN]

Memory routing ("parking garage directory"):
  The router classifies each query and activates only relevant memory domains.
  The Memory Directory block is always injected — a tiny summary of what data
  exists so Kai knows where to look even when the actual data isn't loaded.

Memory tiers:
  - Identity + Procedural: always injected (persona + rules)
  - Memory Directory: always injected (~200 chars — what stores have data)
  - Semantic: ROUTED — only facts matching active domains are injected
  - Episodic: ROUTED — only searched when "history" domain is active
  - Session: always injected (tiny, volatile runtime stats)
  - Uploaded files: always injected (tiny inventory)
  - RAG chunks: ROUTED — only searched when "documents" domain is active
  - Campaign: only injected when DM mode + "campaign" domain active
"""
from kai.config import MAX_CONTEXT_CHARS, DM_CONTEXT_CHARS, EPISODIC_TOP_K, RAG_TOP_K, RAG_THRESHOLD
from kai.identity import build_identity_block
from kai.memory import semantic, procedural, episodic
from kai.memory import router
from kai.schema import ContextBlock
from typing import Callable


def build(
    query: str = "",
    embed_fn: Callable[[str], list[float]] | None = None,
    session_state: dict[str, str] | None = None,
    dm_mode: bool = False,
    query_embedding: list[float] | None = None,
    domain_index: dict[str, list[float]] | None = None,
) -> ContextBlock:
    """
    Build a ContextBlock for the given query.

    New parameters (memory routing):
      query_embedding — pre-computed embedding of the user's query (avoids double embed)
      domain_index    — pre-built domain index from router.build_domain_index()

    When both are provided, the router classifies the query and only fetches
    data from relevant memory stores. When missing, falls back to injecting
    everything (same as pre-router behavior).
    """
    identity_text = build_identity_block()
    proc_rules    = procedural.list_rules()
    all_facts     = semantic.list_facts()
    doc_inv       = _fetch_doc_inventory()

    # ── Route: classify query → active domains ────────────────────────────────
    if query_embedding and domain_index:
        active = router.classify(query_embedding, domain_index)
    else:
        active = set(router._MEMORY_DOMAINS.keys())  # fallback: everything

    # DM mode always activates campaign domain
    if dm_mode:
        active.add("campaign")

    # ── Semantic facts: filtered by active domains ────────────────────────────
    sem_facts = router.filter_facts(all_facts, active)

    # ── Episodic: only search when "history" domain is active ─────────────────
    episodes = _fetch_episodic(query, embed_fn) if "history" in active else []

    # ── RAG chunks: only search when "documents" domain is active ─────────────
    rag_chunks = _fetch_rag_chunks(query, embed_fn) if "documents" in active else []

    # ── Campaign: only when dm_mode AND domain active ─────────────────────────
    campaign_text = ""
    if dm_mode and "campaign" in active:
        campaign_text = _fetch_campaign(query, embed_fn)

    # ── Memory directory: always built, always injected ───────────────────────
    directory = router.build_directory(
        semantic_facts=all_facts,
        doc_inventory=doc_inv,
        episodic_count=router.get_episodic_count(),
        learned_count=router.get_learned_count(),
        campaign_name=router.get_active_campaign_name() if dm_mode else None,
        session_keys=list((session_state or {}).keys()),
    )

    block = ContextBlock(
        identity=identity_text,
        memory_directory=directory,
        procedural=proc_rules,
        semantic=sem_facts,
        episodic=episodes,
        session_state=session_state or {},
        campaign=campaign_text,
        rag_chunks=rag_chunks,
        doc_inventory=doc_inv,
    )

    # Use a larger budget in DM mode — campaigns need more context
    budget = DM_CONTEXT_CHARS if dm_mode else MAX_CONTEXT_CHARS

    # Trim if over budget — drop oldest episodic first, then RAG chunks
    while block.episodic and len(block.render()) > budget:
        block.episodic.pop(0)
    while block.rag_chunks and len(block.render()) > budget:
        block.rag_chunks.pop()

    return block


def _fetch_episodic(
    query: str,
    embed_fn: Callable[[str], list[float]] | None,
) -> list:
    """
    Fetch episodic context for the current query.
    Prefers archived summaries (non-turn entries) — they are concise and cross-session.
    Falls back to raw turns if no summaries exist yet (e.g. first session before any
    compression or clear-chat has fired).
    """
    results = episodic.search_non_turns(query.strip(), embed_fn=embed_fn, top_k=EPISODIC_TOP_K)
    if not results:
        # No archives yet — surface raw turns so the model has some cross-session context.
        # Raw turns are larger but better than nothing while the system is still warm.
        results = episodic.search(query.strip(), embed_fn=embed_fn, top_k=EPISODIC_TOP_K)
    return results


def _fetch_rag_chunks(
    query: str,
    embed_fn: Callable[[str], list[float]] | None,
) -> list[dict]:
    """
    Auto-inject relevant document chunks from uploaded files.
    Only fires when documents exist and embed_fn is available.
    Similarity-gated by RAG_THRESHOLD so irrelevant docs don't pollute context.
    """
    if not embed_fn or not query.strip():
        return []
    try:
        from kai.memory import documents as _docs
        if not _docs.has_documents():
            return []
        results = _docs.search(query.strip(), embed_fn=embed_fn, top_k=RAG_TOP_K)
        return [
            {
                "doc_name":    r["doc_name"],
                "content":     r["content"],
                "chunk_index": r["chunk_index"],
            }
            for r in results
            if r["distance"] <= RAG_THRESHOLD
        ]
    except Exception:
        return []


def _fetch_doc_inventory() -> list[dict]:
    """
    Return a brief list of all uploaded documents (filename + type + chunk count).
    Cheap query — no embeddings, no content. Always runs so the model knows
    what documents exist even when no chunks matched the current query.
    """
    try:
        from kai.memory import documents as _docs
        if not _docs.has_documents():
            return []
        return _docs.list_documents()
    except Exception:
        return []


def _fetch_campaign(
    query: str,
    embed_fn: Callable[[str], list[float]] | None,
) -> str:
    """Fetch the active campaign's context block (NPCs, quests, events)."""
    try:
        from kai import campaign as _camp
        active = _camp.get_active_campaign()
        if not active:
            return ""
        return _camp.build_campaign_context(
            campaign_id=active["id"],
            query=query,
            embed_fn=embed_fn,
        )
    except Exception:
        return ""
