"""
Tool package — auto-discovers and registers every first-party tool.

Importing this package walks the domain subpackages (system, files, web,
knowledge, memory, media, compute, agent) and imports each tool module so its
@registry.tool() decorators fire. Adding a first-party tool is therefore just
dropping a .py file into one of those folders — there is no import list to edit
and (with inline metadata on the decorator) no central dicts to touch either.

Skipped: the registry itself, package __init__ files, and any _-prefixed helper
module (e.g. _shell). A tool module that fails to import raises loudly here, the
same as the old explicit-import list did — a broken tool must never be silently
dropped.

Note: this covers *first-party* tools shipped in the repo. Untrusted third-party
marketplace packs load through the (sandboxed) pack loader, never this scan.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

from kai.tools.registry import registry  # noqa: F401 — re-exported

_PKG_DIR = Path(__file__).parent
_SKIP = {"registry", "__init__"}


def _on_error(name: str) -> None:  # pragma: no cover — surfaces a broken subpackage
    raise ImportError(f"failed to import tool package {name!r}")


def _discover() -> None:
    pkg = sys.modules[__name__]
    for _finder, mod_name, is_pkg in pkgutil.walk_packages(
        [str(_PKG_DIR)], prefix="kai.tools.", onerror=_on_error
    ):
        leaf = mod_name.rsplit(".", 1)[-1]
        if is_pkg or leaf in _SKIP or leaf.startswith("_"):
            continue
        mod = importlib.import_module(mod_name)
        # Expose the module under its leaf name on the package, matching the old
        # explicit `from kai.tools.<sub> import <leaf>` list — callers do
        # `from kai.tools import lxc`, `... import memory_tools`, etc.
        setattr(pkg, leaf, mod)


_discover()

__all__ = ["registry"]
