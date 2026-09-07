"""Kai's entry points.

These live inside the package (not at the repo root) so a wheel can expose them
as console scripts — `kai`, `kai-web`, `kai-app` — and so they resolve the same
from a checkout, an installed wheel, or a container. The root-level `web.py`,
`cli.py` and `app.py` are thin shims kept for the documented
`python web.py` workflow.

    web      FastAPI + SSE server (kai-web)
    cli      terminal REPL        (kai)
    desktop  pywebview native app (kai-app)
"""
