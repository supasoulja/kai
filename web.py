#!/usr/bin/env python3
"""Shim — the FastAPI + SSE web server now lives in kai/apps/web.py.

Kept so `python web.py` keeps working from a source checkout (README,
scripts/start.sh, scripts/kai.desktop). Installed copies use the console
script instead; nothing new should import this module.
"""

from kai.apps.web import *  # noqa: F401,F403  (re-export for `from web import app`)
from kai.apps.web import main

if __name__ == "__main__":
    main()
