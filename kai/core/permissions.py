"""
Who may do what on this Kai. One table to read, one place to change.

docs/active/PERMISSIONS.md is the contract; this is its enforced half. A capability is listed
here only once code checks it — a row for something nothing enforces would be a promise the
server can't keep. Tool tiers per level, guest monitoring and Kai's refusal gate get their
rows when they land. The one forward entry is GRANTS: the owner can already set them, and the
tool gate that reads tools.destructive is the next increment.

Levels are read live from the accounts table on every check, so a demotion or a kick applies
to someone's very next request. It lives in kai/core, not kai/api, because the memory layer
(guests keep nothing) and the tool gate both need it, and they sit below the API.
"""

from kai.store import users

LEVELS = users.LEVELS  # highest first

CAPABILITIES: dict[str, frozenset[str]] = {
    # Accounts
    "accounts.view": frozenset({"owner", "admin"}),  # every account, and the audit log
    "accounts.create": frozenset({"owner", "admin"}),  # admins: Members and Guests only
    "accounts.edit": frozenset({"owner", "admin"}),  # admins: only accounts they created
    "accounts.kick": frozenset({"owner"}),
    "accounts.grant": frozenset({"owner"}),
    "accounts.transfer": frozenset({"owner"}),
    # The machine
    "server.control": frozenset({"owner", "admin"}),  # restart, shut down, console, logs
    "models.manage": frozenset({"owner", "admin"}),
    "recipes.manage": frozenset({"owner", "admin"}),
    # Memory
    "memory.persist": frozenset({"owner", "admin", "member"}),  # guests keep nothing
}

# Per-person additions, made by the owner only: grant -> the one level it can be given to.
GRANTS: dict[str, str] = {
    "tools.destructive": "member",
}


def level_of(user_id: int) -> str | None:
    """The account's level, or None for an id with no account."""
    return users.get_level(user_id)


def can(user_id: int, capability: str) -> bool:
    """Whether this account's level allows *capability*. An unknown capability is refused,
    so a typo fails closed."""
    level = level_of(user_id)
    return level is not None and level in CAPABILITIES.get(capability, frozenset())


def capabilities_of(user_id: int) -> list[str]:
    """Everything this account's level allows — what the UI shows and hides by."""
    level = level_of(user_id)
    return sorted(cap for cap, levels in CAPABILITIES.items() if level in levels)


def assignable_levels(actor_id: int) -> tuple[str, ...]:
    """The levels this account may give someone, creating or editing. Nobody assigns
    "owner": ownership moves only by transfer."""
    level = level_of(actor_id)
    if level == "owner":
        return ("admin", "member", "guest")
    if level == "admin":
        return ("member", "guest")
    return ()


def can_edit(actor_id: int, target: dict) -> bool:
    """The owner edits every account. An admin edits only the Members and Guests they
    created — never an admin, the owner, or an account with no recorded creator."""
    level = level_of(actor_id)
    if level == "owner":
        return True
    if level == "admin":
        return target.get("created_by") == actor_id and level_of(target["id"]) in (
            "member",
            "guest",
        )
    return False


def keeps_memory(user_id: int) -> bool:
    """Whether Kai may keep anything from this account's conversations. An id with no account
    (the terminal's scratch identity) isn't an account and keeps the old default."""
    level = level_of(user_id)
    return level is None or level in CAPABILITIES["memory.persist"]
