"""
sandbox.* tools — safe file management with proposal→approval gating.

Read-only tools (files.list, files.read, etc.) work everywhere.
Write tools (files.write, files.edit, files.append) are locked to KaiFiles.

Sandbox tools bridge the gap: move, copy, rename, delete files ANYWHERE the
user has access — but destructive ops go through a two-step flow:
  1. Kai proposes the operation → user sees exactly what will happen
  2. User approves → Kai executes via sandbox.approve

Protected system paths are permanently blocked — no proposal, no override.
"""
import shutil
import time
import uuid
from pathlib import Path

from kai.tools.registry import registry
from kai.db import get_conn
import kai.config as cfg

# ── Protected paths (never touched, period) ──────────────────────────────────

_PROTECTED_PATHS = {
    "windows", "system32", "syswow64", "winsxs",
    "system volume information", "$recycle.bin", "recovery",
    "programdata", "boot", "efi",
}

_PROTECTED_FILES = {
    "bootmgr", "ntldr", "pagefile.sys", "swapfile.sys", "hiberfil.sys",
    "ntdetect.com", "boot.ini", "autoexec.bat", "config.sys",
}

_PROTECTED_EXTENSIONS = {".sys", ".dll", ".drv"}

# These top-level dirs are read-only (can copy FROM, never write/move/delete INTO)
_READ_ONLY_ROOTS = {
    "windows", "program files", "program files (x86)", "programdata",
}


def _is_protected(p: Path) -> str | None:
    """Return a reason string if the path is protected, else None."""
    resolved = p.resolve()
    parts_lower = [part.lower() for part in resolved.parts]

    for part in parts_lower:
        if part in _PROTECTED_PATHS:
            return f"'{resolved}' is inside a protected system directory ({part})"

    if resolved.name.lower() in _PROTECTED_FILES:
        return f"'{resolved.name}' is a protected system file"

    if resolved.suffix.lower() in _PROTECTED_EXTENSIONS:
        return f"'{resolved.name}' has a protected system extension ({resolved.suffix})"

    # Block writes to drive root files (C:\something.exe)
    if len(resolved.parts) <= 3 and resolved.is_file():
        drive = resolved.parts[0] if resolved.parts else ""
        if drive.upper() in ("C:\\", "C:/"):
            return f"Cannot modify files at the drive root ({resolved})"

    return None


def _is_read_only_root(p: Path) -> str | None:
    """Check if path falls under a read-only root directory."""
    resolved = p.resolve()
    if len(resolved.parts) >= 2:
        top = resolved.parts[1].lower() if len(resolved.parts) > 1 else ""
        if top in _READ_ONLY_ROOTS:
            return f"'{top}' is read-only — you can copy files out of it but not modify it"
    return None


# ── Proposal store (in-memory, short-lived) ──────────────────────────────────

_pending: dict[str, dict] = {}  # proposal_id → {op, source, dest, created}
_PROPOSAL_TTL = 300  # 5 minutes


def _clean_expired():
    now = time.time()
    expired = [k for k, v in _pending.items() if now - v["created"] > _PROPOSAL_TTL]
    for k in expired:
        del _pending[k]


def _create_proposal(op: str, source: str, dest: str = "") -> dict:
    _clean_expired()
    pid = uuid.uuid4().hex[:8]
    entry = {
        "op": op,
        "source": source,
        "dest": dest,
        "created": time.time(),
    }
    _pending[pid] = entry
    return {"proposal_id": pid, **entry}


# ── Audit log ─────────────────────────────────────────────────────────────────

