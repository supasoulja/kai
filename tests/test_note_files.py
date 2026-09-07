"""
Note file mirroring + notes repo tests.

notes.save must never lose a note to a disk problem, note files must land in the
configured repo's notes subdir, and a commit must stage *only* that subdir so
Kai can't sweep up unrelated work in the user's repo. A failed git command must
raise, never come back as a success string.

Fast — no Ollama. Uses a temp DB and a real throwaway git repo.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest

import kai.config as cfg

_tmp_db = tempfile.NamedTemporaryFile(suffix="_notefiles.db", delete=False)
_tmp_db.close()
cfg.DB_PATH = Path(_tmp_db.name)

from kai.core._app_state import set_current_user_id  # noqa: E402
from kai.store import note_files as nf  # noqa: E402
from kai.store.db import get_conn  # noqa: E402
from kai.tools.knowledge import notes as notes_tool  # noqa: E402

UID = 4242


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "notesrepo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "Test")
    (r / "README.md").write_text("hello\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "NOTES_DIR", tmp_path / "KaiFiles" / "notes")
    set_current_user_id(UID)
    conn = get_conn()
    conn.execute("DELETE FROM notes WHERE user_id = ?", (UID,))
    conn.commit()
    nf.clear_repo(UID)
    yield
    nf.clear_repo(UID)


# ── Destination ───────────────────────────────────────────────────────────────


def test_defaults_to_notes_dir_when_no_repo():
    assert nf.notes_dir(UID) == Path(cfg.NOTES_DIR)


def test_repo_redirects_notes_dir(repo):
    nf.set_repo(str(repo), user_id=UID)
    assert nf.notes_dir(UID) == repo / cfg.NOTES_REPO_SUBDIR


def test_set_repo_rejects_non_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError, match="Not a git repository"):
        nf.set_repo(str(plain), user_id=UID)


def test_set_repo_rejects_missing_path():
    with pytest.raises(ValueError, match="does not exist"):
        nf.set_repo("/definitely/not/here", user_id=UID)


def test_vanished_repo_falls_back_to_default(repo):
    import shutil

    nf.set_repo(str(repo), user_id=UID)
    shutil.rmtree(repo)
    # A deleted repo must degrade to the default folder, not raise.
    assert nf.get_repo(UID) is None
    assert nf.notes_dir(UID) == Path(cfg.NOTES_DIR)


# ── Writing ───────────────────────────────────────────────────────────────────


def test_save_writes_md_and_txt():
    out = notes_tool.save_note("Body of the note", "My Title")
    assert "Saved note" in out

    files = sorted(Path(cfg.NOTES_DIR).iterdir())
    assert [f.suffix for f in files] == [".md", ".txt"]

    md = next(f for f in files if f.suffix == ".md").read_text()
    assert 'title: "My Title"' in md
    assert "# My Title" in md
    assert "Body of the note" in md

    txt = next(f for f in files if f.suffix == ".txt").read_text()
    assert txt == "Body of the note\n"


def test_filename_is_dated_and_slugged():
    notes_tool.save_note("x", "App Idea: Trip Expense Splitter!")
    name = next(Path(cfg.NOTES_DIR).glob("*.md")).name
    assert "app-idea-trip-expense-splitter" in name
    assert name[:2] == "20"  # leading date


def test_quote_in_title_does_not_break_front_matter():
    notes_tool.save_note("body", 'He said "hi"')
    md = next(Path(cfg.NOTES_DIR).glob("*.md")).read_text()
    # The title line must stay a single well-formed scalar.
    title_line = next(ln for ln in md.splitlines() if ln.startswith("title:"))
    assert title_line == 'title: "He said \\"hi\\""'


def test_note_survives_unwritable_destination(monkeypatch):
    """A disk failure must warn, not report the note as lost — the DB write is
    what counts."""

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(nf, "write_note", boom)
    out = notes_tool.save_note("important thought", "Keep me")
    assert "Saved note" in out
    assert "warning" in out

    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM notes WHERE user_id = ?", (UID,)).fetchone()[0]
    assert n == 1


# ── Git ───────────────────────────────────────────────────────────────────────


def test_commit_writes_note_files_to_repo(repo):
    nf.set_repo(str(repo), user_id=UID)
    notes_tool.save_note("graph problem", "Trip Splitter")

    result = nf.commit_notes("notes: trip splitter", user_id=UID)
    assert "Committed 2 file(s)" in result

    tracked = _git(repo, "ls-files", "notes").stdout.split()
    assert len(tracked) == 2


def test_commit_leaves_unrelated_changes_unstaged(repo):
    nf.set_repo(str(repo), user_id=UID)
    notes_tool.save_note("body", "Note")
    (repo / "README.md").write_text("edited by the user\n")

    nf.commit_notes(user_id=UID)

    # The user's own edit must still be sitting there, unstaged.
    status = _git(repo, "status", "--porcelain").stdout
    assert " M README.md" in status


def test_commit_is_idempotent(repo):
    nf.set_repo(str(repo), user_id=UID)
    notes_tool.save_note("body", "Note")
    nf.commit_notes(user_id=UID)
    assert "Nothing to commit" in nf.commit_notes(user_id=UID)


def test_commit_without_repo_is_a_relayable_message():
    out = nf.commit_notes(user_id=UID)
    assert "No notes repo is configured" in out


def test_failed_git_raises_rather_than_reporting_success(repo, monkeypatch):
    """A failed git command must never be laundered into a success string."""
    nf.set_repo(str(repo), user_id=UID)
    notes_tool.save_note("body", "Note")

    real = nf._git

    def fail(r, *args):
        if args and args[0] == "commit":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="fatal: nope")
        return real(r, *args)

    monkeypatch.setattr(nf, "_git", fail)
    with pytest.raises(RuntimeError, match="git commit failed"):
        nf.commit_notes(user_id=UID)


def test_repo_status_reports_pending(repo):
    nf.set_repo(str(repo), user_id=UID)
    notes_tool.save_note("body", "Note")
    info = nf.repo_status(UID)
    assert info["repo"] == str(repo)
    assert info["file_count"] == 1
    assert info["pending"] == 2  # .md + .txt, both untracked
