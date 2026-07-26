"""
Detects and saves facts automatically from conversation text.

Two extraction modes:
  extract_and_save(text)              — user messages → stable preferences, identity facts
  extract_stable_observations(text)  — AI/tool responses → stable hardware profile (RAM total)
  extract_volatile_observations(text) — AI/tool responses → runtime stats (CPU%, temps, etc.)
                                        returned as dict, NOT saved to DB — session cache only
"""

import re

from kai.memory import semantic
from kai.memory import tree as _tree

# ── User message patterns ──────────────────────────────────────────────────────
# Extracted from things the USER says. (pattern, key_name, capture_group)

# (pattern, key_name, capture_group, confidence). Confidence is the trust we put
# in the capture — loose, greedy patterns score low so a casual phrase can never
# overwrite a confident singleton fact (see _singleton_should_write).
_USER_PATTERNS: list[tuple[re.Pattern, str, int, float]] = [
    # "my name is X" / "call me X" — explicit self-statement, full trust
    (re.compile(r"\b(?:my name is|call me)\s+([A-Za-z]+)", re.I), "user_name", 1, 1.0),
    # "I prefer X" / "I like X" / "I love X" / "I hate X"
    (
        re.compile(r"\bI (?:prefer|like|love|hate|dislike)\s+(.+?)(?:\s*[.,!?]|$)", re.I),
        "preference",
        1,
        0.7,
    ),
    # "from now on, X" — explicit directive, full trust
    (re.compile(r"\bfrom now on[,\s]+(.+?)(?:\s*[.,!?]|$)", re.I), "instruction", 1, 1.0),
    # "remember that X" / "remember X" — anchored to the start of the message
    # (optionally after a short greeting) so conversational mentions of
    # "remember" ("I don't know if you'll remember any of this tomorrow",
    # "so you can remember your whole convo with Claude") don't get captured
    # as if they were save-this-note commands. Explicit save command, full trust.
    (
        re.compile(
            r"^\s*(?:hey[,!]?\s*)?(?:please\s+)?remember (?:that\s+)?(.+?)(?:\s*[.,!?]|$)", re.I
        ),
        "note",
        1,
        1.0,
    ),
    # "I'm a/an X" / "I am a/an X" — greedy; catches transient states
    # ("I'm a bit tired" → "bit tired"), so low trust + state-word filtering.
    (re.compile(r"\bI(?:'m| am) an?\s+([A-Za-z ]+?)(?:\s*[.,!?]|$)", re.I), "user_role", 1, 0.5),
    # "I use X" (tools, languages, hardware)
    (re.compile(r"\bI use\s+([A-Za-z0-9_+# ]+?)(?:\s*[.,!?]|$)", re.I), "uses", 1, 0.6),
    # "I'm based in X" / "I live in X"
    (
        re.compile(r"\bI(?:'m| am) (?:based |located )?in\s+([A-Za-z ,]+?)(?:\s*[.,!?]|$)", re.I),
        "location",
        1,
        0.7,
    ),
    # "I play X" / "I game on X"
    (
        re.compile(r"\bI (?:play|mainly play|mostly play)\s+(.+?)(?:\s*[.,!?]|$)", re.I),
        "gaming",
        1,
        0.6,
    ),
]

# Keys where only one value makes sense — overwrite rather than append _1, _2
_SINGLETON_KEYS = {"user_name", "user_role", "location"}

# A singleton capture below this confidence may SET the fact when none exists,
# but may never OVERWRITE an existing one. This is what stops casual phrasing
# ("I'm a bit tired", confidence 0.5) from clobbering user_role=developer.
_SINGLETON_OVERWRITE_MIN = 0.7

# "I'm a/an X" is greedy and snags transient moods/states rather than identity.
# Reject any singleton capture containing one of these so a mood never lands as a
# profession. Not exhaustive — the confidence guard is the general backstop.
_STATE_WORDS = {
    "bit",
    "little",
    "lot",
    "tad",  # degree hedges that precede an adjective
    "tired",
    "exhausted",
    "sleepy",
    "hungry",
    "thirsty",
    "bored",
    "busy",
    "sick",
    "ill",
    "happy",
    "sad",
    "angry",
    "upset",
    "excited",
    "nervous",
    "scared",
    "afraid",
    "fine",
    "okay",
    "ok",
    "good",
    "great",
    "bad",
    "sure",
    "ready",
    "done",
    "lost",
    "confused",
    "stuck",
    "curious",
    "worried",
    "fan",
}

