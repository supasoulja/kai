"""
kai/memory/scorer.py — Version C probabilistic scoring equation.

Every factor answers one plain English question:
  P(still true)  × P(correct) × P(relevant now) × boost(P(worth surfacing), frequency)
           ↓              ↓             ↓                       ↓
        recency      confidence    similarity × domain      importance × specificity + frequency lift

When a node scores low you know exactly which question failed it.
"""

import math  # for exp() in the decay formula
import time  # for current timestamp in recency calc
from typing import Optional  # Optional[X] means "X or None"

import numpy as np  # for cosine similarity math

from kai.llm.vecmath import cosine as _vec_cosine  # shared cosine math

from . import tree as _tree  # module import lets us call tree.hardcoded_nodes() etc.
from .tree import Node  # import the Node type we defined in tree.py

# Shorthand for a numpy array that might not exist yet (before embedding runs)
MaybeArray = Optional[np.ndarray]


# How quickly different source types age out — in days for a 50% confidence drop.
# A "stated" fact (user said it explicitly) decays slowly.
# A "pattern" fact (inferred from behavior) decays faster because behavior changes.
_HALF_LIFE: dict[str, float] = {
    "stated": 365 * 2,  # 2 years — user said it explicitly, trust it until corrected
    "inferred": 365,  # 1 year — inferred from conversation patterns
    "pattern": 180,  # 6 months — behavioral signals drift faster
}

# Frequency cap: 20+ confirmations is treated as "fully established"
# This prevents an old frequently-queried node from scoring artificially high forever
_FREQ_CAP = 20


# ─── Individual probability factors ───────────────────────────────────────────


def _recency(node: Node) -> float:
    """
    P(still true) — how likely is it this fact is still accurate?
    Uses exponential decay: starts at 1.0, halves every HALF_LIFE days.
    Non-decaying nodes (hardware, medical) always return 1.0.
    """
    if not node.decays:  # permanent nodes never age out
        return 1.0

    # How many days have passed since this node was last updated?
    days_old = (time.time() - node.last_updated) / 86400  # 86400 seconds in a day

    # Pick the decay rate based on how this node was learned
    half_life = _HALF_LIFE.get(node.source, 365)  # .get() returns default 365 if source unknown

    # Exponential decay formula: e^(-t × ln(2) / half_life)
    # At t=0: e^0 = 1.0 (fresh, full confidence)
    # At t=half_life: e^(-ln2) = 0.5 (half confidence)
    return math.exp(-days_old * math.log(2) / half_life)


def _cosine(a: MaybeArray, b: MaybeArray) -> float:
    """
    Cosine similarity between two embedding vectors.
    Returns how similar two pieces of text are in meaning: 1.0 = identical, 0.0 = unrelated.
    Returns 0.5 (neutral, not punishing) if either embedding is missing.
    """
    if a is None or b is None:  # can't compare if embeddings haven't been computed yet
        return 0.5  # neutral — don't punish nodes that haven't been embedded

    if np.linalg.norm(a) == 0.0 or np.linalg.norm(b) == 0.0:  # zero vector → neutral, not 0.0
        return 0.5

    return _vec_cosine(a, b)  # shared cosine: dot / (|a| × |b|), in [-1, 1]


def _domain_match(node: Node, active_domains: set[str]) -> float:
    """
    How well does this node's domain tag align with what's being discussed?
    Returns 1.0 if either side has no domain info (treat as universal).
    Returns fraction of overlap when both sides have domain tags.
    """
    if not node.domain or not active_domains:  # no domain info means this node is universal
        return 1.0

    node_domains = set(node.domain.split(","))  # "gaming,hardware" → {"gaming", "hardware"}
    overlap = node_domains & active_domains  # & = set intersection: elements in both sets
    return len(overlap) / len(node_domains)  # e.g. 1/2 if "gaming" matches but "hardware" doesn't


def _norm_frequency(node: Node) -> float:
    """
    Normalize raw frequency count to 0.0–1.0.
    Caps at _FREQ_CAP so old nodes with many hits can't dominate indefinitely.
    """
    return min(node.frequency / _FREQ_CAP, 1.0)  # min() clamps: never exceeds 1.0


# ─── Main scoring function ────────────────────────────────────────────────────


def score_node(
    node: Node,
    query_embedding: MaybeArray,  # 384-dim numpy array of the current query
    active_domains: set[str] = None,  # domains active in this conversation turn
    context_modifier: float = 1.0,  # from state stores: user/kai/relationship
) -> float:
    """
    Version C probabilistic score. Returns 0.0–1.0.

    score = P(still true) × P(correct) × P(relevant now) × boost × context_modifier

    The base is multiplicative — a near-zero on any core factor tanks the whole score.
    This is intentional: a highly relevant but uncertain fact should not rank high.
    """
    r = _recency(node)  # P(still true)
    c = node.confidence  # P(correct)
    v = _cosine(node.embedding, query_embedding)  # semantic similarity to query
    d = _domain_match(node, active_domains or set())  # domain alignment

    # Multiplicative base — all three gates must be reasonably open
    base = r * c * (v * d)

    # P(worth surfacing) = importance × specificity
    # Both must be high: an important but vague fact is less useful than a specific one
    worth = node.importance * node.specificity

    # Frequency boost: boost = worth + F × (1 - worth)
    # This is the key shape:
    #   — if worth is already 0.9, frequency barely moves it (0.9 + 0.1×F = 0.9–1.0)
    #   — if worth is 0.3, high frequency can pull it up to 0.3 + 0.7×1.0 = 1.0
    # Frequency rescues undervalued nodes. It cannot manufacture a high score from nothing.
    f = _norm_frequency(node)  # 0.0–1.0
    boost = worth + f * (1.0 - worth)  # always in [worth, 1.0]

    # Context modifier comes from the three state stores and is applied last.
    # It's a scalar that compresses or expands scores based on relationship depth,
    # session emotional read, and trust trajectory.
    return base * boost * context_modifier


