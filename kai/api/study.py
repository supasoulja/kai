"""
Study mode — discover and save legitimately free/open-access papers and books.

All sources are open-access; no paywall bypass — just aggregating resources that
exist but aren't widely known. Mounted by web.py via include_router.
"""

import asyncio
import threading
import urllib.request as _urlreq
import uuid as _uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

import kai.config as cfg
from kai.api.deps import get_user
from kai.api.models import (
    StudyAskRequest,
    StudyDownloadRequest,
    StudyFindFreeRequest,
    StudySearchRequest,
)

router = APIRouter()


# Curated catalog of legitimately free knowledge sources — the map most people never see.
_STUDY_COLLECTIONS = {
    "Research Papers": [
        {
            "name": "arXiv",
            "desc": "Free preprints in physics, math, CS, economics, biology",
            "url": "https://arxiv.org",
        },
        {
            "name": "PubMed Central",
            "desc": "NIH-mandated free access to biomedical research",
            "url": "https://www.ncbi.nlm.nih.gov/pmc/",
        },
        {
            "name": "Semantic Scholar",
            "desc": "Free academic graph with PDF links, 200M+ papers",
            "url": "https://www.semanticscholar.org",
        },
        {
            "name": "CORE",
            "desc": "200M+ full-text open-access papers from global repos (free API key at core.ac.uk)",
            "url": "https://core.ac.uk",
        },
        {
            "name": "DOAJ",
            "desc": "Directory of Open Access Journals — peer-reviewed, free",
            "url": "https://doaj.org",
        },
        {
            "name": "BASE",
            "desc": "Bielefeld Academic Search Engine — 300M+ open documents",
            "url": "https://www.base-search.net",
        },
        {
            "name": "SciELO",
            "desc": "Latin America & Spain's entire scientific output — unknown outside the region",
            "url": "https://scielo.org",
        },
        {
            "name": "African Journals Online",
            "desc": "Africa's scientific literature — another massive blind spot in Western search",
            "url": "https://www.ajol.info",
        },
        {
            "name": "Unpaywall",
            "desc": "Find the legal free copy of any paper by DOI (checks 50k+ repos)",
            "url": "https://unpaywall.org",
        },
        {
            "name": "Open Access Button",
            "desc": "Like Unpaywall + author direct request — gets papers Unpaywall misses",
            "url": "https://openaccessbutton.org",
        },
        {
            "name": "Europe PMC",
            "desc": "European counterpart to PubMed Central — Wellcome Trust, UKRI mandated papers",
            "url": "https://europepmc.org",
        },
    ],
    "Books": [
        {
            "name": "Project Gutenberg",
            "desc": "70,000+ public domain books as free epub/pdf",
            "url": "https://www.gutenberg.org",
        },
        {
            "name": "Standard Ebooks",
            "desc": "Polished, carefully proofread public domain epubs",
            "url": "https://standardebooks.org",
        },
        {
            "name": "Open Library",
            "desc": "Internet Archive digital lending + public domain books",
            "url": "https://openlibrary.org",
        },
        {
            "name": "HathiTrust",
            "desc": "17M+ scanned books — public domain items free to download",
            "url": "https://www.hathitrust.org",
        },
    ],
    "Textbooks": [
        {
            "name": "OpenStax",
            "desc": "Free peer-reviewed college textbooks (CC licensed)",
            "url": "https://openstax.org",
        },
        {
            "name": "LibreTexts",
            "desc": "Free open textbooks across every STEM and humanities field",
            "url": "https://libretexts.org",
        },
        {
            "name": "Open Textbook Library",
            "desc": "Peer-reviewed free textbooks for higher ed",
            "url": "https://open.umn.edu/opentextbooks",
        },
        {
            "name": "BC Campus OpenEd",
            "desc": "Curated open textbooks, many with epub downloads",
            "url": "https://open.bccampus.ca",
        },
    ],
    "Courses": [
        {
            "name": "MIT OpenCourseWare",
            "desc": "Full MIT course materials, free forever",
            "url": "https://ocw.mit.edu",
        },
        {
            "name": "Khan Academy",
            "desc": "Free K-12 and college-level courses",
            "url": "https://www.khanacademy.org",
        },
        {
            "name": "OpenLearn (Open Univ.)",
            "desc": "Free courses from The Open University UK",
            "url": "https://www.open.edu/openlearn/",
        },
    ],
    "Government & Policy": [
        {
            "name": "NASA Technical Reports",
            "desc": "All NASA research, free to the public",
            "url": "https://ntrs.nasa.gov",
        },
        {
            "name": "NIH Research Portfolio",
            "desc": "Federally funded biomedical research results",
            "url": "https://reporter.nih.gov",
        },
        {
            "name": "Congressional Research Service",
            "desc": "In-depth policy reports for Congress, now public",
            "url": "https://crsreports.congress.gov",
        },
        {
            "name": "NIST Publications",
            "desc": "Technical standards and research from NIST",
            "url": "https://www.nist.gov/publications",
        },
        {
            "name": "GovInfo",
            "desc": "Official U.S. government publications and legal records",
            "url": "https://www.govinfo.gov",
        },
    ],
    "Law": [
        {
            "name": "Cornell LII",
            "desc": "Free U.S. law: Constitution, statutes, regulations, case law",
            "url": "https://www.law.cornell.edu",
        },
        {
            "name": "CourtListener",
            "desc": "Free Law Project — 4M+ court opinions, free to search",
            "url": "https://www.courtlistener.com",
        },
        {
            "name": "Google Scholar (Cases)",
            "desc": "Full text of court opinions, freely searchable",
            "url": "https://scholar.google.com",
        },
    ],
    "History & Culture": [
        {
            "name": "Library of Congress Digital",
            "desc": "Millions of historical documents, photos, maps",
            "url": "https://www.loc.gov/collections/",
        },
        {
            "name": "Smithsonian Open Access",
            "desc": "4.4M CC0-licensed images and media",
            "url": "https://www.si.edu/openaccess",
        },
        {
            "name": "Europeana",
            "desc": "50M+ digitized cultural heritage items from European institutions",
            "url": "https://www.europeana.eu",
        },
        {
            "name": "Internet Archive",
            "desc": "330B web pages, 40M books, 14M videos — all free",
            "url": "https://archive.org",
        },
    ],
}


