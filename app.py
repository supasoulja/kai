#!/usr/bin/env python3
"""Shim — the native desktop app now lives in kai/apps/desktop.py.

Kept so `python app.py` keeps working from a source checkout (README,
scripts/start.sh, scripts/kai.desktop). Installed copies use the console
script instead; nothing new should import this module.
"""

from kai.apps.desktop import *  # noqa: F401,F403  (re-export for `from app import app`)
from kai.apps.desktop import main

if __name__ == "__main__":
    main()
