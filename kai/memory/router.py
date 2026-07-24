"""
Memory Router — the "parking garage directory" for Kai's memory.

Instead of dumping ALL semantic facts, episodic entries, and document chunks
into every system prompt, this module classifies the user's query and routes
to only the relevant memory stores.

Pattern reused from kai/tools/registry.py (tool category routing):
  1. Static domain definitions with human-readable descriptions
  2. Embed descriptions once at startup (batch call)
  3. Per turn: cosine-rank domains, return top-k active set
  4. Fallback to full injection if embedding fails

The Memory Directory is a tiny always-injected block (~200 chars) that tells
Kai what data exists in each store — the "sign at the entrance" — so she
knows what's available even when the actual data isn't loaded.
"""
from typing import Callable

from kai.llm.vecmath import cosine as _cosine

from kai.config import MEMORY_ROUTER_TOP_K, MEMORY_ROUTER_THRESHOLD
from kai.store.schema import SemanticFact


# ── Domain definitions ────────────────────────────────────────────────────────
# Descriptions written in USER-QUERY language (how people ask about these
# topics), not sysadmin jargon. This maximizes embedding similarity when
# the user's natural phrasing hits the same semantic space.

_MEMORY_DOMAINS: dict[str, dict] = {
    "identity": {
        "description": (
            "personal information about the user, their name, who they are, "
            "where they live, their job or role"
        ),
        "fact_prefixes": ["user_name", "user_role", "location"],
        "stores": ["semantic"],
    },
    "preferences": {
        "description": (
            "what the user likes, dislikes, prefers to use, hobbies, "
            "games they play, favorite things"
        ),
        "fact_prefixes": ["preference", "uses", "gaming"],
        "stores": ["semantic"],
    },
    "hardware": {
        "description": (
            "computer specs, PC hardware, system RAM, GPU, CPU, disk space, "
            "software versions, operating system"
        ),
        "fact_prefixes": ["sys_"],
        "stores": ["semantic", "session"],
    },
    "documents": {
        "description": (
            "uploaded files, PDFs, documents, reading material, file contents, "
            "what's in my documents"
        ),
        "stores": ["rag"],
    },
    "history": {
        "description": (
            "past conversations, what we discussed before, previous sessions, "
            "memories, do you remember, recall"
        ),
        "stores": ["episodic"],
    },
    "notes": {
        "description": (
            "things to remember, saved notes, reminders, things I told you "
            "to keep track of"
        ),
        "fact_prefixes": ["note"],
        "stores": ["semantic"],
    },
}


# ── Cosine similarity ─────────────────────────────────────────────────────────
# ── Startup: build domain index ──────────────────────────────────────────────

def build_domain_index(
    embed_batch_fn: Callable[[list[str]], list[list[float]]],
) -> dict[str, list[float]]:
    """
    Embed all domain descriptions in one batch call at startup.
    Returns {domain_name: embedding_vector}.
    Called once — cached in MemoryManager._domain_index.
    """
    names = list(_MEMORY_DOMAINS.keys())
    descs = [_MEMORY_DOMAINS[n]["description"] for n in names]
    vecs = embed_batch_fn(descs)
    return dict(zip(names, vecs, strict=True))


# ── Per-turn: classify query ─────────────────────────────────────────────────

def classify(
    query_embedding: list[float],
    domain_index: dict[str, list[float]],
    top_k: int | None = None,
    threshold: float | None = None,
) -> set[str]:
    """
    Rank memory domains by cosine similarity to the query embedding.
    Returns the set of active domain names (top-k above threshold).

    If no domains score above threshold, returns ALL domain names
    (fallback to dump-everything behavior).
    """
    k = top_k if top_k is not None else MEMORY_ROUTER_TOP_K
    t = threshold if threshold is not None else MEMORY_ROUTER_THRESHOLD

    if not domain_index or not query_embedding:
        return set(_MEMORY_DOMAINS.keys())  # fallback: everything

    scores = sorted(
        ((name, _cosine(query_embedding, vec)) for name, vec in domain_index.items()),
        key=lambda pair: pair[1],
        reverse=True,
    )

    active: set[str] = set()
    for name, score in scores[:k]:
        if score < t:
            break
        active.add(name)

    # Fallback: if nothing matched, inject everything (safe default)
    if not active:
        return set(_MEMORY_DOMAINS.keys())

    # Identity is ALWAYS active — you always know who you're talking to.
    # These facts are tiny (name, role, location) and critical for Kai
    # to feel like she knows the user. Same logic as search.web always
    # being available in tool routing.
    active.add("identity")

    return active


# ── Semantic fact filtering ──────────────────────────────────────────────────

