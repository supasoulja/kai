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