# Values that regex captures but aren't real facts — pronouns, filler, etc.
_JUNK_VALUES = {
    "it",
    "that",
    "this",
    "them",
    "those",
    "these",
    "something",
    "anything",
    "everything",
    "nothing",
    "stuff",
    "things",
    "whatever",
    "you",
    "me",
    "him",
    "her",
    "us",
    "they",
    "one",
    "some",
    "none",
    "all",
    "both",
    "not",
    "don't",
    "didn't",
    "won't",
    "can't",
    "to",
    "so",
}

# Minimum length for extracted values (after strip). Catches single-word junk
# that slipped past _JUNK_VALUES.
_MIN_VALUE_LEN = 2


# ── System observation patterns ────────────────────────────────────────────────
# STABLE: hardware facts that persist across sessions — saved to semantic DB.
_STABLE_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Total RAM — only common sizes to avoid false positives
    ("sys_ram_total_gb", re.compile(r"\b(8|16|32|64|128)\s*gb(?:\s+(?:ram|memory))?\b", re.I)),
]

# VOLATILE: runtime stats that change every few minutes.
# Returned as a dict — NEVER saved to long-term semantic DB.
# Lives only in MemoryManager._session_state for the current session.
_VOLATILE_PATTERNS: list[tuple[str, re.Pattern]] = [
    # CPU load: "CPU: 23%" / "cpu  45.1%"
    ("cpu_pct", re.compile(r"\bcpu[:\s]+(\d+\.?\d*)\s*%", re.I)),
    # RAM: "RAM: 67% used"
    ("ram_pct", re.compile(r"\bram[:\s]+(\d+\.?\d*)\s*%\s*used", re.I)),
    # Disk: "disk: 78% used"
    ("disk_pct", re.compile(r"\bdisk[:\s]+(\d+\.?\d*)\s*%\s*used", re.I)),
    # GPU core temp: "GPU Core: 72°C" / "gpu temp: 65C"
    ("gpu_temp_c", re.compile(r"\bgpu(?:\s+core)?[:\s]+(\d+)\s*°?\s*c\b", re.I)),
    # CPU temp: "CPU Package: 55°C" / "cpu temp: 48C"
    ("cpu_temp_c", re.compile(r"\bcpu(?:\s+package)?[:\s]+(\d+)\s*°?\s*c\b", re.I)),
    # Startup count: "25 startup programs"
    ("startup_count", re.compile(r"(\d+)\s+startup\s+(?:programs?|entries?|items?)", re.I)),
    # Free disk: "14.2 GB free"
    ("disk_free_gb", re.compile(r"(\d+\.?\d*)\s*gb\s+free", re.I)),
]

# Old keys that may exist in the DB from previous code — purged on startup via semantic.migrate()
VOLATILE_DB_KEYS = {
    "sys_cpu_pct",
    "sys_ram_pct",
    "sys_disk_pct",
    "sys_gpu_temp_c",
    "sys_cpu_temp_c",
    "sys_startup_count",
    "sys_disk_free_gb",
}


# ── Memory tree mirroring ────────────────────────────────────────────────────
# Bridges facts found above into kai/memory/tree.py so the memory model loop
# (gather -> rank -> flag -> render in memory/loop.py) has real nodes to draw
# on. Full conversational extraction into the tree is still future work (see
# BRAIN_DESIGN "Open Questions") — this mirrors only the facts the regex
# patterns above already find with reasonable confidence.
_TREE_PATHS: dict[str, tuple[str, float, float]] = {
    # base key -> (path / path template using {key}, importance, specificity)
    "user_name": ("user/identity/name", 0.6, 0.9),
    "user_role": ("user/identity/profession", 0.8, 0.7),
    "location": ("user/identity/location", 0.5, 0.7),
    "instruction": ("user/identity/critical/{key}", 0.9, 0.8),
    "note": ("user/identity/critical/{key}", 0.85, 0.7),
    "preference": ("user/preferences/{key}", 0.5, 0.6),
    "gaming": ("user/preferences/gaming/{key}", 0.5, 0.6),
    "uses": ("user/knowledge/{key}", 0.5, 0.6),
    "sys_ram_total_gb": ("user/identity/hardware/ram_total_gb", 0.6, 0.9),
}

# Per-process guard so the skeleton seed runs at most once per user.
_TREE_SEEDED: set[str] = set()


