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


# ── User message patterns ──────────────────────────────────────────────────────
# Extracted from things the USER says. (pattern, key_name, capture_group)

_USER_PATTERNS: list[tuple[re.Pattern, str, int]] = [
    # "my name is X" / "call me X"
    (re.compile(r"\b(?:my name is|call me)\s+([A-Za-z]+)", re.I),          "user_name",    1),
    # "I prefer X" / "I like X" / "I love X" / "I hate X"
    (re.compile(r"\bI (?:prefer|like|love|hate|dislike)\s+(.+?)(?:\s*[.,!?]|$)", re.I), "preference", 1),
    # "from now on, X"
    (re.compile(r"\bfrom now on[,\s]+(.+?)(?:\s*[.,!?]|$)", re.I),         "instruction",  1),
    # "remember that X" / "remember X"
    (re.compile(r"\bremember (?:that\s+)?(.+?)(?:\s*[.,!?]|$)", re.I),     "note",         1),
    # "I'm a/an X" / "I am a/an X"
    (re.compile(r"\bI(?:'m| am) an?\s+([A-Za-z ]+?)(?:\s*[.,!?]|$)", re.I),"user_role",    1),
    # "I use X" (tools, languages, hardware)
    (re.compile(r"\bI use\s+([A-Za-z0-9_+# ]+?)(?:\s*[.,!?]|$)", re.I),   "uses",         1),
    # "I'm based in X" / "I live in X"
    (re.compile(r"\bI(?:'m| am) (?:based |located )?in\s+([A-Za-z ,]+?)(?:\s*[.,!?]|$)", re.I), "location", 1),
    # "I play X" / "I game on X"
    (re.compile(r"\bI (?:play|mainly play|mostly play)\s+(.+?)(?:\s*[.,!?]|$)", re.I), "gaming", 1),
]

# Keys where only one value makes sense — overwrite rather than append _1, _2
_SINGLETON_KEYS = {"user_name", "user_role", "location"}

# Values that regex captures but aren't real facts — pronouns, filler, etc.
_JUNK_VALUES = {
    "it", "that", "this", "them", "those", "these", "something", "anything",
    "everything", "nothing", "stuff", "things", "whatever", "you", "me",
    "him", "her", "us", "they", "one", "some", "none", "all", "both",
    "not", "don't", "didn't", "won't", "can't", "to", "so",
}

# Minimum length for extracted values (after strip). Catches single-word junk
# that slipped past _JUNK_VALUES.
_MIN_VALUE_LEN = 2


# ── System observation patterns ────────────────────────────────────────────────
# STABLE: hardware facts that persist across sessions — saved to semantic DB.
_STABLE_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Total RAM — only common sizes to avoid false positives
    ("sys_ram_total_gb",  re.compile(r"\b(8|16|32|64|128)\s*gb(?:\s+(?:ram|memory))?\b", re.I)),
]

# VOLATILE: runtime stats that change every few minutes.
# Returned as a dict — NEVER saved to long-term semantic DB.
# Lives only in MemoryManager._session_state for the current session.
_VOLATILE_PATTERNS: list[tuple[str, re.Pattern]] = [
    # CPU load: "CPU: 23%" / "cpu  45.1%"
    ("cpu_pct",       re.compile(r"\bcpu[:\s]+(\d+\.?\d*)\s*%", re.I)),
    # RAM: "RAM: 67% used"
    ("ram_pct",       re.compile(r"\bram[:\s]+(\d+\.?\d*)\s*%\s*used", re.I)),
    # Disk: "disk: 78% used"
    ("disk_pct",      re.compile(r"\bdisk[:\s]+(\d+\.?\d*)\s*%\s*used", re.I)),
    # GPU core temp: "GPU Core: 72°C" / "gpu temp: 65C"
    ("gpu_temp_c",    re.compile(r"\bgpu(?:\s+core)?[:\s]+(\d+)\s*°?\s*c\b", re.I)),
    # CPU temp: "CPU Package: 55°C" / "cpu temp: 48C"
    ("cpu_temp_c",    re.compile(r"\bcpu(?:\s+package)?[:\s]+(\d+)\s*°?\s*c\b", re.I)),
    # Startup count: "25 startup programs"
    ("startup_count", re.compile(r"(\d+)\s+startup\s+(?:programs?|entries?|items?)", re.I)),
    # Free disk: "14.2 GB free"
    ("disk_free_gb",  re.compile(r"(\d+\.?\d*)\s*gb\s+free", re.I)),
]

# Old keys that may exist in the DB from previous code — purged on startup via semantic.migrate()
VOLATILE_DB_KEYS = {
    "sys_cpu_pct", "sys_ram_pct", "sys_disk_pct",
    "sys_gpu_temp_c", "sys_cpu_temp_c", "sys_startup_count", "sys_disk_free_gb",
}


# ── Public API ─────────────────────────────────────────────────────────────────

def extract_and_save(text: str) -> list[tuple[str, str]]:
    """
    Scan a user message for stable semantic facts. Save any found.
    Returns list of (key, value) pairs saved.
    """
    saved = []
    for pattern, key_name, group in _USER_PATTERNS:
        match = pattern.search(text)
        if match:
            value = match.group(group).strip()
            if len(value) < _MIN_VALUE_LEN:
                continue
            if value.lower() in _JUNK_VALUES:
                continue
            key = key_name if key_name in _SINGLETON_KEYS else _next_slot(key_name, value)
            semantic.set_fact(key, value, source="user_message")
            saved.append((key, value))
    return saved


def extract_stable_observations(text: str) -> list[tuple[str, str]]:
    """
    Scan Kai's response for stable hardware facts worth keeping across sessions.
    Saves to semantic DB. Returns list of (key, value) pairs saved.
    """
    saved = []
    for key, pattern in _STABLE_PATTERNS:
        match = pattern.search(text)
        if match:
            value = match.group(1).strip()
            semantic.set_fact(key, value, source="observation")
            saved.append((key, value))
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

def _next_slot(base_key: str, value: str) -> str:
    """
    For accumulating keys (preference_1, preference_2, ...):
    - If this exact value already stored, return same key (no duplicate).
    - Otherwise find the next free numbered slot.
    """
    existing = semantic.list_facts()
    for f in existing:
        if f.key.startswith(base_key) and f.value.lower().strip() == value.lower().strip():
            return f.key  # already have it
    existing_keys = {f.key for f in existing}
    i = 1
    while f"{base_key}_{i}" in existing_keys:
        i += 1
    return f"{base_key}_{i}"
