"""
workspace.* tools — write, append, and edit files inside C:\\KaiFiles\\.

Kai can ONLY write to the configured WORKSPACE_DIR. Any path that resolves
outside that folder is rejected (see _resolve): leading slashes are stripped so
absolute paths are neutralized to inside the sandbox, and the resolved candidate
must stay under WORKSPACE_DIR — which also defeats ../ traversal and symlinks
pointing out. WORKSPACE_DIR lives outside the source package, so these tools can
never modify Kai's own code or config.

Edits to Kai's source tree / persona are deliberately NOT possible here — those
go only through the confirm-gated self.* tools (self.apply_persona_update), which
surface a reviewable diff before anything is written. See tests/test_workspace_sandbox.py.
"""

import subprocess
from pathlib import Path

import kai.config as cfg
from kai.tools.registry import registry


def _resolve(filename: str) -> Path | None:
    """
    Resolve a filename/relative path inside the workspace.
    Returns None if the resolved path escapes the workspace (path traversal guard).
    """
    workspace = cfg.WORKSPACE_DIR.resolve()
    # Strip any leading slashes/backslashes so the model can't pass absolute paths
    filename = filename.strip().lstrip("/\\")
    candidate = (workspace / filename).resolve()
    try:
        candidate.relative_to(workspace)  # raises ValueError if outside
        return candidate
    except ValueError:
        return None


def _workspace_str() -> str:
    return str(cfg.WORKSPACE_DIR)


# ── files.write ────────────────────────────────────────────────────────────────


@registry.tool(
    name="files.write",
    description=(
        f"Create or overwrite a file in the workspace folder ({_workspace_str()}). "
        "Use this when the user asks to create, save, or write a file — "
        "e.g. 'write a Python script', 'save this as notes.txt', 'create a config file'. "
        "The folder is created automatically if it doesn't exist. "
        "Filenames may include one level of subfolder (e.g. 'scripts/hello.py'). "
        "IMPORTANT: Kai can ONLY write files here — no other locations are allowed."
    ),
    parameters={
        "filename": {
            "type": "string",
            "description": "Filename to write, relative to the workspace (e.g. 'notes.txt', 'scripts/hello.py'). Required.",
            "required": True,
        },
        "content": {
            "type": "string",
            "description": "Full content to write to the file. Required.",
            "required": True,
        },
    },
)
def workspace_write(filename: str, content: str) -> str:
    path = _resolve(filename)
    if path is None:
        return f"Rejected: '{filename}' resolves outside the workspace. Only files inside {_workspace_str()} are allowed."
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        size = len(content.encode("utf-8"))
        return f"Written: {path}  ({size:,} bytes)"
    except Exception as e:
        # A failed write must surface as success=False (raise), never a
        # success-wrapped "Failed to…" string — otherwise the model reports the
        # file as saved when it wasn't. See the lxc fabrication fix.
        raise RuntimeError(f"Failed to write '{path}': {e}") from e


# ── files.append ───────────────────────────────────────────────────────────────


@registry.tool(
    name="files.append",
    description=(
        f"Append text to an existing file in the workspace ({_workspace_str()}). "
        "Use when the user wants to add to a file without replacing what's there — "
        "e.g. 'add this to my log', 'append a line to notes.txt'. "
        "Creates the file if it doesn't exist yet."
    ),
    parameters={
        "filename": {
            "type": "string",
            "description": "Filename to append to, relative to the workspace. Required.",
            "required": True,
        },
        "content": {
            "type": "string",
            "description": "Text to append. A newline is added before it if the file already has content. Required.",
            "required": True,
        },
    },
)
def workspace_append(filename: str, content: str) -> str:
    path = _resolve(filename)
    if path is None:
        return f"Rejected: '{filename}' resolves outside the workspace."
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        separator = "\n" if existing and not existing.endswith("\n") else ""
        path.write_text(existing + separator + content, encoding="utf-8")
        return f"Appended {len(content.encode('utf-8')):,} bytes to {path}"
    except Exception as e:
        raise RuntimeError(f"Failed to append to '{path}': {e}") from e


# ── files.edit ─────────────────────────────────────────────────────────────────


@registry.tool(
    name="files.edit",
    description=(
        f"Find and replace text inside a file in the workspace ({_workspace_str()}). "
        "Use when the user wants to change a specific part of an existing file — "
        "e.g. 'change the port to 8080', 'rename the function', 'fix the typo on line X'. "
        "The old_text must match exactly (including whitespace). "
        "Returns an error if old_text is not found in the file."
    ),
    parameters={
        "filename": {
            "type": "string",
            "description": "Filename to edit, relative to the workspace. Required.",
            "required": True,
        },
        "old_text": {
            "type": "string",
            "description": "Exact text to find in the file. Required.",
            "required": True,
        },
        "new_text": {
            "type": "string",
            "description": "Text to replace it with. Required.",
            "required": True,
        },
        "replace_all": {
            "type": "boolean",
            "description": "Replace every occurrence (default false — only replaces the first match).",
        },
    },
)
def workspace_edit(filename: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
    path = _resolve(filename)
    if path is None:
        return f"Rejected: '{filename}' resolves outside the workspace."
    # Below, every outcome where the edit did NOT happen raises → success=False,
    # so the model can't report "done" for an edit it never made (the lxc lesson).
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    try:
        original = path.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"Failed to read '{path}' for editing: {e}") from e
    if old_text not in original:
        # Give a helpful snippet of what's actually in the file.
        preview = original[:300].replace("\n", "↵")
        raise ValueError(
            f"Text not found in '{path}'.\nold_text was: {old_text!r}\nFile starts with: {preview}"
        )
    if replace_all:
        updated = original.replace(old_text, new_text)
        count = original.count(old_text)
    else:
        updated = original.replace(old_text, new_text, 1)
        count = 1
    try:
        path.write_text(updated, encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"Failed to write edit to '{path}': {e}") from e
    return f"Replaced {count} occurrence(s) in {path}"