def _mirror_to_tree(saved: list[tuple[str, str]], user_id: int, source: str) -> None:
    """Write extracted facts into the matching tree paths, if any."""
    if not saved:
        return
    uid = str(user_id)
    if uid not in _TREE_SEEDED:
        _tree.seed_skeleton(uid)
        _TREE_SEEDED.add(uid)
    for key, value in saved:
        base = re.sub(r"_\d+$", "", key)
        entry = _TREE_PATHS.get(base)
        if not entry:
            continue
        template, importance, specificity = entry
        _tree.write(
            uid,
            _tree.Node(
                path=template.format(key=key),
                value=value,
                confidence=0.85,
                importance=importance,
                specificity=specificity,
                source=source,
            ),
        )


# ── Public API ─────────────────────────────────────────────────────────────────


def extract_and_save(text: str, user_id: int = 0) -> list[tuple[str, str]]:
    """
    Scan a user message for stable semantic facts. Save any found.
    Returns list of (key, value) pairs saved.
    """
    saved = []
    # One read of the existing facts, reused for the singleton overwrite guard
    # and the accumulating-slot dedup below.
    existing = {f.key: f for f in semantic.list_facts(user_id=user_id)}
    for pattern, key_name, group, confidence in _USER_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        value = match.group(group).strip()
        if len(value) < _MIN_VALUE_LEN or value.lower() in _JUNK_VALUES:
            continue
        if key_name in _SINGLETON_KEYS:
            if _looks_like_state(value):
                continue  # a transient mood/state, not a stable identity fact
            key = key_name
            if not _singleton_should_write(existing.get(key_name), value, confidence):
                continue
        else:
            key = _next_slot(key_name, value, user_id, existing)
        semantic.set_fact(key, value, source="user_message", confidence=confidence, user_id=user_id)
        saved.append((key, value))
    _mirror_to_tree(saved, user_id, source="stated")
    return saved


def extract_stable_observations(text: str, user_id: int = 0) -> list[tuple[str, str]]:
    """
    Scan Kai's response for stable hardware facts worth keeping across sessions.
    Saves to semantic DB. Returns list of (key, value) pairs saved.
    """
    saved = []
    for key, pattern in _STABLE_PATTERNS:
        match = pattern.search(text)
        if match:
            value = match.group(1).strip()
            semantic.set_fact(key, value, source="observation", user_id=user_id)
            saved.append((key, value))
    _mirror_to_tree(saved, user_id, source="stated")
    return saved


def extract_volatile_observations(text: str) -> dict[str, str]:
    """
    Scan Kai's response for volatile runtime stats (CPU%, temps, disk%, etc.).
    Returns a dict — does NOT touch the DB. Caller stores in session cache only.
    """
    found: dict[str, str] = {}
    for key, pattern in _VOLATILE_PATTERNS:
        match = pattern.search(text)
        if match:
            found[key] = match.group(1).strip()
    return found


# ── Helpers ────────────────────────────────────────────────────────────────────


def _looks_like_state(value: str) -> bool:
    """True if a singleton capture contains a transient-state word — a guard for
    the greedy 'I'm a/an X' pattern so a mood isn't saved as identity."""
    return any(tok in _STATE_WORDS for tok in re.findall(r"[a-z]+", value.lower()))


def _singleton_should_write(prior, value: str, confidence: float) -> bool:
    """Whether a singleton capture should be written.

    First capture wins (sets the fact when none exists). An existing fact is only
    overwritten by a *different* value whose confidence clears the overwrite floor
    AND is at least as high as what's already stored — so a casual, low-trust
    phrase can't clobber a known name/role/location.
    """
    if prior is None:
        return True
    if prior.value.strip().lower() == value.strip().lower():
        return False  # already stored — no-op
    return confidence >= _SINGLETON_OVERWRITE_MIN and confidence >= prior.confidence


def _next_slot(base_key: str, value: str, user_id: int = 0, existing=None) -> str:
    """
    For accumulating keys (preference_1, preference_2, ...):
    - If this exact value already stored, return same key (no duplicate).
    - Otherwise find the next free numbered slot.

    `existing` (dict of key -> SemanticFact) is reused when the caller already
    loaded the fact list; otherwise it's fetched here.
    """
    facts = (
        list(existing.values()) if existing is not None else semantic.list_facts(user_id=user_id)
    )
    for f in facts:
        if f.key.startswith(base_key) and f.value.lower().strip() == value.lower().strip():
            return f.key  # already have it
    existing_keys = {f.key for f in facts}
    i = 1
    while f"{base_key}_{i}" in existing_keys:
        i += 1
    return f"{base_key}_{i}"
