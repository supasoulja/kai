"""
kai/memory/intuition.py — The intuition flag. A process interrupt, not a score.

Five detectors run every turn. Each one answers a different question about
*fit* between what's being said now and what's already known. When one trips,
it produces a flag that sits outside the scoring equation and can override it
entirely — the memory model saying "the equation didn't catch this, but I did."

Three of the five (contradiction, pattern_break, emotional_incongruence) need
a semantic read of the live conversation — something only the memory model's
language understanding can supply. Python can't tell that "I love mornings now"
contradicts "I'm not a morning person" — that takes meaning, not string matching.
So those three take a pre-judged signal (which node, how strong the conflict)
and turn it into a properly-calibrated flag.

The other two (accumulation, escalation_approach) run on stored data alone —
no semantic judgment required, just arithmetic over what's already in the tree.
"""

import time
from dataclasses import dataclass, field

from .scorer import MaybeArray, _cosine, _norm_frequency
from .state import UserState
from .tree import Node

# ─── Flag structure ───────────────────────────────────────────────────────────


@dataclass
class IntuitionFlag:
    """One flagged moment. Carries enough for the memory model to act on and
    enough for the chat model to understand why retrieval changed shape."""

    level: str  # "soft" | "hard" | "alert"
    detector: str  # which of the five raised it
    strength: float  # 0.0-1.0 — how confident the detector is
    nodes: list[str]  # paths of the nodes involved
    action: str  # "hold" | "ask" | "soften" | "escalate"
    reason: str  # plain-English why — surfaces in [FLAGS] block
    raised_at: float = field(default_factory=time.time)


# Severity order — used to pick the dominant flag when several trip at once.
_SEVERITY_RANK = {"soft": 1, "hard": 2, "alert": 3}


def highest_priority(flags: list[IntuitionFlag]) -> IntuitionFlag | None:
    """
    The equation can only be overridden by one flag at a time.
    Alert beats hard beats soft. Ties broken by raw strength.
    """
    if not flags:
        return None
    return max(flags, key=lambda f: (_SEVERITY_RANK[f.level], f.strength))


# ─── Shared calibration ───────────────────────────────────────────────────────
#
# Three of the five detectors share a shape: take an established weight (how
# solid is the thing being disrupted) and a disruption strength (how hard is
# it being disrupted), multiply them, and bucket the result into a level.
# A shaky belief getting mildly contradicted is noise. A rock-solid belief
# getting flatly contradicted is the kind of thing worth pausing for.


def _bucket(raw: float, *, soft_at: float = 0.25, hard_at: float = 0.55) -> str | None:
    """Turn a raw 0.0-1.0 disruption score into a level, or None if it's noise."""
    if raw >= hard_at:
        return "hard"
    if raw >= soft_at:
        return "soft"
    return None


# ─── Detector 1: Contradiction ────────────────────────────────────────────────


def detect_contradiction(node: Node, conflict_strength: float) -> IntuitionFlag | None:
    """
    Current statement conflicts with a high-confidence stored node.

    `conflict_strength` is the memory model's read of how directly opposed the
    new statement is to the stored value (0.0 = barely related, 1.0 = flat
    negation). Calibrated against the node's own confidence — contradicting
    something Kai was never sure about is a shrug, not a flag.
    """
    raw = node.confidence * conflict_strength
    level = _bucket(raw)
    if level is None:
        return None

    action = "soften" if level == "soft" else "ask"
    reason = (
        f"new statement may conflict with {node.path} "
        f'("{node.value}", confidence {node.confidence:.2f})'
    )
    return IntuitionFlag(level, "contradiction", round(raw, 3), [node.path], action, reason)


# ─── Detector 2: Pattern break ────────────────────────────────────────────────


def detect_pattern_break(node: Node, deviation_strength: float) -> IntuitionFlag | None:
    """
    A user who reliably does X is suddenly doing Y.

    `deviation_strength` is the memory model's read of how far the current
    behavior sits from the stored pattern. Weighted by how *established* that
    pattern actually is — frequency stands in for "how many times has this
    held true" the same way it does in the scoring equation.
    """
    established = node.confidence * _norm_frequency(node)
    raw = established * deviation_strength
    level = _bucket(raw)
    if level is None:
        return None

    action = "soften" if level == "soft" else "hold"
    reason = (
        f"behavior diverges from established pattern at {node.path} "
        f'("{node.value}", seen {node.frequency}x)'
    )
    return IntuitionFlag(level, "pattern_break", round(raw, 3), [node.path], action, reason)


# ─── Detector 3: Emotional incongruence ───────────────────────────────────────


def detect_emotional_incongruence(
    user_state: UserState,
    expected_register: str,
    magnitude: float,
) -> IntuitionFlag | None:
    """
    Session tone doesn't match the stored emotional baseline for this person.

    `expected_register` is what the relationship history would predict for a
    session like this one. `magnitude` is the memory model's read of how far
    the actual register has drifted from that expectation. This detector tops
    out at "hard" — a tone mismatch alone is a reason to check in, not a crisis.
    """
    if user_state.emotional_register == expected_register:
        return None

    raw = magnitude
    level = _bucket(raw, soft_at=0.3, hard_at=0.65)
    if level is None:
        return None

    action = "soften" if level == "soft" else "ask"
    reason = (
        f"tone reads as '{user_state.emotional_register}', "
        f"expected closer to '{expected_register}' for a session like this"
    )
    return IntuitionFlag(level, "emotional_incongruence", round(raw, 3), [], action, reason)


