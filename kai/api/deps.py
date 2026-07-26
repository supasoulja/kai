"""
Shared request dependencies for API routers.

These read the authenticated user that the _AuthGuard middleware (in web.py)
stashes on request.state. They live here — not in web.py — so routers in this
package can depend on them without importing the entry-point module (which would
double-import when web.py is run directly).
"""

from fastapi import HTTPException, Request


def get_user(request: Request) -> dict | None:
    """The authenticated user dict ({"name", "user_id"}) or None for public routes."""
    return getattr(request.state, "user", None)


def uid_for(request: Request) -> int:
    """The authenticated user_id, or 0 for public/unauthenticated requests."""
    user = get_user(request)
    return user["user_id"] if user else 0


def require_user(request: Request) -> dict:
    """FastAPI dependency: the authenticated user, or 401 if there is none.

    Use as ``user = Depends(require_user)`` on routes that must be authenticated,
    replacing the hand-rolled ``if not get_user(request): raise HTTPException(401)``.
    (The _AuthGuard middleware already blocks unauthenticated requests to
    non-public routes; this makes the guarantee explicit at the route and covers
    the optional-auth paths.)"""
    user = get_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user
