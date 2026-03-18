"""
Upgrade awareness — detects version changes at startup and writes an
episodic memory entry so Kai naturally remembers being updated.

Kai never sees her own source code. She sees:
  - The version number
  - A human-written changelog (what changed, in plain language)
  - An episodic memory of the upgrade event

This gives her self-awareness without self-modification ability.
"""
import json

from kai.config import CHANGELOG_PATH
from kai.memory import semantic, episodic

# Semantic key where the last-known version is stored
_VERSION_KEY = "kai_version"


def check_for_upgrade(embed_fn=None) -> str | None:
    """
    Compare the current changelog version to the last-known version in semantic memory.
    If different (or first run), write an episodic entry about the upgrade and update
    the stored version.

    Returns the upgrade summary string if an upgrade was detected, None otherwise.
    """
    if not CHANGELOG_PATH.exists():
        return None

    try:
        changelog = json.loads(CHANGELOG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    current_version = changelog.get("version", "unknown")
    notes = changelog.get("notes", [])
    updated = changelog.get("updated", "")

    last_version = semantic.get_fact(_VERSION_KEY)

    if last_version == current_version:
        return None  # no change

    # Build a natural-language upgrade summary
    if last_version:
        headline = f"I was upgraded from v{last_version} to v{current_version} on {updated}."
    else:
        headline = f"First startup as v{current_version}."

    if notes:
        details = " ".join(notes)
        summary = f"{headline} Changes: {details}"
    else:
        summary = headline

    # Write to episodic memory — Kai will "remember" this like any other event
    episodic.add_entry(
        content=summary,
        embed_fn=embed_fn,
        entry_type="event",
        metadata={"upgrade": True, "from": last_version or "new", "to": current_version},
    )

    # Update the stored version so we don't fire again
    semantic.set_fact(_VERSION_KEY, current_version, source="system")

    return summary