# ─── Detector 4: Accumulation ─────────────────────────────────────────────────


def detect_accumulation(
    signal_nodes: list[Node],
    threshold: float = 3.0,
) -> IntuitionFlag | None:
    """
    Individually weak signals that cross a threshold together.

    One stress mention is noise. Seven in two weeks is a pattern. This one
    runs entirely on stored data — no semantic read needed. Each node
    contributes confidence × how-often-confirmed; the sum is compared
    against the threshold.

    Soft at half the threshold (something is building — stay aware).
    Alert at the threshold (the pattern crossed the line — change mode).
    Never "hard" — accumulation either hasn't crossed yet or it has.

    Weight is a straight sum of confidence across the signal nodes — NOT
    scaled by frequency. Frequency answers "how solid is this one fact,"
    which is the wrong question here. What matters for accumulation is how
    many independent signals exist and how much each is trusted; a frequency
    term would punish exactly the case this detector exists to catch — many
    distinct, individually-unconfirmed observations adding up to a pattern.
    """
    if not signal_nodes:
        return None

    weight = sum(n.confidence for n in signal_nodes)

    if weight >= threshold:
        level = "alert"
        action = "escalate"
    elif weight >= threshold * 0.5:
        level = "soft"
        action = "soften"
    else:
        return None

    paths = [n.path for n in signal_nodes]
    reason = (
        f"{len(signal_nodes)} accumulating signals, weighted total "
        f"{weight:.2f} against threshold {threshold:.2f}"
    )
    strength = min(weight / threshold, 1.0)
    return IntuitionFlag(level, "accumulation", round(strength, 3), paths, action, reason)


# ─── Detector 5: Escalation approach ──────────────────────────────────────────


def detect_escalation_approach(
    topic_embedding: MaybeArray,
    sensitive_nodes: list[Node],
    soft_at: float = 0.5,
    hard_at: float = 0.75,
) -> IntuitionFlag | None:
    """
    The conversation is heading toward a topic with a sensitive stored node —
    flag it before arrival, not after. Pure cosine similarity between where
    the conversation is now and where the sensitive ground sits. Like
    accumulation, this needs no semantic judgment call — just distance.
    """
    if topic_embedding is None or not sensitive_nodes:
        return None

    best_node = None
    best_sim = -1.0
    for node in sensitive_nodes:
        sim = _cosine(topic_embedding, node.embedding)
        if sim > best_sim:
            best_sim, best_node = sim, node

    if best_sim < soft_at:
        return None

    level = "hard" if best_sim >= hard_at else "soft"
    action = "soften" if level == "soft" else "ask"
    reason = (
        f"conversation drifting toward sensitive ground at {best_node.path} "
        f"(similarity {best_sim:.2f})"
    )
    return IntuitionFlag(
        level, "escalation_approach", round(best_sim, 3), [best_node.path], action, reason
    )


# ─── Orchestration ────────────────────────────────────────────────────────────


def run_detectors(
    *,
    contradiction: tuple[Node, float] | None = None,
    pattern_break: tuple[Node, float] | None = None,
    emotional: tuple[UserState, str, float] | None = None,
    accumulation_nodes: list[Node] | None = None,
    accumulation_threshold: float = 3.0,
    topic_embedding: MaybeArray = None,
    sensitive_nodes: list[Node] | None = None,
) -> list[IntuitionFlag]:
    """
    Run every detector the caller has signal for. Semantic detectors
    (contradiction, pattern_break, emotional) are skipped if the memory model
    didn't supply a candidate — there's nothing to calibrate without a read on
    the conversation. Data-only detectors (accumulation, escalation_approach)
    run whenever the relevant nodes are passed in.

    Returns every flag that tripped, not just the dominant one — the memory
    model may want to log all of them even though only the highest-priority
    flag actually changes how retrieval behaves this turn.
    """
    flags: list[IntuitionFlag] = []

    if contradiction is not None:
        flag = detect_contradiction(*contradiction)
        if flag:
            flags.append(flag)

    if pattern_break is not None:
        flag = detect_pattern_break(*pattern_break)
        if flag:
            flags.append(flag)

    if emotional is not None:
        flag = detect_emotional_incongruence(*emotional)
        if flag:
            flags.append(flag)

    if accumulation_nodes is not None:
        flag = detect_accumulation(accumulation_nodes, accumulation_threshold)
        if flag:
            flags.append(flag)

    if sensitive_nodes is not None:
        flag = detect_escalation_approach(topic_embedding, sensitive_nodes)
        if flag:
            flags.append(flag)

    return flags


# ─── Output formatting ────────────────────────────────────────────────────────


def format_flags(flags: list[IntuitionFlag]) -> str:
    """
    Render the [FLAGS] section of the memory model's output block.
    Only the dominant flag is shown — that's the one that actually changes
    how this turn behaves. Everything else was logged, not surfaced.
    """
    dominant = highest_priority(flags)
    if dominant is None:
        return "none"
    return f"[{dominant.level.upper()}] {dominant.detector}: {dominant.reason} → {dominant.action}"
