"""Kai — a local AI agent. Python + Ollama + SQLite, no cloud calls."""

# Single source of truth for the package version: pyproject.toml reads it from
# here (`dynamic = ["version"]`), so a bump lands in the wheel, the CLI banner
# and `pip show` at once.
#
# kai/persona/changelog.json carries the SAME version plus the human-readable
# release notes Kai narrates on first run after an upgrade (kai/system/upgrade.py).
# Bump both — tests/test_packaging.py fails if they drift apart.
__version__ = "0.7.0"
