"""
Platform detection — one source of truth.

Several tool modules used to each redefine `_IS_WINDOWS = sys.platform == "win32"`.
Import these constants instead so a detection tweak (or a third platform) is a
one-line change here, not a hunt across the codebase.
"""
import sys

IS_WINDOWS = sys.platform == "win32"
IS_LINUX   = sys.platform.startswith("linux")
IS_MAC     = sys.platform == "darwin"