@router.get("/study/collections")
async def study_collections(request: Request):
    if not get_user(request):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    return {"collections": _STUDY_COLLECTIONS}


@router.post("/study/search")
async def study_search(req: StudySearchRequest, request: Request):
    if not get_user(request):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    from kai.tools.knowledge.study import search_books, search_papers

    def _search() -> str:
        # Blocking HTTP to external open-access catalogs — keep it off the loop.
        text = ""
        if req.filter in ("all", "papers"):
            text += search_papers(req.query) + "\n\n"
        if req.filter in ("all", "books"):
            text += search_books(req.query)
        return text.strip()

    results_text = await asyncio.to_thread(_search)
    return {"results": results_text}


@router.post("/study/find_free")
async def study_find_free(req: StudyFindFreeRequest, request: Request):
    if not get_user(request):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    from kai.tools.knowledge.study import find_free

    # Blocking DOI/Unpaywall lookup — run it off the event loop.
    result = await asyncio.to_thread(find_free, doi=req.doi, title=req.title)
    return {"result": result}


@router.post("/study/download")
async def study_download(req: StudyDownloadRequest, request: Request):
    """Download an epub/pdf to the user's local study library."""
    user = get_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    uid = user["user_id"]

    from kai.store.db import get_conn

    lib_path = Path(cfg.STUDY_LIBRARY_PATH) / str(uid)
    lib_path.mkdir(parents=True, exist_ok=True)

    item_id = _uuid.uuid4().hex
    ext = "epub" if req.format == "epub" else "pdf"
    file_path = lib_path / f"{item_id}.{ext}"

    def _download() -> None:
        opener = _urlreq.build_opener()
        opener.addheaders = [("User-Agent", "Mozilla/5.0 Kai-Study/1.0")]
        with opener.open(req.url, timeout=30) as resp:
            file_path.write_bytes(resp.read())

    try:
        # Up to 30s of blocking network I/O — must not freeze in-flight chat
        # streaming on the event loop.
        await asyncio.to_thread(_download)
    except Exception as e:
        return JSONResponse(status_code=502, content={"detail": f"Download failed: {e}"})

    conn = get_conn()
    conn.execute(
        "INSERT INTO study_library (user_id, title, author, source, original_url, format, path)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (uid, req.title or file_path.name, req.author, req.source, req.url, ext, str(file_path)),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM study_library WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)
    ).fetchone()
    item_id = row[0]

    # Background: extract text and index chunks for library RAG search
    def _index():
        from kai.tools.knowledge.study import index_study_item

        try:
            n = index_study_item(item_id, uid, str(file_path), ext)
            if n:
                print(
                    f"[+] Study: indexed {n} chunks for item {item_id} ({req.title or 'untitled'})"
                )
        except Exception as exc:
            print(f"[!] Study index failed for item {item_id} (non-critical): {exc}")

    threading.Thread(target=_index, daemon=True).start()

    return {"item_id": item_id, "format": ext, "title": req.title}


@router.get("/study/library")
async def study_library(request: Request):
    user = get_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    uid = user["user_id"]
    from kai.store.db import get_conn

    conn = get_conn()
    rows = conn.execute(
        "SELECT id, title, author, source, format, created_at FROM study_library"
        " WHERE user_id=? ORDER BY id DESC",
        (uid,),
    ).fetchall()
    items = [
        {
            "id": r[0],
            "title": r[1],
            "author": r[2],
            "source": r[3],
            "format": r[4],
            "created_at": r[5],
        }
        for r in rows
    ]
    return {"items": items}


@router.get("/study/read/{item_id}")
async def study_read(item_id: int, request: Request):
    """Serve a downloaded epub/pdf inline so the browser can display it."""
    user = get_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    uid = user["user_id"]
    from kai.store.db import get_conn

    conn = get_conn()
    row = conn.execute(
        "SELECT path, format, title FROM study_library WHERE id=? AND user_id=?",
        (item_id, uid),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Item not found")
    file_path = Path(row[0])
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
    fmt = row[1]
    media_type = "application/epub+zip" if fmt == "epub" else "application/pdf"
    return Response(
        content=file_path.read_bytes(),
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{file_path.name}"'},
    )


@router.post("/study/ask")
async def study_ask(req: StudyAskRequest, request: Request):
    """Search saved library chunks for passages answering a question."""
    user = get_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    uid = user["user_id"]
    from kai.tools.knowledge.study import ask_library

    # Embeds the question + scans library chunks — offload the CPU/IO work.
    result = await asyncio.to_thread(ask_library, question=req.question, user_id=uid)
    return {"result": result}


@router.delete("/study/library/{item_id}")
async def study_delete(item_id: int, request: Request):
    user = get_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    uid = user["user_id"]
    from kai.store.db import get_conn

    conn = get_conn()
    row = conn.execute(
        "SELECT path FROM study_library WHERE id=? AND user_id=?", (item_id, uid)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Item not found")
    try:
        Path(row[0]).unlink(missing_ok=True)
    except Exception:
        pass
    conn.execute("DELETE FROM study_library WHERE id=? AND user_id=?", (item_id, uid))
    conn.commit()
    return {"ok": True}
