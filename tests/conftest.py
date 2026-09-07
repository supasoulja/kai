"""Shared pytest configuration.

Two jobs, both about keeping tests off the real database:

1. Force KAI_TEST_MODE on for every test process.

2. Redirect cfg.DB_PATH away from the production database *before* any test
   module is imported. Test modules each set their own temp DB at import time
   (which now works — get_conn() resolves cfg.DB_PATH at call time, see
   kai/store/db.py). This is the safety net for the module that forgets: without
   it, a stray _clear_users() in setup would wipe real accounts. With it, the
   worst case is a throwaway temp file.

Historical note: before kai/store/db.py resolved the path at call time and
tracked schema init per-path, the per-module `cfg.DB_PATH = tmp` assignments
silently did nothing — whichever module imported db first won, and the rest
shared its database. That produced ~25 order-dependent "no such table" failures.
"""

import os
import sys
import tempfile
from pathlib import Path

import kai.config as cfg

os.environ.setdefault("KAI_TEST_MODE", "1")

# The real database, captured before anything can point a test at it.
_PRODUCTION_DB = Path(cfg.DB_PATH)


def pytest_configure(config):
    """Runs before test modules are collected/imported. Swap the production DB
    for a session-wide temp file as the default, so a module that never sets its
    own path can't touch real data."""
    if Path(cfg.DB_PATH) == _PRODUCTION_DB:
        tmp = tempfile.NamedTemporaryFile(suffix="_conftest.db", delete=False)
        tmp.close()
        cfg.DB_PATH = Path(tmp.name)

    # The tree/state/knowledge/workspace dirs default to the real ones too — and since
    # _post_turn now WRITES to the tree (extraction + reinforcement) on top of the
    # regex mirror commit_turn already did, an un-isolated Brain test would mutate
    # real memory. Redirect them to a session sandbox. Modules capture these at
    # import, so set cfg (for later imports) AND any already-imported module ref.
    if not getattr(cfg, "_TEST_VAR_SANDBOXED", False):
        sandbox = Path(tempfile.mkdtemp(prefix="kai_test_var_"))
        cfg.TREE_DIR = sandbox / "tree"
        cfg.STATE_DIR = sandbox / "state"
        cfg.KNOWLEDGE_DIR = sandbox / "knowledge"
        # notes.save mirrors every note to disk, so an un-isolated notes test
        # drops .md/.txt files into the user's real ~/KaiFiles/notes.
        cfg.WORKSPACE_DIR = sandbox / "KaiFiles"
        cfg.NOTES_DIR = cfg.WORKSPACE_DIR / "notes"
        for d in (cfg.TREE_DIR, cfg.STATE_DIR, cfg.KNOWLEDGE_DIR, cfg.NOTES_DIR):
            d.mkdir(parents=True, exist_ok=True)
        for modname, attr, val in (
            ("kai.memory.tree", "_TREE_DIR", cfg.TREE_DIR),
            ("kai.memory.state", "_STATE_DIR", cfg.STATE_DIR),
        ):
            mod = sys.modules.get(modname)
            if mod is not None:
                setattr(mod, attr, val)
        cfg._TEST_VAR_SANDBOXED = True


def pytest_runtest_setup(item):
    """Belt and suspenders: refuse to run any test whose DB_PATH somehow points
    back at the production database."""
    if Path(cfg.DB_PATH) == _PRODUCTION_DB:
        raise RuntimeError(
            f"Refusing to run {item.nodeid}: cfg.DB_PATH points at the real "
            f"database ({_PRODUCTION_DB}). Set it to a temp file first."
        )
