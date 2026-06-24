"""
Shared request dependencies for API routers.

These read the authenticated user that the _AuthGuard middleware (in web.py)
stashes on request.state. They live here — not in web.py — so routers in this
package can depend on them without importing the entry-point module (which would
double-import when web.py is run directly).
"""
from fastapi import Request


def get_user(request: Request) -> dict | None:
    """The authenticated user dict ({"name", "user_id"}) or None for public routes."""
    return getattr(request.state, "user", None)


def uid_for(request: Request) -> int:
    """The authenticated user_id, or 0 for public/unauthenticated requests."""
    user = get_user(request)
    return user["user_id"] if user else 0
