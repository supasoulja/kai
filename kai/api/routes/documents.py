"""Document RAG — upload (extract/chunk/embed), list, and delete documents."""
import asyncio
import logging

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from kai.api.deps import uid_for
from kai.api.state import brain_for

_log = logging.getLogger(__name__)

router = APIRouter()

_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB max upload size


@router.post("/docs/upload")
async def upload_doc(file: UploadFile = File(...), request: Request = None):  # noqa: B008  (FastAPI file-upload idiom)
    """Ingest an uploaded document: extract text, chunk, embed, store."""
    import tempfile
    from pathlib import Path

    from kai.memory import documents as _docs

    # Content-Length is untrusted (client-controlled) — use as a fast early-reject only.
    # The read loop below is the actual enforcement and cannot be bypassed.
    content_length = request.headers.get("content-length") if request else None
    if content_length and int(content_length) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum upload size is {_MAX_UPLOAD_BYTES // (1024*1024)} MB.",
        )

    uid = uid_for(request)
    brain = brain_for(request)

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _docs.ALLOWED_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported type '{suffix}'. Allowed: {', '.join(sorted(_docs.ALLOWED_TYPES))}",
        )

    # Save stream to a temp file with size enforcement
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        bytes_written = 0
        while True:
            chunk = file.file.read(65536)
            if not chunk:
                break
            bytes_written += len(chunk)
            if bytes_written > _MAX_UPLOAD_BYTES:
                tmp_path = Path(tmp.name)
                tmp_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Maximum upload size is {_MAX_UPLOAD_BYTES // (1024*1024)} MB.",
                )
            tmp.write(chunk)
        tmp_path = Path(tmp.name)

    try:
        embed_fn = brain.get_embed_fn()
        # Text extraction + chunking + embedding is heavy, blocking work — run it
        # off the event loop so it can't freeze in-flight chat streaming.
        meta = await asyncio.to_thread(
            _docs.ingest, tmp_path, embed_fn=embed_fn,
            original_name=file.filename, user_id=uid,
        )

        # Inject the upload as a message in the conversation history
        upload_note = (
            f"[Document uploaded: {file.filename} — "
            f"{meta.get('chunk_count', '?')} chunks, "
            f"{meta.get('char_count', '?')} chars]"
        )
        brain.append_external_turn("user", upload_note)

        return {"ok": True, **meta}
    except ValueError as e:
        # ValueError is raised intentionally by _extract_text for known-bad input;
        # safe to surface the message (it's ours, not a library traceback).
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception:
        # Log the real error server-side; never expose library tracebacks to client
        _log.exception("Document ingestion failed")
        raise HTTPException(status_code=500, detail="Document ingestion failed. Check the server log for details.") from None
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("/docs/list")
async def list_docs(request: Request):
    from kai.memory import documents as _docs
    uid = uid_for(request)
    return _docs.list_documents(user_id=uid)


@router.delete("/docs/{doc_id}")
async def delete_doc(doc_id: str, request: Request):
    from kai.memory import documents as _docs
    uid = uid_for(request)
    ok = _docs.delete_document(doc_id, user_id=uid)
    if not ok:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"ok": True, "deleted": doc_id}
