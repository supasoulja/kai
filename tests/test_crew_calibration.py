"""
Calibration / drift guard for the crew's CATEGORY_FLOOR routing threshold.

`crew.CATEGORY_FLOOR` is a single cosine number that decides, per turn, which
domains count as "matched" — and therefore whether a query takes the fast
single-specialist lane, escalates to BOSS orchestration, or falls through to
chat. That number is only meaningful *relative to the live embedding model's
score distribution*. The live triage path embeds with the CPU model
`kai.llm.embed` (bge-small-en-v1.5) — NOT the shutdown-only qwen3-embedding — so
the floor must be calibrated to bge-small, and a model swap (or a bad manual
retune) silently breaks routing: too high and real tool requests fall to chat
with no tools; too low and greetings hit full BOSS orchestration.

These tests embed a fixed query set with the *real* live embedder and assert the
separation properties routing relies on. They are a tripwire, not a unit test of
the logic (that's test_crew.py, which feeds synthetic scores). When the embedding
model changes, this file fails loudly with the new distribution printed, telling
you to recalibrate CATEGORY_FLOOR rather than shipping silently-broken routing.

Skipped when the live embedder isn't available (model not downloaded / no
onnxruntime), so it never fails spuriously in a bare CI.
"""

from __future__ import annotations

import pytest

from kai.core import crew
from kai.tools.registry import ToolRegistry

# ── Fixed calibration corpus ─────────────────────────────────────────────────
# Chosen to be unambiguous exemplars of each routing outcome. Keep these stable:
# their whole value is as a fixed yardstick the live model is measured against.

# Single-domain requests that MUST resolve to exactly one specialist (FAST lane).
# Each is a clean, single-intent phrasing — the runner-up categories stay below
# the floor for these, so they are FAST-eligible.
SINGLE_DOMAIN: list[tuple[str, str]] = [
    ("how much disk space do I have", "Dewey"),
    ("what is my cpu and memory usage", "Gus"),
    ("search the web for the latest gemma release", "Scout"),
]

# Genuinely multi-domain requests that MUST spread to >=2 specialists (BOSS lane),
# with the named pair among the matched set (the coverage set Otto must cover).
COMPOUND: list[tuple[str, set[str]]] = [
    ("check my disk space and what containers are running", {"Dewey", "Cargo"}),
    ("show cpu usage and search the web for a fix", {"Gus", "Scout"}),
]

# Small talk / greetings that MUST NOT clear the floor on any domain, so the fuzzy
# tool axis can't drag them into BOSS ("hey there" -> 21s orchestration was the bug
# dbb2e2f fixed). These are the fuzzy-axis-only turns triage sends to chat.
SMALL_TALK: list[str] = [
    "hey there how are you",
    "thanks that was super helpful",
    "what do you think about jazz music",
]


@pytest.fixture(scope="module")
def index():
    """Build the category index once with the LIVE embedder (the same
    kai.llm.embed path triage uses). Skip the whole module if it's unavailable."""
    try:
        from kai.llm.embed import embed_batch
    except Exception as e:  # pragma: no cover - import-time env gap
        pytest.skip(f"live embedder import failed: {e}")
    reg = ToolRegistry()
    try:
        idx = reg.build_category_index(embed_batch)
    except Exception as e:  # model not downloaded / onnxruntime missing / no net
        pytest.skip(f"live embedder unavailable (no model/onnx): {e}")
    if not idx:
        pytest.skip("category index came back empty")
    return reg, idx


def _rank(index, query: str) -> list[tuple[str, float]]:
    from kai.llm.embed import embed_batch

    emb = embed_batch([query])[0]
    reg, idx = index
    return reg.rank_categories(emb, idx, top_k=4)


def _fmt(scores: list[tuple[str, float]]) -> str:
    return "  ".join(f"{c}={s:.3f}" for c, s in scores)


# ── The separation the floor must preserve ───────────────────────────────────


@pytest.mark.parametrize("query", SMALL_TALK)
def test_small_talk_stays_below_floor(index, query):
    """Small talk must not clear CATEGORY_FLOOR on any domain — otherwise the
    fuzzy tool axis routes greetings to BOSS (the pre-dbb2e2f regression)."""
    scores = _rank(index, query)
    top = scores[0][1] if scores else 0.0
    assert top < crew.CATEGORY_FLOOR, (
        f"small talk cleared the floor ({top:.3f} >= {crew.CATEGORY_FLOOR}); "
        f"greetings will route to BOSS. Recalibrate CATEGORY_FLOOR.\n  {_fmt(scores)}"
    )
    assert crew._specialists_for(scores) == [], (
        f"small talk matched specialists {crew._specialists_for(scores)}; "
        f"expected none.\n  {_fmt(scores)}"
    )


@pytest.mark.parametrize("query,expected", SINGLE_DOMAIN)
def test_single_domain_takes_fast_lane(index, query, expected):
    """A clean single-domain query must resolve to exactly one specialist (the
    right one) so triage takes the FAST lane instead of paying for BOSS."""
    scores = _rank(index, query)
    specialists = crew._specialists_for(scores)
    assert specialists == [expected], (
        f"single-domain query did not resolve to the FAST lane: expected "
        f"[{expected!r}], got {specialists}.\n  {_fmt(scores)}"
    )
    assert scores[0][1] >= crew.CATEGORY_FLOOR, (
        f"top score {scores[0][1]:.3f} below floor {crew.CATEGORY_FLOOR} — the "
        f"specialist wouldn't be matched at all.\n  {_fmt(scores)}"
    )


@pytest.mark.parametrize("query,expected_pair", COMPOUND)
def test_compound_spreads_to_boss(index, query, expected_pair):
    """A multi-domain request must spread to >=2 specialists (BOSS lane) with the
    expected pair among them — the coverage set Otto is required to fully cover."""
    scores = _rank(index, query)
    specialists = set(crew._specialists_for(scores))
    assert len(specialists) >= 2, (
        f"compound query did not spread: matched {specialists} (<2), would take "
        f"FAST/chat instead of BOSS.\n  {_fmt(scores)}"
    )
    assert expected_pair <= specialists, (
        f"compound query missed part of the ask: expected {expected_pair} "
        f"⊆ matched, got {specialists}.\n  {_fmt(scores)}"
    )


def test_floor_brackets_chat_and_tool_turns(index):
    """The headline drift invariant: CATEGORY_FLOOR must sit strictly between the
    highest small-talk score and the lowest single-domain top score. If the
    embedding model's distribution shifts under the floor, this bracket collapses
    and routing breaks in one direction or the other — fail here with both bounds
    printed so the fix is 'move the floor into the new gap'."""
    small_talk_max = max((_rank(index, q)[0][1] for q in SMALL_TALK), default=0.0)
    single_domain_min_top = min(_rank(index, q)[0][1] for q, _ in SINGLE_DOMAIN)
    assert small_talk_max < crew.CATEGORY_FLOOR <= single_domain_min_top, (
        "CATEGORY_FLOOR no longer separates chat turns from tool turns on the "
        "live embedding model — recalibrate it into the gap.\n"
        f"  small-talk max top   = {small_talk_max:.3f}  (must be < floor)\n"
        f"  CATEGORY_FLOOR       = {crew.CATEGORY_FLOOR}\n"
        f"  single-domain min top= {single_domain_min_top:.3f}  (must be >= floor)"
    )