# ─── Batch ranking ────────────────────────────────────────────────────────────


def rank_nodes(
    nodes: list[Node],
    query_embedding: MaybeArray,
    active_domains: set[str] = None,
    context_modifier: float = 1.0,
    top_k: int = 12,  # how many nodes to return in the context block
    min_score: float = 0.05,  # nodes below this threshold are excluded
) -> list[tuple[Node, float]]:
    """
    Score a list of nodes and return the top-k above the minimum threshold.
    Returns (node, score) pairs sorted from highest to lowest score.
    """
    scored = []  # will hold (node, score) tuples
    for node in nodes:  # score every node in the candidate list
        s = score_node(node, query_embedding, active_domains, context_modifier)
        if s >= min_score:  # skip nodes that scored too low to be useful
            scored.append((node, s))  # add the (node, score) pair to results

    # Sort descending by score — highest relevance first
    # key=lambda x: x[1] means "sort by the second element of each tuple" (the score)
    # reverse=True means highest first instead of lowest first
    scored.sort(key=lambda x: x[1], reverse=True)

    return scored[:top_k]  # slice: return only the top_k entries


def explain_score(
    node: Node,
    query_embedding: MaybeArray,
    active_domains: set[str] = None,
    context_modifier: float = 1.0,
) -> dict:
    """
    Return a breakdown of every factor in the score. Useful for debugging and
    for the memory model to understand why a node ranked where it did.
    """
    r = _recency(node)
    c = node.confidence
    v = _cosine(node.embedding, query_embedding)
    d = _domain_match(node, active_domains or set())
    base = r * c * (v * d)

    worth = node.importance * node.specificity
    f = _norm_frequency(node)
    boost = worth + f * (1.0 - worth)

    final = base * boost * context_modifier

    return {
        "path": node.path,
        "final_score": round(final, 4),
        "factors": {
            "recency (P still true)": round(r, 4),  # how old is this?
            "confidence (P correct)": round(c, 4),  # how sure are we?
            "similarity (P relevant)": round(v, 4),  # does it match the query?
            "domain_match": round(d, 4),  # right topic?
            "base (r×c×v×d)": round(base, 4),  # multiplicative gate
            "worth (I×Sp)": round(worth, 4),  # importance × specificity
            "frequency_norm": round(f, 4),  # normalized hit count
            "boost": round(boost, 4),  # final boost value
            "context_modifier": round(context_modifier, 4),
        },
    }


# ─── Context selection — hardcoded + ranked, the actual retrieval entry point ─


def select_for_context(
    user_id: str,
    query_embedding: MaybeArray,
    active_domains: set[str] = None,
    context_modifier: float = 1.0,
    total_limit: int = 12,  # how many nodes total go into the context block
) -> list[tuple[Node, "float | None"]]:
    """
    The real retrieval entry point — combines hardcoded and scored nodes correctly.

    Hardcoded nodes (medical, hardware, profession, critical) bypass the equation
    entirely and always appear first, with score=None to mark them as unscored.
    The remaining slots are filled by ranked nodes from the rest of the tree.

    This is the function the memory model calls — never call rank_nodes() directly
    on a full node list, or hardcoded facts can fall below threshold and vanish.
    """
    # Seed nodes are folder scaffolding, not facts — never surface them.
    # Critically: several seeded folders sit on hardcoded paths (user/health
    # etc.) and would otherwise bypass scoring and appear in EVERY context.
    hardcoded = [
        n for n in _tree.hardcoded_nodes(user_id) if n.source != "seed"
    ]  # always-surface nodes, unscored
    hardcoded_paths = {n.path for n in hardcoded}  # set for fast membership checks below

    # Pull every other node and exclude the ones we already have
    rest = [
        n for n in _tree.all_nodes(user_id) if n.path not in hardcoded_paths and n.source != "seed"
    ]

    # Only as many ranked slots remain as the limit allows after hardcoded nodes
    remaining_slots = max(0, total_limit - len(hardcoded))
    ranked = rank_nodes(
        rest, query_embedding, active_domains, context_modifier, top_k=remaining_slots
    )

    # Hardcoded nodes carry score=None — the context builder renders them as "[permanent]"
    # rather than with a numeric score, since the equation never touched them
    result: list[tuple[Node, float | None]] = [(n, None) for n in hardcoded]
    result.extend(ranked)  # ranked entries are already (node, score) tuples
    return result