def _ensure_table():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sandbox_log (
            id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            operation TEXT NOT NULL,
            source TEXT NOT NULL,
            dest TEXT DEFAULT '',
            status TEXT NOT NULL,
            user_id INTEGER DEFAULT 0
        )
    """)
    conn.commit()


def _log_op(op: str, source: str, dest: str, status: str, user_id: int = 0):
    _ensure_table()
    conn = get_conn()
    conn.execute(
        "INSERT INTO sandbox_log (id, timestamp, operation, source, dest, status, user_id) "
        "VALUES (?, datetime('now'), ?, ?, ?, ?, ?)",
        (uuid.uuid4().hex[:12], op, source, dest, status, user_id),
    )
    conn.commit()


# ── Tools ─────────────────────────────────────────────────────────────────────

@registry.tool(
    name="sandbox.copy_to_workspace",
    description=(
        "Copy a file or folder into KaiFiles (the workspace). "
        "This is always safe — it reads from the source and writes a copy to KaiFiles. "
        "The original is never modified. Use this to bring files into Kai's workspace "
        "for safe editing, analysis, or organization."
    ),
    parameters={
        "source": {
            "type": "string",
            "description": "Full path to the file or folder to copy. Required.",
            "required": True,
        },
        "dest_name": {
            "type": "string",
            "description": "Name or relative path inside KaiFiles for the copy (default: same filename).",
        },
    },
)
def copy_to_workspace(source: str, dest_name: str = "") -> str:
    src = Path(source.strip().strip("'\"")).expanduser().resolve()
    if not src.exists():
        return f"Source not found: {src}"

    name = dest_name.strip().lstrip("/\\") if dest_name.strip() else src.name
    dest = (cfg.WORKSPACE_DIR / name).resolve()

    # Ensure dest stays inside workspace
    try:
        dest.relative_to(cfg.WORKSPACE_DIR.resolve())
    except ValueError:
        return f"Destination must be inside {cfg.WORKSPACE_DIR}"

    try:
        cfg.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dest.exists():
                return f"Destination already exists: {dest}. Choose a different dest_name."
            shutil.copytree(src, dest)
            count = sum(1 for _ in dest.rglob("*") if _.is_file())
            _log_op("copy_to_workspace", str(src), str(dest), "ok")
            return f"Copied folder → {dest}  ({count} files)"
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            size = dest.stat().st_size
            sz = f"{size/1_048_576:.1f} MB" if size >= 1_048_576 else f"{size/1024:.1f} KB"
            _log_op("copy_to_workspace", str(src), str(dest), "ok")
            return f"Copied → {dest}  ({sz})"
    except PermissionError:
        return f"Permission denied reading {src}"
    except Exception as e:
        return f"Copy failed: {e}"


@registry.tool(
    name="sandbox.propose_move",
    description=(
        "Propose moving a file or folder to a new location. "
        "This does NOT execute the move — it creates a proposal that the user must approve. "
        "Show the proposal details to the user and ask for confirmation. "
        "Then call sandbox.approve with the proposal_id to execute it. "
        "Protected system files and directories cannot be moved."
    ),
    parameters={
        "source": {
            "type": "string",
            "description": "Full path to the file or folder to move. Required.",
            "required": True,
        },
        "dest": {
            "type": "string",
            "description": "Full destination path (new location). Required.",
            "required": True,
        },
    },
)
def propose_move(source: str, dest: str) -> str:
    src = Path(source.strip().strip("'\"")).expanduser().resolve()
    dst = Path(dest.strip().strip("'\"")).expanduser().resolve()

    if not src.exists():
        return f"Source not found: {src}"

    reason = _is_protected(src)
    if reason:
        return f"BLOCKED: {reason}. This path cannot be moved."

    reason = _is_protected(dst)
    if reason:
        return f"BLOCKED: {reason}. Cannot move files into a protected location."

    reason = _is_read_only_root(src)
    if reason:
        return f"BLOCKED: {reason}. Use sandbox.copy_to_workspace instead."

    reason = _is_read_only_root(dst)
    if reason:
        return f"BLOCKED: {reason}."

    if dst.exists():
        return f"Destination already exists: {dst}. Choose a different path or rename first."

    proposal = _create_proposal("move", str(src), str(dst))

    is_dir = src.is_dir()
    if is_dir:
        count = sum(1 for _ in src.rglob("*") if _.is_file())
        what = f"folder ({count} files)"
    else:
        size = src.stat().st_size
        what = f"file ({size/1_048_576:.1f} MB)" if size >= 1_048_576 else f"file ({size/1024:.1f} KB)"

    return (
        f"PROPOSAL [{proposal['proposal_id']}] — MOVE {what}\n"
        f"  From: {src}\n"
        f"  To:   {dst}\n\n"
        f"Show this to the user and ask for approval.\n"
        f"To execute: call sandbox.approve with proposal_id='{proposal['proposal_id']}'"
    )


@registry.tool(
    name="sandbox.propose_delete",
    description=(
        "Propose deleting a file or folder. "
        "This does NOT delete anything — it creates a proposal that the user must approve. "
        "Show the proposal details to the user and ask for confirmation. "
        "Then call sandbox.approve with the proposal_id to execute it. "
        "Deleted files are moved to a trash folder in KaiFiles, not permanently destroyed. "
        "Protected system files and directories cannot be deleted."
    ),
    parameters={
        "source": {
            "type": "string",
            "description": "Full path to the file or folder to delete. Required.",
            "required": True,
        },
    },
)
def propose_delete(source: str) -> str:
    src = Path(source.strip().strip("'\"")).expanduser().resolve()

    if not src.exists():
        return f"Not found: {src}"

    reason = _is_protected(src)
    if reason:
        return f"BLOCKED: {reason}. This path cannot be deleted."

    reason = _is_read_only_root(src)
    if reason:
        return f"BLOCKED: {reason}."

    proposal = _create_proposal("delete", str(src))

    is_dir = src.is_dir()
    if is_dir:
        count = sum(1 for _ in src.rglob("*") if _.is_file())
        total_size = sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
        sz = f"{total_size/1_048_576:.1f} MB" if total_size >= 1_048_576 else f"{total_size/1024:.1f} KB"
        what = f"folder ({count} files, {sz} total)"
    else:
        size = src.stat().st_size
        sz = f"{size/1_048_576:.1f} MB" if size >= 1_048_576 else f"{size/1024:.1f} KB"
        what = f"file ({sz})"

    return (
        f"PROPOSAL [{proposal['proposal_id']}] — DELETE {what}\n"
        f"  Target: {src}\n"
        f"  Safety: file will be moved to {cfg.WORKSPACE_DIR / '.trash'}, not permanently destroyed\n\n"
        f"Show this to the user and ask for approval.\n"
        f"To execute: call sandbox.approve with proposal_id='{proposal['proposal_id']}'"
    )


@registry.tool(
    name="sandbox.propose_rename",
    description=(
        "Propose renaming a file or folder (same location, new name). "
        "This does NOT rename anything — it creates a proposal that the user must approve. "
        "Protected system files cannot be renamed."
    ),
    parameters={
        "source": {
            "type": "string",
            "description": "Full path to the file or folder to rename. Required.",
            "required": True,
        },
        "new_name": {
            "type": "string",
            "description": "New filename (just the name, not a full path). Required.",
            "required": True,
        },
    },
)
def propose_rename(source: str, new_name: str) -> str:
    src = Path(source.strip().strip("'\"")).expanduser().resolve()
    new_name = new_name.strip().strip("/\\")

    if not src.exists():
        return f"Not found: {src}"
    if "/" in new_name or "\\" in new_name:
        return "new_name must be just a filename, not a path. Use sandbox.propose_move for moving."

    reason = _is_protected(src)
    if reason:
        return f"BLOCKED: {reason}."

    reason = _is_read_only_root(src)
    if reason:
        return f"BLOCKED: {reason}."

    dst = src.parent / new_name
    if dst.exists():
        return f"A file named '{new_name}' already exists in {src.parent}"

    proposal = _create_proposal("rename", str(src), str(dst))
    return (
        f"PROPOSAL [{proposal['proposal_id']}] — RENAME\n"
        f"  From: {src.name}\n"
        f"  To:   {new_name}\n"
        f"  In:   {src.parent}\n\n"
        f"Show this to the user and ask for approval.\n"
        f"To execute: call sandbox.approve with proposal_id='{proposal['proposal_id']}'"
    )


@registry.tool(
    name="sandbox.approve",
    description=(
        "Execute a previously proposed file operation (move, delete, rename). "
        "ONLY call this after the user has seen the proposal and explicitly approved it. "
        "Proposals expire after 5 minutes."
    ),
    parameters={
        "proposal_id": {
            "type": "string",
            "description": "The proposal ID from a previous sandbox.propose_* call. Required.",
            "required": True,
        },
    },
)
def approve_proposal(proposal_id: str) -> str:
    _clean_expired()
    pid = proposal_id.strip()
    if pid not in _pending:
        return f"Proposal '{pid}' not found or expired (proposals last 5 minutes). Create a new one."

    p = _pending.pop(pid)
    op = p["op"]
    source = Path(p["source"])
    dest = Path(p["dest"]) if p["dest"] else None

    # Re-validate protection (in case something changed)
    reason = _is_protected(source)
    if reason:
        _log_op(op, str(source), p["dest"], f"blocked: {reason}")
        return f"BLOCKED: {reason}"

    try:
        if op == "move":
            if not source.exists():
                return f"Source no longer exists: {source}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(dest))
            _log_op("move", str(source), str(dest), "ok")
            return f"Moved: {source} → {dest}"

        elif op == "delete":
            if not source.exists():
                return f"Already gone: {source}"
            trash = cfg.WORKSPACE_DIR / ".trash"
            trash.mkdir(parents=True, exist_ok=True)
            trash_dest = trash / f"{source.name}_{int(time.time())}"
            shutil.move(str(source), str(trash_dest))
            _log_op("delete", str(source), str(trash_dest), "trashed")
            return f"Moved to trash: {source} → {trash_dest}\n(Recoverable from {trash})"

        elif op == "rename":
            if not source.exists():
                return f"Source no longer exists: {source}"
            source.rename(dest)
            _log_op("rename", str(source), str(dest), "ok")
            return f"Renamed: {source.name} → {dest.name}"

        else:
            return f"Unknown operation: {op}"

    except PermissionError:
        _log_op(op, str(source), p["dest"], "permission_denied")
        return f"Permission denied. The file may be in use or require admin access."
    except Exception as e:
        _log_op(op, str(source), p["dest"], f"error: {e}")
        return f"Operation failed: {e}"


@registry.tool(
    name="sandbox.history",
    description=(
        "Show recent sandbox operations (moves, deletes, renames, copies). "
        "Use to check what file operations Kai has performed."
    ),
    parameters={
        "limit": {
            "type": "integer",
            "description": "Number of recent entries to show (default 10).",
        },
    },
)
def sandbox_history(limit: int = 10) -> str:
    _ensure_table()
    conn = get_conn()
    rows = conn.execute(
        "SELECT timestamp, operation, source, dest, status "
        "FROM sandbox_log ORDER BY timestamp DESC LIMIT ?",
        (max(1, min(int(limit), 50)),),
    ).fetchall()
    if not rows:
        return "No sandbox operations recorded yet."
    lines = ["Recent sandbox operations:"]
    for ts, op, src, dst, status in reversed(rows):
        entry = f"  [{ts}] {op}: {src}"
        if dst:
            entry += f" → {dst}"
        entry += f"  ({status})"
        lines.append(entry)
    return "\n".join(lines)
