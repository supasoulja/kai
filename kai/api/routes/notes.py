"""Notes browser — list, read, export to disk, and commit to the user's repo.

Backs the Notes tab and the Settings → Notes & repo page. The DB is the listing
source; kai.store.note_files owns everything that touches disk or git.
"""

from fastapi import APIRouter, HTTPException, Request

from kai.api.deps import uid_for
from kai.api.models import NotesRepoRequest
from kai.store import note_files
from kai.store.db import get_conn

router = APIRouter()


@router.get("/api/notes")
async def list_notes(request: Request, limit: int = 100):
    uid = uid_for(request)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, timestamp, title, content FROM notes "
        "WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
        (uid, max(1, min(limit, 500))),
    ).fetchall()

    target = note_files.notes_dir(uid)
    notes = []
    for note_id, ts, title, content in rows:
        stem = note_files._stem(note_id, ts, title or "", content)
        md = target / f"{stem}.md"
        notes.append(
            {
                "id": note_id,
                "timestamp": ts,
                "title": title,
                "preview": content[:160],
                "chars": len(content),
                "md": str(md),
                "txt": str(target / f"{stem}.txt"),
                "on_disk": md.exists(),
            }
        )
    return {"notes": notes, "dir": str(target)}


# ── Repo + disk ───────────────────────────────────────────────────────────────


@router.get("/api/notes/repo/status")
async def repo_status(request: Request):
    return note_files.repo_status(uid_for(request))


@router.post("/api/notes/repo")
async def set_repo(req: NotesRepoRequest, request: Request):
    uid = uid_for(request)
    try:
        repo = note_files.set_repo(req.path, user_id=uid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "repo": str(repo), **note_files.repo_status(uid)}


@router.delete("/api/notes/repo")
async def clear_repo(request: Request):
    uid = uid_for(request)
    note_files.clear_repo(uid)
    return {"ok": True, **note_files.repo_status(uid)}


@router.post("/api/notes/export")
async def export_notes(request: Request):
    """Write every note in the DB to disk. Backfills notes saved before file
    mirroring existed, and re-materializes them after the repo path changes."""
    uid = uid_for(request)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, timestamp, title, content FROM notes WHERE user_id = ? ORDER BY timestamp",
        (uid,),
    ).fetchall()

    written, failed = 0, []
    for note_id, ts, title, content in rows:
        try:
            note_files.write_note(note_id, ts, title or "", content, user_id=uid)
            written += 1
        except OSError as e:
            failed.append(f"{note_id}: {e}")

    return {
        "ok": not failed,
        "written": written,
        "failed": failed,
        "dir": str(note_files.notes_dir(uid)),
    }


@router.post("/api/notes/commit")
async def commit_notes(req: NotesRepoRequest, request: Request):
    uid = uid_for(request)
    try:
        result = note_files.commit_notes(req.message or "", user_id=uid)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "result": result, **note_files.repo_status(uid)}


# ── Single note ───────────────────────────────────────────────────────────────


@router.get("/api/notes/{note_id}")
async def get_note(note_id: str, request: Request):
    uid = uid_for(request)
    conn = get_conn()
    row = conn.execute(
        "SELECT id, timestamp, title, content FROM notes WHERE user_id = ? AND id = ?",
        (uid, note_id),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    nid, ts, title, content = row
    return {"id": nid, "timestamp": ts, "title": title, "content": content}


@router.delete("/api/notes/{note_id}")
async def delete_note(note_id: str, request: Request):
    """Delete a note and its files. Files go first-effort; the DB row is the
    delete that counts."""
    uid = uid_for(request)
    conn = get_conn()
    row = conn.execute(
        "SELECT timestamp, title, content FROM notes WHERE user_id = ? AND id = ?",
        (uid, note_id),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")

    ts, title, content = row
    target = note_files.notes_dir(uid)
    stem = note_files._stem(note_id, ts, title or "", content)
    for ext in (".md", ".txt"):
        try:
            (target / f"{stem}{ext}").unlink(missing_ok=True)
        except OSError:
            pass

    conn.execute("DELETE FROM notes WHERE user_id = ? AND id = ?", (uid, note_id))
    conn.commit()
    return {"ok": True}
