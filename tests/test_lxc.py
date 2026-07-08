"""
lxc.* tool failure-contract tests.

Regression lock for the fabricated-container bug (event log, 2026-06-28): a
failed `incus launch` was reported as success=True, so the classifier never
escalated and the voice model invented a healthy running container. Root cause:
the tool functions discarded _run()'s ok flag and returned the failure text as a
normal value, which the registry wraps as success=True.

These tests assert the fix end to end: a command failure now RAISES, so the
registry marks the result success=False and the classifier flags a hard error —
while the "no client installed" pre-condition stays an informational return.
"""
import os

import pytest

os.environ.setdefault("KAI_TEST_MODE", "1")

import kai.tools.compute.lxc as lxc
from kai.core.engine import TurnEngine
from kai.tools.registry import registry

# A realistic _run failure, shaped exactly like the 2026-06-28 incident.
_FAIL_MSG = '`incus launch ubuntu:22.04 test1` failed: Error: The remote "ubuntu" doesn\'t exist'


@pytest.fixture
def managers_available(monkeypatch):
    """Pretend a container client is installed so we exercise the command path,
    not the _guard() pre-condition — regardless of the test host."""
    monkeypatch.setattr(lxc, "_guard", lambda: None)
    monkeypatch.setattr(lxc, "_find_client", lambda: "incus")


def _fail(*_a, **_k):
    return (False, _FAIL_MSG)


def _ok(out):
    def _inner(*_a, **_k):
        return (True, out)
    return _inner


# ── A failed command must RAISE (so the registry marks it success=False) ─────

@pytest.mark.parametrize("call", [
    lambda: lxc.list_instances(),
    lambda: lxc.instance_info("test1"),
    lambda: lxc.create_instance("test1"),
    lambda: lxc.start_instance("test1"),
    lambda: lxc.stop_instance("test1"),
    lambda: lxc.delete_instance("test1", force=True),
], ids=["list", "info", "create", "start", "stop", "delete"])
def test_command_failure_raises(managers_available, monkeypatch, call):
    monkeypatch.setattr(lxc, "_run", _fail)
    with pytest.raises(RuntimeError) as exc:
        call()
    # The failure detail is preserved for the model to relay honestly.
    assert _FAIL_MSG in str(exc.value)


# ── Success paths return clean, truthful confirmations ───────────────────────

def test_create_success_reports_creation(managers_available, monkeypatch):
    monkeypatch.setattr(lxc, "_run", _ok("Instance test1 started"))
    out = lxc.create_instance("test1", image="images:ubuntu/22.04")
    assert "Created and started container 'test1'" in out


def test_list_empty_says_no_containers(managers_available, monkeypatch):
    monkeypatch.setattr(lxc, "_run", _ok(""))
    assert lxc.list_instances() == "No containers found."


def test_lifecycle_success_confirmations(managers_available, monkeypatch):
    monkeypatch.setattr(lxc, "_run", _ok("ok"))
    assert lxc.start_instance("test1") == "Started 'test1'."
    assert lxc.stop_instance("test1") == "Stopped 'test1'."
    assert lxc.delete_instance("test1", force=True) == "Deleted 'test1'."


# ── "No client installed" stays informational (NOT a raised failure) ─────────

def test_no_client_is_informational_not_a_failure(monkeypatch):
    """A missing container manager is a pre-condition, not a command failure:
    the model should relay the install hint, so this must NOT raise."""
    monkeypatch.setattr(lxc, "_IS_WINDOWS", False)
    monkeypatch.setattr(lxc, "_find_client", lambda: None)
    out = lxc.list_instances()
    assert "No container manager found" in out  # returned as a normal string


# ── End-to-end: failure → registry success=False → classifier hard error ─────

def test_registry_and_classifier_flag_the_failure(managers_available, monkeypatch):
    monkeypatch.setattr(lxc, "_run", _fail)

    # The registry surfaces the raise (Brain._execute_tool turns this into
    # {"success": False, "error": ...}).
    with pytest.raises(RuntimeError):
        registry.execute("lxc.create", {"name": "test1"})

    # And the classifier flags success=False as a hard error → escalation fires.
    hard, win = TurnEngine._classify_tool_result({"success": False, "error": _FAIL_MSG})
    assert hard is True


def test_classifier_cannot_catch_a_laundered_failure():
    """Why the tool MUST raise: if a failure is laundered into success=True with
    the error only in the output text, the classifier is blind to it (Linux
    output-scanning was deliberately rejected as false-positive-prone). This is
    the exact hole the fabricated-container bug fell through — encoded so nobody
    'fixes' it by re-introducing laundering."""
    hard, win = TurnEngine._classify_tool_result(
        {"success": True, "output": _FAIL_MSG}
    )
    assert hard is False  # invisible to the classifier — hence the raise-at-source fix
