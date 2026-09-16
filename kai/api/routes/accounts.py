"""
Accounts: list them, create them, edit them, hand out grants, kick, and pass on ownership.

Every rule lives in kai/core/permissions.py. These routes ask it, act, and write what happened
— refusals included — to the audit log (kai/store/audit.py). docs/active/PERMISSIONS.md is the
contract.
"""

from fastapi import APIRouter, HTTPException, Request

from kai.api import state
from kai.api.deps import require_capability
from kai.api.models import (
    AccountCreateRequest,
    AccountGrantsRequest,
    AccountUpdateRequest,
    KickRequest,
    TransferOwnerRequest,
)
from kai.core import permissions
from kai.store import audit, users

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _account_or_404(user_id: int) -> dict:
    account = users.get_account(user_id)
    if not account:
        raise HTTPException(status_code=404, detail="No such account.")
    return account


def _refuse(
    action: str,
    actor: dict,
    message: str,
    *,
    target: dict | None = None,
    target_name: str | None = None,
    **detail,
):
    """Write a refused attempt to the audit log, then answer 403."""
    audit.record(
        action,
        actor_id=actor["user_id"],
        actor_name=actor.get("name"),
        target_id=target["id"] if target else None,
        target_name=target["name"] if target else target_name,
        detail={"refused": message, **detail},
        ok=False,
    )
    raise HTTPException(status_code=403, detail=message)


def _view(account: dict, actor_id: int) -> dict:
    return {
        **account,
        "can_edit": permissions.can_edit(actor_id, account),
        "you": account["id"] == actor_id,
    }


@router.get("")
async def list_accounts(request: Request):
    """Every account on this Kai. Owner and admins."""
    user = require_capability(request, "accounts.view")
    me = user["user_id"]
    return {
        "accounts": [_view(a, me) for a in users.list_accounts()],
        "assignable_levels": list(permissions.assignable_levels(me)),
        "grants": dict(permissions.GRANTS) if permissions.can(me, "accounts.grant") else {},
    }


@router.get("/audit")
async def audit_log(request: Request, limit: int = 200, before: int | None = None):
    """The audit log, newest first. The owner and every admin see every entry."""
    require_capability(request, "accounts.view")
    return {"entries": audit.list_entries(limit=max(1, min(limit, 500)), before_id=before)}


@router.post("")
async def create_account(req: AccountCreateRequest, request: Request):
    """Create an account. Admins create Members and Guests; only the owner creates Admins."""
    from kai.system.device import key_hash

    user = require_capability(request, "accounts.create")
    me = user["user_id"]
    name = req.name.strip()
    if req.level not in permissions.assignable_levels(me):
        _refuse(
            "account.create",
            user,
            f"Your account can't create {req.level} accounts.",
            target_name=name,
            level=req.level,
        )
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if not users.valid_pin(req.pin):
        raise HTTPException(status_code=400, detail="PIN must be 4–8 digits")
    new = users.create_user(name, req.pin.strip(), key_hash(), level=req.level, created_by=me)
    if not new:
        raise HTTPException(status_code=409, detail="That name is already taken")
    audit.record(
        "account.create",
        actor_id=me,
        actor_name=user.get("name"),
        target_id=new["id"],
        target_name=name,
        detail={"level": new["level"]},
    )
    return {"ok": True, "account": _view(users.get_account(new["id"]), me)}


