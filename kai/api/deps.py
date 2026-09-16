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


def require_owner(request: Request) -> dict:
    """The authenticated owner, or 403. For the few things only the owner does; most
    machine-wide controls are require_capability() now, which admins can pass."""
    from kai.store import users

    user = require_user(request)
    owner = users.get_owner_id()
    if owner is None or user["user_id"] != owner:
        raise HTTPException(status_code=403, detail="Only the owner of this Kai can change that.")
    return user


_REFUSED = {
    "server.control": "Only the owner or an admin can do that.",
    "models.manage": "Only the owner or an admin can change the models on this Kai.",
    "recipes.manage": "Only the owner or an admin can change recipes on this Kai.",
    "accounts.view": "Only the owner or an admin can see accounts.",
    "accounts.create": "Only the owner or an admin can add accounts.",
    "accounts.edit": "Only the owner or an admin can edit accounts.",
    "accounts.kick": "Only the owner can kick someone.",
    "accounts.grant": "Only the owner can hand out grants.",
    "accounts.transfer": "Only the owner can hand over ownership.",
}


def require_capability(request: Request, capability: str) -> dict:
    """The authenticated user, if their level allows *capability* (kai/core/permissions.py).
    Otherwise 403 — and anything beyond looking goes in the audit log, because someone
    reaching past their level is what the log is for."""
    from kai.core import permissions

    user = require_user(request)
    if permissions.can(user["user_id"], capability):
        return user
    message = _REFUSED.get(capability, "Your account can't do that.")
    if capability != "accounts.view":
        from kai.store import audit

        audit.record(
            capability,
            actor_id=user["user_id"],
            actor_name=user.get("name"),
            detail={"refused": message, "path": request.url.path},
            ok=False,
        )
    raise HTTPException(status_code=403, detail=message)
