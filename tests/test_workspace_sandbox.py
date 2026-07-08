"""
Workspace write-sandbox tests.

files.write / files.append / files.edit must only ever touch files inside
cfg.WORKSPACE_DIR — never Kai's own source tree or anywhere else on disk.
Source-tree / config edits are reserved for the confirm-gated self.* tools.
This locks the containment guard (`workspace_tools._resolve`) against the three
escape vectors: path traversal, absolute paths, and symlinks pointing out.

Fast — no Ollama, no DB.
"""
import os
from pathlib import Path

import pytest

import kai.config as cfg
from kai.tools.files import workspace_tools as w


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "KaiFiles"
    ws.mkdir()
    monkeypatch.setattr(cfg, "WORKSPACE_DIR", ws)
    return ws


def test_normal_relative_path_resolves_inside(workspace):
    p = w._resolve("notes.txt")
    assert p is not None
    assert p == (workspace / "notes.txt").resolve()


def test_subfolder_path_allowed(workspace):
    p = w._resolve("scripts/hello.py")
    assert p is not None
    assert str(p).startswith(str(workspace.resolve()))


def test_dotdot_traversal_rejected(workspace):
    assert w._resolve("../../etc/passwd") is None
    assert w._resolve("../escape.txt") is None


def test_absolute_path_neutralized_not_escaped(workspace):
    # Leading slashes are stripped, so an absolute path is treated as relative
    # and lands INSIDE the workspace — it can never reach the real /etc/passwd.
    p = w._resolve("/etc/passwd")
    assert p is not None
    assert str(p).startswith(str(workspace.resolve()))
    assert p != Path("/etc/passwd")


def test_symlink_escape_rejected(workspace):
    # A symlink inside the workspace that points outside must not be a backdoor.
    link = workspace / "link"
    os.symlink("/etc", link)
    assert w._resolve("link/passwd") is None


def test_write_outside_is_refused(workspace):
    result = w.workspace_write("../escape.txt", "owned")
    assert "Rejected" in result
    assert not (workspace.parent / "escape.txt").exists()


def test_write_inside_succeeds(workspace):
    result = w.workspace_write("hello.txt", "hi")
    assert "Written" in result
    assert (workspace / "hello.txt").read_text() == "hi"


def test_edit_cannot_touch_source_tree(workspace):
    # Even naming a real source file by traversal is refused before any read.
    result = w.workspace_edit("../../kai/config.py", "FLOW_TRACE = True", "FLOW_TRACE = False")
    assert "Rejected" in result


# ── Failure contract: an action that did NOT happen must raise (success=False) ──
# Regression lock alongside test_lxc.py — a failed write/edit/git must never come
# back as a success-wrapped "Failed to…" string, or the model reports work it
# never did (the fabrication class the lxc fix closed).

def test_edit_missing_file_raises(workspace):
    with pytest.raises(FileNotFoundError):
        w.workspace_edit("nope.txt", "a", "b")


def test_edit_text_not_found_raises(workspace):
    w.workspace_write("f.txt", "hello world")
    with pytest.raises(ValueError) as exc:
        w.workspace_edit("f.txt", "not present", "x")
    assert "Text not found" in str(exc.value)
    # The file is untouched — nothing was replaced.
    assert (workspace / "f.txt").read_text() == "hello world"


def test_write_failure_raises(workspace, monkeypatch):
    # Simulate an OS-level write failure (disk full / permissions).
    def boom(*_a, **_k):
        raise OSError("No space left on device")
    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(RuntimeError) as exc:
        w.workspace_write("x.txt", "data")
    assert "Failed to write" in str(exc.value)


def test_git_clone_nonzero_exit_raises(workspace, monkeypatch):
    monkeypatch.setattr(cfg, "ALLOWED_GIT_REPOS", ["https://example.com/repo.git"])

    class _R:
        returncode = 128
        stdout = ""
        stderr = "fatal: repository not found"
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: _R())
    with pytest.raises(RuntimeError) as exc:
        w.workspace_git_clone("https://example.com/repo.git")
    assert "Git clone failed" in str(exc.value)


def test_git_clone_not_allowed_stays_informational(workspace):
    # A disallowed URL is a pre-condition the model relays — NOT a raised failure.
    out = w.workspace_git_clone("https://evil.example/repo.git")
    assert "not on the allowlist" in out


def test_registry_marks_workspace_failure_unsuccessful(workspace, monkeypatch):
    """End-to-end: a failed edit raises through registry.execute → the engine
    wraps it success=False → the classifier flags a hard error."""
    from kai.core.engine import TurnEngine
    from kai.tools.registry import registry

    def _wrap(name, args):
        try:
            return {"success": True, "output": registry.execute(name, args)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    res = _wrap("files.edit", {"filename": "ghost.txt", "old_text": "a", "new_text": "b"})
    assert res["success"] is False
    hard, _ = TurnEngine._classify_tool_result(res)
    assert hard is True
