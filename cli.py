#!/usr/bin/env python3
"""Shim — the terminal REPL now lives in kai/apps/cli.py.

Kept so `python cli.py` keeps working from a source checkout (README,
scripts/start.sh, scripts/kai.desktop). Installed copies use the console
script instead; nothing new should import this module.
"""

from kai.apps.cli import *  # noqa: F401,F403  (re-export for `from cli import app`)
from kai.apps.cli import main

if __name__ == "__main__":
    main()