def filter_facts(
    facts: list[SemanticFact],
    active_domains: set[str],
) -> list[SemanticFact]:
    """
    Return only facts whose key matches an active domain's prefix list.
    Facts with keys that don't match ANY domain are always included
    (unknown facts shouldn't be silently dropped).
    """
    # Collect all prefixes for active domains
    active_prefixes: list[str] = []
    all_prefixes: list[str] = []
    for name, domain in _MEMORY_DOMAINS.items():
        prefixes = domain.get("fact_prefixes", [])
        all_prefixes.extend(prefixes)
        if name in active_domains:
            active_prefixes.extend(prefixes)

    # If all domains active (fallback), return everything
    if active_domains == set(_MEMORY_DOMAINS.keys()):
        return facts

    result: list[SemanticFact] = []
    for fact in facts:
        key = fact.key
        # Check if this fact belongs to an active domain
        if any(key == prefix or key.startswith(prefix + "_")
               for prefix in active_prefixes):
            result.append(fact)
        # Also include facts that don't belong to ANY known domain
        # (orphan facts — unknown keys shouldn't be silently dropped)
        elif not any(key == prefix or key.startswith(prefix + "_")
                     for prefix in all_prefixes):
            result.append(fact)

    return result


# ── Memory directory builder ─────────────────────────────────────────────────

def build_directory(
    semantic_facts: list[SemanticFact],
    doc_inventory: list[dict],
    episodic_count: int,
    learned_count: int = 0,
    session_keys: list[str] | None = None,
) -> str:
    """
    Build the tiny always-injected directory block.
    Lists what data exists in each store so Kai knows what's available
    without the actual data being loaded.

    ~150-300 chars — cheap to always include.
    """
    lines: list[str] = []

    # Group semantic facts by domain
    domain_facts: dict[str, list[str]] = {}
    for name, domain in _MEMORY_DOMAINS.items():
        prefixes = domain.get("fact_prefixes", [])
        if not prefixes:
            continue
        matching = [
            f.key for f in semantic_facts
            if any(f.key == p or f.key.startswith(p + "_")
                   for p in prefixes)
        ]
        if matching:
            domain_facts[name] = matching

    # Identity
    if "identity" in domain_facts:
        keys = domain_facts["identity"]
        lines.append(f"- Identity: {', '.join(keys)} ({len(keys)} fact{'s' if len(keys) != 1 else ''})")

    # Preferences
    if "preferences" in domain_facts:
        count = len(domain_facts["preferences"])
        lines.append(f"- Preferences: {count} fact{'s' if count != 1 else ''} stored")

    # Hardware
    if "hardware" in domain_facts:
        keys = domain_facts["hardware"]
        lines.append(f"- Hardware: {', '.join(keys)} ({len(keys)} fact{'s' if len(keys) != 1 else ''})")

    # Notes
    if "notes" in domain_facts:
        count = len(domain_facts["notes"])
        lines.append(f"- Notes: {count} saved note{'s' if count != 1 else ''}")

    # Documents — note count only; don't list filenames unprompted
    if doc_inventory:
        n = len(doc_inventory)
        lines.append(f"- Documents: {n} uploaded file{'s' if n != 1 else ''} (use docs.list to see names)")

    # Episodic
    if episodic_count > 0:
        lines.append(f"- History: {episodic_count} past conversation{'s' if episodic_count != 1 else ''} searchable")

    # Learned knowledge
    if learned_count > 0:
        lines.append(f"- Learned: {learned_count} knowledge entr{'ies' if learned_count != 1 else 'y'} from past conversations")

    # Session
    if session_keys:
        lines.append(f"- Session: live system stats available ({', '.join(session_keys[:4])})")

    if not lines:
        return ""  # no data anywhere — skip the block entirely

    return "[MEMORY DIRECTORY — What you have available]\n" + "\n".join(lines)


# ── Episodic count (lightweight query) ───────────────────────────────────────

def get_episodic_and_learned_counts(user_id: int = 0) -> tuple[int, int]:
    """Both episodic-archive and learned counts in a single scan of
    episodic_entries. Returns (episodic_count, learned_count)."""
    try:
        from kai.store.db import get_conn
        conn = get_conn()
        row = conn.execute(
            "SELECT "
            "  SUM(CASE WHEN entry_type NOT IN ('turn', 'learned') THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN entry_type = 'learned' THEN 1 ELSE 0 END) "
            "FROM episodic_entries WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        if not row:
            return 0, 0
        return int(row[0] or 0), int(row[1] or 0)
    except Exception:
        return 0, 0


def get_episodic_count(user_id: int = 0) -> int:
    """Count archive/summary episodic entries (excludes turns and learned)."""
    return get_episodic_and_learned_counts(user_id)[0]


def get_learned_count(user_id: int = 0) -> int:
    """Count knowledge entries extracted from conversations."""
    return get_episodic_and_learned_counts(user_id)[1]
