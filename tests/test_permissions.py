"""Accounts, levels and permissions — increment 1 of docs/active/PERMISSIONS.md.

Hermetic: the sandboxed database and MEMORY_DIR from conftest, no Ollama, no app.
Route-level checks (who gets 403, a kick signing someone out) are in test_security.py.
"""

import sqlite3

import pytest

from kai.core import permissions
from kai.store import audit, users
from kai.store.db import get_conn

_MACHINE = "test-machine-hash"


@pytest.fixture(autouse=True)
def _no_accounts():
    users._table_ensured = False  # re-run the create/migrate check against this database
    users._ensure_table()
    audit._ensure_table()
    conn = get_conn()
    conn.execute("DELETE FROM users")
    conn.execute("DELETE FROM audit_log")
    try:
        conn.execute("DELETE FROM session_tokens")
    except sqlite3.OperationalError:
        pass
    conn.commit()


def _make(name: str, level: str = "member", created_by: int | None = None) -> int:
    account = users.create_user(name, "1234", _MACHINE, level=level, created_by=created_by)
    assert account is not None, name
    return account["id"]


def test_the_first_account_is_the_owner_and_everyone_after_starts_as_a_member():
    owner = _make("first", level="guest")  # the level asked for doesn't matter for the first
    member = _make("second")
    assert users.get_owner_id() == owner and users.get_level(owner) == "owner"
    assert users.get_level(member) == "member"


def test_nobody_after_the_first_is_ever_created_as_owner():
    _make("first")
    assert users.create_user("usurper", "1234", _MACHINE, level="owner") is None
    assert users.create_user("typo", "1234", _MACHINE, level="superuser") is None


def test_migration_keeps_the_lowest_id_as_owner():
    conn = get_conn()
    conn.execute("DROP TABLE users")
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL UNIQUE COLLATE NOCASE, pin_hash TEXT NOT NULL, "
        "machine_hash TEXT NOT NULL, created_at TEXT NOT NULL, last_seen TEXT)"
    )
    for name in ("oldest", "newer", "newest"):
        conn.execute(
            "INSERT INTO users (name, pin_hash, machine_hash, created_at) "
            "VALUES (?, 'x', ?, '2026-01-01')",
            (name, _MACHINE),
        )
    conn.commit()
    users._table_ensured = False

    accounts = {a["name"]: a for a in users.list_accounts()}
    assert accounts["oldest"]["level"] == "owner"
    assert accounts["newer"]["level"] == accounts["newest"]["level"] == "member"
    assert all(a["created_by"] is None and a["grants"] == [] for a in accounts.values())


def test_transferring_ownership_moves_it_and_the_old_owner_becomes_an_admin():
    owner, heir = _make("owner"), _make("heir")
    assert users.transfer_ownership(heir)
    assert users.get_owner_id() == heir
    assert users.get_level(owner) == "admin" and users.get_level(heir) == "owner"
    assert not users.transfer_ownership(99_999)


def test_update_account_never_touches_ownership_and_names_stay_unique():
    owner, member = _make("owner"), _make("member")
    _make("taken")
    assert users.update_account(owner, level="admin") is False
    assert users.update_account(member, level="owner") is False
    assert users.get_owner_id() == owner
    assert users.update_account(member, name="taken") is None


def test_a_rename_follows_the_account_into_its_open_sessions():
    from kai.apps import web

    uid = _make("Sam")
    token = web._issue_token({"user_id": uid, "name": "Sam"})
    assert users.update_account(uid, name="Samantha")
    assert web._get_session(token) == {"user_id": uid, "name": "Samantha"}


def test_what_each_level_may_assign():
    owner = _make("owner")
    admin = _make("admin", level="admin")
    member = _make("member")
    assert permissions.assignable_levels(owner) == ("admin", "member", "guest")
    assert permissions.assignable_levels(admin) == ("member", "guest")
    assert permissions.assignable_levels(member) == ()


