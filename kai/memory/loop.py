"""
kai/memory/loop.py — The memory model loop. Prototype.

Ties tree + scorer + state + intuition into the one thing the chat model
actually needs each turn: a rendered context block that already knows who
it's talking to. This is the retrieval half of the parallel architecture
in BRAIN_DESIGN.md — gather → rank → flag → render.

The write half — extracting new facts from the live conversation into tree
nodes — needs its own model and is deliberately deferred (see Open Questions
in BRAIN_DESIGN). Until that exists, the chat loop calls the small, explicit
update hooks at the bottom of this file after a turn completes: the cheap,
unambiguous signals that don't need semantic judgment to record.
"""

from typing import Optional

from . import tree as _tree
from . import state as _state
from . import scorer
from . import intuition
from .tree import Node
from .scorer import MaybeArray
from .intuition import IntuitionFlag


# ─── Plain-language renderings ────────────────────────────────────────────────
# The block speaks in words ("trust: stable", "depth: high"), not raw floats —
# the chat model reasons better from language it already understands than from
# a number it has to interpret mid-conversation.

def _depth_word(depth: float) -> str:
    if depth >= 0.7:
        return "high"
    if depth >= 0.3:
        return "moderate"
    return "low"


def _confidence_word(confidence: float) -> str:
    if confidence >= 0.75:
        return "high"
    if confidence >= 0.45:
        return "moderate"
    return "low"


def _node_line(index: int, node: Node, score: Optional[float]) -> str:
    """
    One [NODES] line. Permanent nodes carry no score — they bypassed the
    equation entirely, and showing a number would imply they didn't.
    Scored nodes carry confidence and source: the chat model needs both
    to pick its phrasing ("you mentioned" vs "I think" vs flat assertion).
    """
    tag = "[permanent]" if score is None else f"[conf:{node.confidence:.1f}, {node.source}]"
    return f"{index}. {node.path}: \"{node.value}\" {tag}"


# ─── Render ───────────────────────────────────────────────────────────────────

def render_memory_block(
    relationship: _state.RelationshipState,
    user_state: _state.UserState,
    kai_state: _state.KaiState,
    ranked: list[tuple[Node, Optional[float]]],
    flags: list[IntuitionFlag],
) -> str:
    """
    The [MEMORY CONTEXT] block exactly as specified in BRAIN_DESIGN —
    relationship/state summary first, ranked nodes second, flags last.
    Target 200-400 tokens. Injected before the system prompt.
    """
    lines = [
        "[MEMORY CONTEXT]",
        f"relationship: {relationship.session_count} sessions | "
        f"trust: {relationship.trust_trajectory} | "
        f"depth: {_depth_word(relationship.relationship_depth)}",
        f"user_state: {user_state.emotional_register}, {user_state.session_intent}",
        f"kai_confidence: {_confidence_word(kai_state.self_confidence)}",
        "",
        "[NODES — ranked by score]",
    ]
    for i, (node, score) in enumerate(ranked, start=1):
        lines.append(_node_line(i, node, score))

    lines.append("")
    lines.append("[FLAGS]")
    lines.append(intuition.format_flags(flags))

    return "\n".join(lines)


# ─── Gather — the loop's entry point ──────────────────────────────────────────

def gather_context(
    user_id: str,
    query_embedding: MaybeArray,
    active_domains: Optional[set[str]] = None,
    total_limit: int = 12,
    detector_signals: Optional[dict] = None,
) -> tuple[str, list[IntuitionFlag]]:
    """
    One full pass of the memory model loop:
      1. load the three state stores
      2. compute the context modifier that bridges them into the equation
      3. select nodes — hardcoded first, ranked filling the rest
      4. run whatever intuition detectors have signal this turn
      5. render the block the chat model receives

    `detector_signals` carries whatever the memory model's semantic read
    produced this turn — contradiction candidates, pattern-break candidates,
    an emotional-baseline comparison (see intuition.run_detectors for the
    expected shape). Pass None on turns where nothing stood out; the
    data-only detectors (accumulation, escalation_approach) still run as
    long as the tree has the relevant nodes.

    Returns the rendered block plus the raw flag list — the chat loop injects
    the block, the memory model logs every flag regardless of which one won.
    """
    relationship = _state.load_relationship_state(user_id)
    user_state = _state.load_user_state(user_id)
    kai_state = _state.load_kai_state(user_id)

    # Reuse the states just loaded — compute_context_modifier would otherwise
    # re-read relationship + kai from disk a second time this turn.
    context_modifier = _state.compute_context_modifier(
        user_id, rel=relationship, kai=kai_state
    )

    ranked = scorer.select_for_context(
        user_id, query_embedding,
        active_domains=active_domains,
        context_modifier=context_modifier,
        total_limit=total_limit,
    )

    flags = _run_detectors_for_turn(user_id, query_embedding, detector_signals or {})

    block = render_memory_block(relationship, user_state, kai_state, ranked, flags)
    return block, flags


def _run_detectors_for_turn(
    user_id: str,
    query_embedding: MaybeArray,
    signals: dict,
) -> list[IntuitionFlag]:
    """
    Wire this turn's semantic signals together with the data-only detectors
    that run on the tree alone. Lives here, not in intuition.py, because it's
    the loop's job to know *where in the tree* to look — intuition.py only
    knows how to *judge* what it's handed.
    """
    accumulation_nodes = None
    if "accumulation_domain" in signals:
        accumulation_nodes = [
            n for n in _tree.domain_nodes(user_id, signals["accumulation_domain"])
            if n.source == "pattern"
        ]

    sensitive_nodes = None
    if signals.get("check_escalation", True):
        sensitive_nodes = [n for n in _tree.hardcoded_nodes(user_id) if n.embedding is not None]

    return intuition.run_detectors(
        contradiction=signals.get("contradiction"),
        pattern_break=signals.get("pattern_break"),
        emotional=signals.get("emotional"),
        accumulation_nodes=accumulation_nodes,
        accumulation_threshold=signals.get("accumulation_threshold", 3.0),
        topic_embedding=query_embedding,
        sensitive_nodes=sensitive_nodes,
    )


# ─── Post-turn hooks — small, explicit, called by the chat loop ──────────────
#
# The write half of the parallel architecture (turning a live conversation
# into new tree nodes) needs its own model — deferred per BRAIN_DESIGN's open
# questions. These hooks record the cheap, unambiguous signals that don't
# need semantic judgment at all: a node got used, the user confirmed a read,
# the user corrected one. Real learning starts here even before that model exists.

def record_node_used(user_id: str, path: str) -> None:
    """A surfaced node held up — the user engaged with it, didn't correct it."""
    _tree.increment_frequency(user_id, path)


def record_turn_validation(user_id: str) -> None:
    """User confirmed Kai's read was right this turn."""
    _state.record_validation(user_id)


def record_turn_correction(user_id: str, domain: str = "") -> None:
    """User corrected Kai's read this turn."""
    _state.record_correction(user_id, domain)
