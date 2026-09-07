"""
Note files — mirror each saved note to disk as .md + .txt, and commit them.

SQLite stays the source of truth; these files are the human-readable copy. They
land in cfg.NOTES_DIR, or in <repo>/cfg.NOTES_REPO_SUBDIR once the user sets a
notes repo (the `notes_repo` fact). Commits are scoped to the notes subdir so
Kai never stages unrelated work, and nothing is ever pushed.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import kai.config as cfg
from kai.memory import semantic

_FACT_KEY = "notes_repo"
_GIT_TIMEOUT = 30
_MAX_SLUG = 60


# ── Repo setting ──────────────────────────────────────────────────────────────


def get_repo(user_id: int = 0) -> Path | None:
    """The configured notes repo, or None. A repo that has since vanished reads
    as None so notes fall back to the default folder instead of failing."""
    raw = semantic.get_fact(_FACT_KEY, user_id=user_id)
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if (path / ".git").exists() else None


def set_repo(path: str, user_id: int = 0) -> Path:
    """Validate and store the notes repo. Raises ValueError, message shown to the user."""
    cleaned = (path or "").strip()
    if not cleaned:
        raise ValueError("No path given.")
    repo = Path(cleaned).expanduser()
    if not repo.exists():
        raise ValueError(f"Path does not exist: {repo}")
    if not repo.is_dir():
        raise ValueError(f"Not a folder: {repo}")
    if not (repo / ".git").exists():
        raise ValueError(f"Not a git repository (no .git folder): {repo}")
    semantic.set_fact(_FACT_KEY, str(repo.resolve()), source="user_setting", user_id=user_id)
    return repo.resolve()


def clear_repo(user_id: int = 0) -> None:
    semantic.delete_fact(_FACT_KEY, user_id=user_id)


def notes_dir(user_id: int = 0) -> Path:
    """Folder notes are written to — inside the repo when one is configured."""
    repo = get_repo(user_id)
    return repo / cfg.NOTES_REPO_SUBDIR if repo else Path(cfg.NOTES_DIR)


# ── Writing ───────────────────────────────────────────────────────────────────


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:_MAX_SLUG].rstrip("-") or "note"


def _stem(note_id: str, timestamp: str, title: str, content: str) -> str:
    label = title.strip() if title and title.strip() else content[:_MAX_SLUG]
    return f"{timestamp[:10]}-{slugify(label)}-{note_id}"


def _markdown(note_id: str, timestamp: str, title: str, content: str) -> str:
    """YAML front-matter + body, so the .md round-trips back to its DB row."""
    heading = title.strip() if title and title.strip() else "Note"
    # Quote-escape the title so a colon or quote in it can't break the front-matter.
    safe_title = heading.replace('"', '\\"')
    return (
        "---\n"
        f'title: "{safe_title}"\n'
        f"id: {note_id}\n"
        f"created: {timestamp}\n"
        "source: kai\n"
        "---\n\n"
        f"# {heading}\n\n"
        f"{content.strip()}\n"
    )


def write_note(
    note_id: str, timestamp: str, title: str, content: str, user_id: int = 0
) -> list[Path]:
    """Write the .md and .txt pair. Raises OSError on failure."""
    target = notes_dir(user_id)
    target.mkdir(parents=True, exist_ok=True)
    stem = _stem(note_id, timestamp, title, content)

    md = target / f"{stem}.md"
    txt = target / f"{stem}.txt"
    md.write_text(_markdown(note_id, timestamp, title, content), encoding="utf-8")
    txt.write_text(content.strip() + "\n", encoding="utf-8")
    return [md, txt]


def list_files(user_id: int = 0) -> list[Path]:
    """Note files on disk, newest first. Empty if the folder isn't there yet."""
    target = notes_dir(user_id)
    if not target.is_dir():
        return []
    files = [p for p in target.glob("*.md") if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


# ── Git ───────────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
        encoding="utf-8",
        errors="replace",
    )


def commit_notes(message: str = "", user_id: int = 0) -> str:
    """Stage and commit the notes folder in the configured repo. Never pushes.

    Raises RuntimeError if git runs and fails — a failed commit must never come
    back as a success string (see CLAUDE.md).
    """
    repo = get_repo(user_id)
    if repo is None:
        return (
            "No notes repo is configured. Set one in Settings → Notes & repo "
            "(it must be an existing folder with a .git in it)."
        )

    subdir = cfg.NOTES_REPO_SUBDIR
    if not (repo / subdir).is_dir():
        return f"No notes have been written to {repo / subdir} yet — nothing to commit."

    try:
        # Stage only the notes subdir so unrelated edits in the repo stay unstaged.
        add = _git(repo, "add", "--", subdir)
        if add.returncode != 0:
            raise RuntimeError(f"git add failed: {(add.stderr or add.stdout).strip()[:300]}")

        staged = _git(repo, "diff", "--cached", "--name-only", "--", subdir)
        if staged.returncode != 0:
            raise RuntimeError(f"git diff failed: {(staged.stderr or staged.stdout).strip()[:300]}")
        changed = [ln for ln in staged.stdout.splitlines() if ln.strip()]
        if not changed:
            return "Nothing to commit — notes in the repo are already up to date."

        msg = (message or "").strip() or f"notes: {len(changed)} file(s) from Kai"
        commit = _git(repo, "commit", "-m", msg, "--", subdir)
        if commit.returncode != 0:
            raise RuntimeError(
                f"git commit failed: {(commit.stderr or commit.stdout).strip()[:300]}"
            )

        sha = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
        listing = "\n".join(f"  • {c}" for c in changed[:10])
        more = f"\n  … and {len(changed) - 10} more" if len(changed) > 10 else ""
        return f"Committed {len(changed)} file(s) to {repo} [{sha}]\n{listing}{more}"
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Git timed out after {_GIT_TIMEOUT}s in {repo}.") from e
    except FileNotFoundError:
        # Relayable pre-condition, not a failed commit.
        return "Git is not installed or not in PATH. Install from https://git-scm.com/"


def repo_status(user_id: int = 0) -> dict:
    """Repo + notes-folder state for the settings UI."""
    repo = get_repo(user_id)
    raw = semantic.get_fact(_FACT_KEY, user_id=user_id)
    target = notes_dir(user_id)
    info: dict = {
        "repo": str(repo) if repo else None,
        "configured_path": raw,
        "repo_missing": bool(raw) and repo is None,
        "notes_dir": str(target),
        "file_count": len(list_files(user_id)),
        "pending": 0,
        "branch": None,
    }
    if repo is not None:
        try:
            # -uall: without it an untracked notes/ collapses to a single
            # "?? notes/" line and the count under-reports the pending files.
            st = _git(repo, "status", "--porcelain", "-uall", "--", cfg.NOTES_REPO_SUBDIR)
            if st.returncode == 0:
                info["pending"] = len([ln for ln in st.stdout.splitlines() if ln.strip()])
            br = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
            if br.returncode == 0:
                info["branch"] = br.stdout.strip()
        except Exception:
            pass  # decoration only — never break the settings page over it
    return info