def test_admins_edit_only_the_members_and_guests_they_created():
    owner = _make("owner")
    admin = _make("admin", level="admin", created_by=owner)
    other_admin = _make("other-admin", level="admin", created_by=owner)
    mine = _make("mine", created_by=admin)
    my_guest = _make("my-guest", level="guest", created_by=admin)
    theirs = _make("theirs", created_by=other_admin)
    legacy = _make("legacy")  # no recorded creator
    account = users.get_account

    assert permissions.can_edit(admin, account(mine))
    assert permissions.can_edit(admin, account(my_guest))
    for untouchable in (theirs, legacy, other_admin, owner, admin):
        assert not permissions.can_edit(admin, account(untouchable)), untouchable
    assert all(permissions.can_edit(owner, account(u)) for u in (admin, mine, theirs, legacy))
    assert not permissions.can_edit(mine, account(my_guest))


def test_capabilities_by_level():
    owner = _make("owner")
    admin = _make("admin", level="admin")
    member = _make("member")
    assert permissions.can(admin, "server.control") and permissions.can(admin, "models.manage")
    assert not permissions.can(admin, "accounts.kick")
    assert not permissions.can(admin, "accounts.grant")
    assert permissions.can(owner, "accounts.kick") and permissions.can(owner, "accounts.transfer")
    assert not permissions.can(member, "server.control")
    assert not permissions.can(owner, "no.such.capability")  # a typo fails closed
    assert not permissions.can(0, "server.control")
    assert "server.control" in permissions.capabilities_of(admin)


def test_guests_keep_nothing():
    from kai.core import sleep
    from kai.memory import privacy

    _make("owner")
    member = _make("member")
    guest = _make("guest", level="guest")
    assert not privacy.learning_enabled(guest)
    assert not privacy.patterns_enabled(guest)

    history = [
        {"role": "user", "content": "hello, this exchange is long enough to leave a trail"},
        {"role": "assistant", "content": "hi there"},
    ]
    sleep.save_welcome_back("a note about the guest's visit", guest)
    sleep.checkpoint_session(history, user_id=guest)
    assert not sleep.welcome_back_file(guest).exists()
    assert not sleep.checkpoint_file(guest).exists()

    sleep.checkpoint_session(history, user_id=member)
    assert sleep.checkpoint_file(member).exists()


def test_a_demotion_to_guest_applies_at_once_and_non_accounts_keep_the_default():
    _make("owner")
    member = _make("member")
    assert permissions.keeps_memory(member)
    assert users.update_account(member, level="guest")
    assert not permissions.keeps_memory(member)
    assert permissions.keeps_memory(0)  # the terminal's scratch identity isn't an account


def test_audit_rows_outlive_the_account_they_name():
    assert "audit_log" not in users._USER_TABLES
    owner, doomed = _make("owner"), _make("doomed")
    audit.record(
        "account.kick",
        actor_id=owner,
        actor_name="owner",
        target_id=doomed,
        target_name="doomed",
        detail={"deleted": True},
    )
    get_conn().execute("DELETE FROM users WHERE id = ?", (doomed,))
    get_conn().commit()
    [entry] = audit.list_entries()
    assert entry["action"] == "account.kick" and entry["target_name"] == "doomed"
    assert entry["detail"] == {"deleted": True} and entry["ok"] is True


def test_refused_attempts_are_recorded_as_refused():
    audit.record("account.create", actor_name="admin", detail={"refused": "not allowed"}, ok=False)
    assert audit.list_entries()[0]["ok"] is False


def test_the_terminal_can_hand_ownership_to_the_right_account():
    from kai.apps import cli

    wrong = _make("wrong-owner")
    right = _make("Riley")
    assert cli._set_owner("riley") == 0  # names match case-insensitively, like sign-in
    assert users.get_owner_id() == right and users.get_level(wrong) == "admin"
    assert audit.list_entries()[0]["action"] == "account.transfer_owner"
    assert cli._set_owner("riley") == 0  # already the owner: says so, changes nothing
    assert cli._set_owner("nobody") == 2
