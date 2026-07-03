"""Domain route modules extracted from web.py. Each exposes `router` (an
APIRouter) mounted by web.py via `app.include_router(...)`. Shared bits come from
kai/api/{deps,state,models}.py so these never import the web entrypoint."""