# ── workspace.git_clone ────────────────────────────────────────────────────────


def _normalize_url(url: str) -> str:
    """Strip trailing slash and .git for comparison."""
    return url.rstrip("/").removesuffix(".git").lower()


def _is_allowed(url: str) -> bool:
    norm = _normalize_url(url)
    return any(norm == _normalize_url(u) for u in cfg.ALLOWED_GIT_REPOS)


@registry.tool(
    name="workspace.git_clone",
    description=(
        f"Clone a pre-approved Git repository into the workspace folder ({_workspace_str()}). "
        "Only repos explicitly added to the allowlist by the user can be cloned — "
        "Kai cannot clone arbitrary URLs. "
        "Use workspace.git_list_allowed to see which repos are permitted. "
        "The repo lands in a subfolder named after the repo (or a custom name)."
    ),
    parameters={
        "url": {
            "type": "string",
            "description": "Full Git repository URL — must be on the allowlist. Required.",
            "required": True,
        },
        "folder_name": {
            "type": "string",
            "description": "Subfolder name inside the workspace to clone into (default: repo name from URL).",
        },
    },
)
def workspace_git_clone(url: str, folder_name: str = "") -> str:
    url = url.strip()
    if not _is_allowed(url):
        allowed = "\n".join(f"  • {u}" for u in cfg.ALLOWED_GIT_REPOS) or "  (none)"
        return (
            f"'{url}' is not on the allowlist.\n"
            f"Allowed repos:\n{allowed}\n"
            "Add the URL to ALLOWED_GIT_REPOS in config.py to grant access."
        )

    # Derive folder name from the repo slug in the URL
    if not folder_name:
        slug = url.rstrip("/").split("/")[-1]
        folder_name = slug[:-4] if slug.endswith(".git") else slug

    target = _resolve(folder_name)
    if target is None:
        return f"Rejected: '{folder_name}' resolves outside the workspace."
    if target.exists():
        return (
            f"Folder already exists: {target}. "
            "Use workspace.git_pull to update it, or provide a different folder_name."
        )

    try:
        cfg.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "clone", url, str(target)],
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            "Git clone timed out (120s). The repo may be very large or the connection slow."
        ) from e
    except FileNotFoundError:
        # Git missing is a relayable pre-condition (like lxc's "no client"), not a
        # failed clone — return it as guidance the model passes on, not an error.
        return "Git is not installed or not in PATH. Install from https://git-scm.com/"
    except Exception as e:
        raise RuntimeError(f"Failed to clone: {e}") from e
    # The clone command ran but exited non-zero — it did not clone. Raise so the
    # result is success=False, never a success-wrapped "Git clone failed" string.
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"Git clone failed: {err[:500]}")
    try:
        file_count = sum(1 for f in target.rglob("*") if f.is_file())
    except Exception:
        file_count = "?"
    return f"Cloned {url} → {target}  ({file_count} files)"


# ── workspace.git_pull ─────────────────────────────────────────────────────────


@registry.tool(
    name="workspace.git_pull",
    description=(
        f"Update (git pull) an already-cloned repository in the workspace ({_workspace_str()}). "
        "Use when the user wants to update or sync a repo they previously cloned. "
        "Pass the folder name of the cloned repo (e.g. 'Python-Scripts')."
    ),
    parameters={
        "folder_name": {
            "type": "string",
            "description": "Subfolder name of the cloned repo inside the workspace. Required.",
            "required": True,
        },
    },
)
def workspace_git_pull(folder_name: str) -> str:
    target = _resolve(folder_name)
    if target is None:
        return f"Rejected: '{folder_name}' resolves outside the workspace."
    if not target.exists():
        return f"Folder not found: {target}. Clone it first with workspace.git_clone."
    if not (target / ".git").exists():
        return f"'{target}' is not a Git repository (no .git folder found)."

    try:
        result = subprocess.run(
            ["git", "-C", str(target), "pull"],
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("Git pull timed out (60s).") from e
    except FileNotFoundError:
        return "Git is not installed or not in PATH."  # relayable pre-condition
    except Exception as e:
        raise RuntimeError(f"Failed to pull: {e}") from e
    out = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(f"Git pull failed:\n{out[:500]}")
    return f"Updated {target}:\n{out[:500]}"


# ── workspace.git_list_allowed ─────────────────────────────────────────────────


@registry.tool(
    name="workspace.git_list_allowed",
    description=(
        "Show the list of Git repositories Kai is allowed to clone. "
        "Use this when the user asks which repos are available, or before "
        "attempting a clone to confirm the URL is permitted."
    ),
    parameters={},
)
def workspace_git_list_allowed() -> str:
    repos = cfg.ALLOWED_GIT_REPOS
    if not repos:
        return "No repos are currently allowed. Add URLs to ALLOWED_GIT_REPOS in config.py to grant access."
    lines = ["Allowed Git repos:"]
    for url in repos:
        lines.append(f"  • {url}")
    return "\n".join(lines)
