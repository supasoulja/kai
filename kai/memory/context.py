"""
Assembles the full context block injected into every LLM call.
Format: [IDENTITY] [MEMORY DIRECTORY] [PROCEDURAL] [SEMANTIC] [EPISODIC] [SESSION] [UPLOADED FILES]

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
"""
import time
from concurrent.futures import ThreadPoolExecutor
from kai.config import MAX_CONTEXT_CHARS, EPISODIC_TOP_K, RAG_TOP_K, RAG_THRESHOLD
from kai.persona.identity import build_identity_block
from kai.memory import semantic, procedural, episodic
from kai.memory import router
from kai.store.schema import ContextBlock
from kai.core.sleep import load_welcome_back, clear_welcome_back
from typing import Callable

# Shared pool for parallel retrieval — 2 workers covers episodic + RAG.
_retrieval_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kai-retrieve")


def build(
    query: str = "",
    embed_fn: Callable[[str], list[float]] | None = None,
    session_state: dict[str, str] | None = None,
    query_embedding: list[float] | None = None,
    domain_index: dict[str, list[float]] | None = None,
    user_id: int = 0,
    include_welcome_back: bool = True,
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
    identity_text = build_identity_block(user_id=user_id)
    proc_rules    = procedural.list_rules(user_id=user_id)
    all_facts     = semantic.list_facts(user_id=user_id)
    doc_inv       = _fetch_doc_inventory(user_id=user_id)
    tool_index    = _render_tool_index(user_id=user_id)

    # ── Route: classify query → active domains ────────────────────────────────
    if query_embedding and domain_index:
        active = router.classify(query_embedding, domain_index)
    else:
        active = set(router._MEMORY_DOMAINS.keys())  # fallback: everything

    # ── Semantic facts: filtered by active domains ────────────────────────────
    sem_facts = router.filter_facts(all_facts, active)

    # ── Parallel retrieval: episodic + RAG run concurrently ───────────────────
    episodes: list = []
    rag_chunks: list[dict] = []

    futures: dict = {}
    if "history" in active:
        futures["episodic"] = _retrieval_pool.submit(
            _fetch_episodic, query, embed_fn, query_embedding, user_id,
        )
    if "documents" in active:
        futures["rag"] = _retrieval_pool.submit(
            _fetch_rag_chunks, query, embed_fn, query_embedding, user_id,
        )

    for key, fut in futures.items():
        try:
            result = fut.result(timeout=10)
            if key == "episodic":
                episodes = result
            elif key == "rag":
                rag_chunks = result
        except Exception:
            pass  # graceful degradation — missing context is better than a crash

    # ── Memory directory: always built, always injected ───────────────────────
    directory = router.build_directory(
        semantic_facts=all_facts,
        doc_inventory=doc_inv,
        episodic_count=router.get_episodic_count(user_id=user_id),
        learned_count=router.get_learned_count(user_id=user_id),
        session_keys=list((session_state or {}).keys()),
    )

    # Welcome-back message — injected once on first turn, then cleared.
    # New-chat greetings opt out so they don't consume/show the morning note.
    welcome_back = _get_and_clear_welcome_back() if include_welcome_back else ""
    if include_welcome_back:
        watchdog_note = _get_and_clear_watchdog_events()
        if watchdog_note:
            welcome_back = f"{welcome_back}\n\n{watchdog_note}" if welcome_back else watchdog_note
        briefing = _get_and_clear_briefing(user_id=user_id)
        if briefing:
            welcome_back = f"{welcome_back}\n\n{briefing}" if welcome_back else briefing

    # Active goals — always injected so Kai never forgets ongoing tasks
    goals_block = _get_active_goals(user_id=user_id)
    # Proactive pattern suggestion (time-of-day patterns)
    pattern_note = _get_pattern_suggestion(user_id=user_id) if include_welcome_back else ""

    merged_state = dict(session_state or {})
    if goals_block:
        merged_state["active_goals"] = goals_block
    if pattern_note:
        merged_state["proactive"] = pattern_note

    block = ContextBlock(
        identity=identity_text,
        memory_directory=directory,
        procedural=proc_rules,
        semantic=sem_facts,
        episodic=episodes,
        session_state=merged_state,
        rag_chunks=rag_chunks,
        doc_inventory=doc_inv,
        welcome_back=welcome_back,
        tool_index=tool_index,
    )

    budget = MAX_CONTEXT_CHARS

    # Trim if over budget — drop oldest episodic first, then RAG chunks.
    # Compute rendered length once and estimate savings to avoid O(n^2) re-renders.
    rendered_len = len(block.render())
    while block.episodic and rendered_len > budget:
        removed = block.episodic.pop(0)
        # Estimate char savings (entry text + formatting overhead)
        rendered_len -= len(str(removed)) + 20
    if rendered_len > budget:
        rendered_len = len(block.render())  # re-sync after episodic trimming
    while block.rag_chunks and rendered_len > budget:
        removed = block.rag_chunks.pop()
        rendered_len -= len(str(removed)) + 20

    return block


_welcome_back_used = False

def _get_and_clear_welcome_back() -> str:
    """Load welcome-back message on first call, return empty string after.
    Does NOT clear the file — call mark_welcome_back_delivered() after a
    successful response so the note survives timeouts and crashes."""
    global _welcome_back_used
    if _welcome_back_used:
        return ""
    _welcome_back_used = True
    # NOTE: no persona "gap" check here. Tools are auto-documented in the memory
    # tree (tool_docs.sync_tool_docs) and injected as the [TOOLS] block every turn
    # — persona.md is identity/voice, not a tool catalog. New capabilities are
    # surfaced as an awareness bubble (see kai/memory/capabilities.py), not nagged
    # into the cold-open greeting.
    msg = load_welcome_back()
    return msg or ""


def mark_welcome_back_delivered():
    """Clear the welcome-back file after a successful first response."""
    try:
        msg = load_welcome_back()
        if msg:
            clear_welcome_back()
    except Exception:
        pass


_watchdog_events_used = False
_pending_watchdog_ids: list[int] = []

def _get_and_clear_watchdog_events() -> str:
    """
    Load any pending watchdog reports on first call this session, return empty
    after. Mirrors _get_and_clear_welcome_back: doesn't mark them delivered
    here — call mark_watchdog_events_delivered() after a successful response
    so a report survives a crash/timeout and gets surfaced again next time.
    """
    global _watchdog_events_used, _pending_watchdog_ids
    if _watchdog_events_used:
        return ""
    _watchdog_events_used = True
    try:
        from kai import watchdog_queue
        events = watchdog_queue.get_pending_events()
    except Exception:
        return ""
    if not events:
        return ""

    _pending_watchdog_ids = [e["id"] for e in events]
    lines = ["[WATCHDOG REPORTS — from monitoring scripts on the network]"]
    for e in events:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"]))
        lines.append(
            f"- [{e['severity']}] {e['label']}/{e['script_id']} at {when}: "
            f"{e['message']} — {e['suggestion']}"
        )
    return "\n".join(lines)


def mark_watchdog_events_delivered():
    """Mark this session's surfaced watchdog reports delivered after a successful
    first response — mirrors mark_welcome_back_delivered's crash-survival semantics."""
    global _pending_watchdog_ids
    if not _pending_watchdog_ids:
        return
    try:
        from kai import watchdog_queue
        watchdog_queue.mark_delivered(_pending_watchdog_ids)
        _pending_watchdog_ids = []
    except Exception:
        pass


def _fetch_episodic(
    query: str,
    embed_fn: Callable[[str], list[float]] | None,
    query_embedding: list[float] | None = None,
    user_id: int = 0,
) -> list:
    """
    Fetch episodic context for the current query.
    Prefers archived summaries (non-turn entries) — they are concise and cross-session.
    Falls back to raw turns if no summaries exist yet (e.g. first session before any
    compression or clear-chat has fired).

    Semantic recall needs a real query. Cold-open greetings build context with an
    empty query and no embedding; embedding "" and KNN-searching it returns the same
    arbitrary "nearest to nothing" archives on every boot — recall that ignores
    context and reliably resurfaces stale topics. Skip it (mirrors _fetch_rag_chunks);
    cold-open continuity comes from the welcome-back note, not similarity search.
    """
    if not query.strip():
        return []
    results = episodic.search_non_turns(
        query.strip(), embed_fn=embed_fn, top_k=EPISODIC_TOP_K,
        query_embedding=query_embedding, user_id=user_id,
    )
    if not results:
        results = episodic.search(
            query.strip(), embed_fn=embed_fn, top_k=EPISODIC_TOP_K,
            query_embedding=query_embedding, user_id=user_id,
        )
    return results


def _fetch_rag_chunks(
    query: str,
    embed_fn: Callable[[str], list[float]] | None,
    query_embedding: list[float] | None = None,
    user_id: int = 0,
) -> list[dict]:
    """
    Auto-inject relevant document chunks from uploaded files.
    Only fires when documents exist and embed_fn is available.
    Similarity-gated by RAG_THRESHOLD so irrelevant docs don't pollute context.
    """
    if not (embed_fn or query_embedding) or not query.strip():
        return []
    try:
        from kai.memory import documents as _docs
        if not _docs.has_documents(user_id=user_id):
            return []
        results = _docs.search(
            query.strip(), embed_fn=embed_fn, top_k=RAG_TOP_K,
            query_embedding=query_embedding, user_id=user_id,
        )
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


def _render_tool_index(user_id: int = 0) -> str:
    """[TOOLS] index — one line per documented tool. Empty if sync hasn't run."""
    try:
        from kai.memory.tool_docs import render_tool_index
        return render_tool_index(user_id)
    except Exception:
        return ""


def _fetch_doc_inventory(user_id: int = 0) -> list[dict]:
    """
    Return a brief list of all uploaded documents (filename + type + chunk count).
    Cheap query — no embeddings, no content. Always runs so the model knows
    what documents exist even when no chunks matched the current query.
    """
    try:
        from kai.memory import documents as _docs
        if not _docs.has_documents(user_id=user_id):
            return []
        return _docs.list_documents(user_id=user_id)
    except Exception:
        return []


_briefing_used = False

def _get_and_clear_briefing(user_id: int = 0) -> str:
    """Load the pending daily briefing once per session."""
    global _briefing_used
    if _briefing_used:
        return ""
    _briefing_used = True
    try:
        from kai.memory.briefing import get_pending
        return get_pending(user_id=user_id)
    except Exception:
        return ""


def mark_briefing_delivered(user_id: int = 0) -> None:
    """Mark pending briefings delivered after a successful first response."""
    try:
        from kai.memory.briefing import mark_delivered
        mark_delivered(user_id=user_id)
    except Exception:
        pass


def _get_active_goals(user_id: int = 0) -> str:
    """Return a compact goals block for context injection. Empty if no active goals."""
    try:
        from kai.store.db import get_conn
        import json as _json
        conn = get_conn()
        rows = conn.execute(
            "SELECT title, steps_json, current_step FROM goals "
            "WHERE user_id = ? AND status = 'active' ORDER BY last_active DESC LIMIT 5",
            (user_id,),
        ).fetchall()
        if not rows:
            return ""
        lines = ["[ACTIVE GOALS]"]
        for title, steps_json, current_step in rows:
            steps = _json.loads(steps_json) if steps_json else []
            if steps and current_step < len(steps):
                next_step = steps[current_step]
                lines.append(f"- {title} → next: {next_step}")
            else:
                lines.append(f"- {title} (in progress)")
        return "\n".join(lines)
    except Exception:
        return ""


def _get_pattern_suggestion(user_id: int = 0) -> str:
    """Return a proactive pattern suggestion if one matches the current time."""
    try:
        from kai.memory.patterns import get_proactive_suggestion
        return get_proactive_suggestion(user_id=user_id)
    except Exception:
        return ""
