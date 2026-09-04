"""
Calibration / drift guard for the crew's domain-selection thresholds.

Three numbers in `kai/core/crew.py` decide, per turn, which domains count as
"matched" — and therefore whether a query takes the fast single-specialist lane,
escalates to BOSS orchestration, or falls through to chat:

    CATEGORY_FLOOR       absolute cosine floor for a match
    CATEGORY_DELTA       relative admission, expand-only (added 2026-09-04)
    BOSS_CATEGORY_FLOOR  the lower floor, BOSS branch only (added 2026-09-04)

All three are only meaningful *relative to the live embedding model's score
distribution*. The live triage path embeds with the CPU model `kai.llm.embed`
(bge-small-en-v1.5) — NOT the shutdown-only qwen3-embedding — so they are
calibrated to bge-small, and a model swap (or a bad manual retune) silently
breaks routing: too high and real tool requests fall to chat with no tools; too
low and greetings hit full BOSS orchestration.

These tests embed a fixed query set with the *real* live embedder and assert the
separation properties routing relies on. They are a tripwire, not a unit test of
the logic (that's test_crew.py, which feeds synthetic scores). When the embedding
model changes, this file fails loudly with the new distribution printed.

**Why all three are guarded.** Before 2026-09-04 only CATEGORY_FLOOR existed and
only it was locked. The floor alone covered 4 of 20 cross-domain tasks; the delta
rule and the BOSS floor are what carry coverage to 17/20, which makes them
load-bearing and means they need the same drift protection the floor has.
(CREW_TRIAGE_DECISION.md, Decision 1 = A.)

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
# The compound/relative and nothing-clears sets are real prompts lifted from the
# 20-task cross-domain corpus (kaibench/kai-crew-tasks.py), so a drift here is a
# drift in the thing that was actually measured.

# Single-domain requests whose OWN domain must rank first and clear the floor.
# Note they are no longer required to match exactly one specialist: the delta rule
# deliberately admits a near-miss runner-up, at a measured cost of 0.71 extra
# specialists per single-domain turn. What must not drift is which domain wins.
SINGLE_DOMAIN: list[tuple[str, str]] = [
    ("how much disk space do I have", "Dewey"),
    ("what is my cpu and memory usage", "Gus"),
    ("search the web for the latest gemma release", "Scout"),
]

# Compound requests where BOTH domains clear the absolute floor on their own.
# These passed before the delta rule existed and must keep passing without it.
COMPOUND_ABSOLUTE: list[tuple[str, set[str]]] = [
    ("check my disk space and my cpu temperature", {"Dewey", "Gus"}),
    ("show cpu usage and search the web for a fix", {"Gus", "Scout"}),
]

# THE CASE THAT HAD NO COVERAGE. Compound asks whose second domain lands in the
# 0.49–0.58 band — just under the floor — so the absolute floor alone drops half
# the question. The delta rule exists for exactly these, and it is the only thing
# admitting the second specialist here.
COMPOUND_RELATIVE: list[tuple[str, str, str]] = [
    # query, domain the floor finds, domain only the delta rule finds
    (
        "what is using the most cpu right now, and have I written anything "
        "about that program before?",
        "Gus",
        "Remy",
    ),
    (
        "how much space would I get back if I cleared the largest cache "
        "directory, and would that be enough to get root under 85%?",
        "Dewey",
        "Gus",
    ),
]

# Tool asks where NOTHING clears the absolute floor. Expand-only cannot help them
# by construction — there is no first match to expand from — so the BOSS floor is
# the only thing that produces a coverage set. 9 of the 20 corpus tasks are here.
NOTHING_CLEARS: list[tuple[str, set[str]]] = [
    (
        "is my disk actually under pressure, and does what I wrote down about it still hold?",
        {"Gus", "Remy"},
    ),
    ("do I have a backup of my home directory anywhere?", {"Dewey", "Gus"}),
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
    # Rank exactly as the live path does — a category outside TRIAGE_TOP_K can
    # never be admitted by any floor, so the tripwire must see the same window.
    return reg.rank_categories(emb, idx, top_k=crew.TRIAGE_TOP_K)


def _fmt(scores: list[tuple[str, float]]) -> str:
    return "  ".join(f"{c}={s:.3f}" for c, s in scores)


# ── The separation the floor must preserve ───────────────────────────────────


@pytest.mark.parametrize("query", SMALL_TALK)
def test_small_talk_stays_below_floor(index, query):
    """Small talk must not clear CATEGORY_FLOOR on any domain — otherwise the
    fuzzy tool axis routes greetings to BOSS (the pre-dbb2e2f regression).

    This also pins the expand-only guarantee: with nothing above the floor there
    is no anchor, so the delta rule stays shut and cannot invent a first match."""
    scores = _rank(index, query)
    top = scores[0][1] if scores else 0.0
    assert top < crew.CATEGORY_FLOOR, (
        f"small talk cleared the floor ({top:.3f} >= {crew.CATEGORY_FLOOR}); "
        f"greetings will route to BOSS. Recalibrate CATEGORY_FLOOR.\n  {_fmt(scores)}"
    )
    assert crew._specialists_for(scores) == [], (
        f"small talk matched specialists {crew._specialists_for(scores)}; expected "
        f"none. If this fires while the assert above passed, the delta rule has "
        f"stopped being expand-only.\n  {_fmt(scores)}"
    )


@pytest.mark.parametrize("query,expected", SINGLE_DOMAIN)
def test_single_domain_ranks_its_own_domain_first(index, query, expected):
    """A clean single-domain query must put its OWN domain first, above the floor.

    It is no longer required to match exactly one specialist — the delta rule
    admits a near-miss runner-up on purpose (0.71 extra specialists per
    single-domain turn, the measured price of 4/20 -> 17/20 coverage). What must
    not drift is which domain wins."""
    scores = _rank(index, query)
    specialists = crew._specialists_for(scores)
    assert specialists and specialists[0] == expected, (
        f"single-domain query no longer ranks its own domain first: expected "
        f"{expected!r} at the front, got {specialists}.\n  {_fmt(scores)}"
    )
    assert scores[0][1] >= crew.CATEGORY_FLOOR, (
        f"top score {scores[0][1]:.3f} below floor {crew.CATEGORY_FLOOR} — the "
        f"specialist wouldn't be matched at all.\n  {_fmt(scores)}"
    )


@pytest.mark.parametrize("query,expected_pair", COMPOUND_ABSOLUTE)
def test_compound_spreads_on_the_absolute_floor(index, query, expected_pair):
    """A multi-domain request whose domains both clear the floor must spread to
    >=2 specialists WITHOUT needing the delta rule — it worked before the rule
    existed and must not come to depend on it."""
    scores = _rank(index, query)
    floor_only = set(crew._specialists_for(scores, delta=None))
    assert expected_pair <= floor_only, (
        f"compound query no longer spreads on the absolute floor alone: expected "
        f"{expected_pair} ⊆ {floor_only}. The delta rule is now load-bearing for a "
        f"case that used to clear on its own.\n  {_fmt(scores)}"
    )


@pytest.mark.parametrize("query,by_floor,by_delta", COMPOUND_RELATIVE)
def test_delta_rule_admits_the_second_domain(index, query, by_floor, by_delta):
    """The delta tripwire. On a compound ask whose second domain sits just under
    the floor, the relative rule must admit it — and the absolute floor alone must
    NOT. Both halves matter: the first is the coverage the rule buys, the second
    proves the fixture still exercises the rule rather than passing for free."""
    scores = _rank(index, query)
    floor_only = crew._specialists_for(scores, delta=None)
    with_delta = crew._specialists_for(scores)
    assert by_delta not in floor_only, (
        f"fixture no longer exercises the delta rule: {by_delta!r} now clears the "
        f"absolute floor by itself, so this test would pass with the rule deleted. "
        f"Pick a new fixture in the 0.49–0.58 band.\n  {_fmt(scores)}"
    )
    assert by_floor in with_delta and by_delta in with_delta, (
        f"delta rule stopped admitting the second domain: expected both "
        f"{by_floor!r} and {by_delta!r} in {with_delta}. Coverage regresses to the "
        f"4/20 baseline if this drifts — retune CATEGORY_DELTA "
        f"(currently {crew.CATEGORY_DELTA}).\n  {_fmt(scores)}"
    )


@pytest.mark.parametrize("query,expected_pair", NOTHING_CLEARS)
def test_boss_floor_covers_what_nothing_clears(index, query, expected_pair):
    """The BOSS-floor tripwire. These asks clear no category at 0.60, so the
    expand-only delta rule cannot reach them. The lower BOSS floor must still
    produce a coverage set containing what the task needs — without it these turns
    reach Otto with matched=() and he may FINISH having dispatched nobody."""
    scores = _rank(index, query)
    assert crew._specialists_for(scores) == [], (
        f"fixture no longer exercises the BOSS floor: something now clears "
        f"CATEGORY_FLOOR, so this would pass with the BOSS floor deleted. Pick a "
        f"new fixture.\n  {_fmt(scores)}"
    )
    boss = set(crew._specialists_for(scores, floor=crew.BOSS_CATEGORY_FLOOR))
    assert expected_pair <= boss, (
        f"BOSS floor no longer covers this ask: expected {expected_pair} ⊆ {boss}. "
        f"Retune BOSS_CATEGORY_FLOOR (currently {crew.BOSS_CATEGORY_FLOOR}).\n"
        f"  {_fmt(scores)}"
    )


@pytest.mark.parametrize("query", SMALL_TALK)
def test_boss_floor_is_unreachable_from_a_chat_turn(index, query):
    """The safety invariant the BOSS floor depends on.

    At 0.50 small talk DOES match specialists — 'hey there how are you' matches
    three. That is only safe because the lower floor is applied inside the BOSS
    branch, which a fuzzy-axis-only turn never reaches: triage sends it to chat
    first. If that guard is ever removed, greetings run tool turns again.

    So this asserts the routing outcome, not the score: a non-keyword-gated turn
    must land in chat with tools off, whatever the BOSS floor would have matched."""
    scores = _rank(index, query)
    would_match = crew._specialists_for(scores, floor=crew.BOSS_CATEGORY_FLOOR)
    r = crew.triage(
        tools_open=True,  # the fuzzy tool axis fired
        needs_think=False,
        category_scores=scores,
        keyword_gated=False,  # ...but no TRUSTED gate did
    )
    assert r.lane == "chat" and not r.tools, (
        f"a fuzzy-axis-only turn reached the {r.lane!r} lane with tools={r.tools}. "
        f"The BOSS floor would have matched {would_match} for this greeting — the "
        f"keyword_gated guard is the only thing keeping it out.\n  {_fmt(scores)}"
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


def test_boss_floor_sits_between_small_talk_and_the_relative_band(index):
    """The BOSS floor's own bracket. It has to sit low enough to catch the
    nothing-clears class and high enough that it is not simply matching noise.

    The lower bound is deliberately NOT small talk — at 0.50 greetings do match,
    and `test_boss_floor_is_unreachable_from_a_chat_turn` is what makes that safe.
    The bound that matters is that the floor stays below the class it exists for."""
    nothing_clears_min_top = min(_rank(index, q)[0][1] for q, _ in NOTHING_CLEARS)
    assert crew.BOSS_CATEGORY_FLOOR <= nothing_clears_min_top, (
        "BOSS_CATEGORY_FLOOR has drifted above the class it exists to cover — "
        "those turns will reach Otto with an empty coverage set again.\n"
        f"  BOSS_CATEGORY_FLOOR      = {crew.BOSS_CATEGORY_FLOOR}\n"
        f"  nothing-clears min top   = {nothing_clears_min_top:.3f}  (must be >= it)"
    )
    assert crew.BOSS_CATEGORY_FLOOR < crew.CATEGORY_FLOOR, (
        f"BOSS_CATEGORY_FLOOR ({crew.BOSS_CATEGORY_FLOOR}) must stay below "
        f"CATEGORY_FLOOR ({crew.CATEGORY_FLOOR}) — above it, it does nothing."
    )