@router.patch("/{user_id}")
async def edit_account(user_id: int, req: AccountUpdateRequest, request: Request):
    """Rename an account, reset its PIN, or change its level. The owner edits anyone; an
    admin edits only the Members and Guests they created."""
    user = require_capability(request, "accounts.edit")
    me = user["user_id"]
    target = _account_or_404(user_id)
    if not permissions.can_edit(me, target):
        _refuse("account.edit", user, "You can only edit accounts you created.", target=target)
    if req.level is not None:
        if target["level"] == "owner":
            raise HTTPException(status_code=400, detail="Ownership moves only by transfer.")
        if req.level not in permissions.assignable_levels(me):
            _refuse(
                "account.edit",
                user,
                f"Your account can't make someone {req.level}.",
                target=target,
                level=req.level,
            )
    name = req.name.strip() if req.name is not None else None
    if name == "":
        raise HTTPException(status_code=400, detail="Name is required")
    if req.pin is not None and not users.valid_pin(req.pin):
        raise HTTPException(status_code=400, detail="PIN must be 4–8 digits")
    if name is None and req.pin is None and req.level is None:
        raise HTTPException(status_code=400, detail="Nothing to change.")

    result = users.update_account(
        user_id,
        name=name,
        pin=req.pin.strip() if req.pin is not None else None,
        level=req.level,
    )
    if result is None:
        raise HTTPException(status_code=409, detail="That name is already taken")
    if not result:
        raise HTTPException(status_code=400, detail="That change isn't possible.")

    changes: dict = {}
    if name is not None and name != target["name"]:
        changes["name"] = {"from": target["name"], "to": name}
    if req.pin is not None:
        changes["pin"] = "reset"  # never the value
        if user_id != me:
            changes["sessions_ended"] = users.revoke_sessions(user_id)
    if req.level is not None and req.level != target["level"]:
        changes["level"] = {"from": target["level"], "to": req.level}
        kept = [g for g in target["grants"] if permissions.GRANTS.get(g) == req.level]
        if kept != target["grants"]:  # a grant fits one level; one that no longer fits goes
            users.set_grants(user_id, kept)
            changes["grants"] = {"from": target["grants"], "to": kept}
    audit.record(
        "account.edit",
        actor_id=me,
        actor_name=user.get("name"),
        target_id=user_id,
        target_name=name or target["name"],
        detail=changes,
    )
    return {"ok": True, "account": _view(users.get_account(user_id), me)}


@router.put("/{user_id}/grants")
async def set_grants(user_id: int, req: AccountGrantsRequest, request: Request):
    """Replace an account's grants. Owner only, and each grant fits exactly one level."""
    user = require_capability(request, "accounts.grant")
    target = _account_or_404(user_id)
    wanted = sorted(set(req.grants))
    unknown = [g for g in wanted if g not in permissions.GRANTS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"No such grant: {', '.join(unknown)}")
    misfits = [g for g in wanted if permissions.GRANTS[g] != target["level"]]
    if misfits:
        raise HTTPException(
            status_code=400,
            detail=f"{misfits[0]} can only be given to a {permissions.GRANTS[misfits[0]]}.",
        )
    users.set_grants(user_id, wanted)
    audit.record(
        "account.grants",
        actor_id=user["user_id"],
        actor_name=user.get("name"),
        target_id=user_id,
        target_name=target["name"],
        detail={"from": target["grants"], "to": wanted},
    )
    return {"ok": True, "account": _view(users.get_account(user_id), user["user_id"])}


@router.post("/{user_id}/kick")
async def kick_account(user_id: int, req: KickRequest, request: Request):
    """Sign an account out everywhere, and optionally remove it with all its data. Owner only."""
    user = require_capability(request, "accounts.kick")
    if user_id == user["user_id"]:
        raise HTTPException(status_code=400, detail="You can't kick yourself.")
    target = _account_or_404(user_id)
    ended = users.revoke_sessions(user_id)
    deleted = False
    if req.delete:
        with state.user_brains_lock:
            brain = state.user_brains.pop(user_id, None)
            if brain:
                brain.shutdown()
        deleted = users.delete_user(user_id)
    audit.record(
        "account.kick",
        actor_id=user["user_id"],
        actor_name=user.get("name"),
        target_id=user_id,
        target_name=target["name"],
        detail={"sessions_ended": ended, "deleted": deleted},
    )
    return {"ok": True, "sessions_ended": ended, "deleted": deleted}


@router.post("/transfer-owner")
async def transfer_owner(req: TransferOwnerRequest, request: Request):
    """Hand ownership to another account; the old owner becomes an admin. Owner only."""
    user = require_capability(request, "accounts.transfer")
    if req.to_id == user["user_id"]:
        raise HTTPException(status_code=400, detail="You already own this Kai.")
    target = _account_or_404(req.to_id)
    if not users.transfer_ownership(req.to_id):
        raise HTTPException(status_code=409, detail="Ownership couldn't be transferred.")
    audit.record(
        "account.transfer_owner",
        actor_id=user["user_id"],
        actor_name=user.get("name"),
        target_id=req.to_id,
        target_name=target["name"],
        detail={"old_owner_is_now": "admin"},
    )
    return {"ok": True}
