"""
Open-access academic search and book discovery tools.

All sources are legitimately free — no paywall bypass, just surfacing what exists:
  - arXiv: author-posted preprints (physics, math, CS, economics, biology)
  - Semantic Scholar: free academic graph with PDF links, 200M+ papers
  - PubMed/NCBI: NIH-funded research, legally required to be open
  - CORE: 200M+ full-text open-access papers aggregated from global repos
  - SciELO: Latin America's entire scientific output, free and largely unknown
  - Unpaywall: finds the legal free copy of any paper by DOI (checks 50k+ repos)
  - Open Access Button: like Unpaywall + author request for the rest
  - Open Library: Internet Archive digital lending + public domain
  - Project Gutenberg: 70k public-domain epub books

Keys:
  UNPAYWALL_EMAIL  — just an email for rate-limiting, no account needed
  CORE_API_KEY     — free at core.ac.uk/services/api, raises rate limits significantly
"""
import json
import zipfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from kai.config import UNPAYWALL_EMAIL, CORE_API_KEY
from kai.tools.registry import registry

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# ── helpers ────────────────────────────────────────────────────────────────────

def _get(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _get_json(url: str, timeout: int = 12) -> dict | list:
    data = _get(url, timeout)
    return json.loads(data)


# ── study.search_papers ────────────────────────────────────────────────────────

@registry.tool(
    name="study.search_papers",
    description=(
        "Search for academic papers across free open-access sources: "
        "arXiv, Semantic Scholar, and PubMed Central. "
        "Returns titles, authors, year, abstract snippet, and a direct PDF link "
        "when one is legally available for free. "
        "Use this when the user wants to find research papers on a topic."
    ),
    parameters={
        "query": {
            "type": "string",
            "description": "Search query, e.g. 'CRISPR gene editing' or 'quantum error correction'",
        },
        "max_results": {
            "type": "integer",
            "description": "Max results per source (default 5, max 10)",
        },
    },
)
def search_papers(query: str, max_results: int = 5) -> str:
    max_results = min(max_results, 10)
    results: list[dict] = []

    # ── arXiv ──────────────────────────────────────────────────────────────────
    try:
        q = urllib.parse.quote(query)
        url = (
            f"https://export.arxiv.org/api/query"
            f"?search_query=all:{q}&start=0&max_results={max_results}"
        )
        xml_bytes = _get(url)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(xml_bytes)
        for entry in root.findall("a:entry", ns):
            title = (entry.findtext("a:title", "", ns) or "").strip().replace("\n", " ")
            authors = [
                a.findtext("a:name", "", ns)
                for a in entry.findall("a:author", ns)
            ]
            abstract = (entry.findtext("a:summary", "", ns) or "").strip()[:300]
            published = (entry.findtext("a:published", "", ns) or "")[:4]
            arxiv_id = (entry.findtext("a:id", "", ns) or "").strip()
            pdf_url = arxiv_id.replace("/abs/", "/pdf/") if "/abs/" in arxiv_id else ""
            if title:
                results.append({
                    "source": "arXiv",
                    "title": title,
                    "authors": authors[:3],
                    "year": published,
                    "abstract": abstract,
                    "pdf_url": pdf_url,
                    "page_url": arxiv_id,
                })
    except Exception as e:
        results.append({"source": "arXiv", "error": str(e)})

    # ── Semantic Scholar ───────────────────────────────────────────────────────
    try:
        q = urllib.parse.quote(query)
        fields = "title,authors,year,abstract,openAccessPdf,externalIds"
        url = (
            f"https://api.semanticscholar.org/graph/v1/paper/search"
            f"?query={q}&limit={max_results}&fields={fields}"
        )
        data = _get_json(url)
        for paper in data.get("data", []):
            oa = paper.get("openAccessPdf") or {}
            pdf_url = oa.get("url", "")
            authors = [a.get("name", "") for a in paper.get("authors", [])[:3]]
            abstract = (paper.get("abstract") or "")[:300]
            results.append({
                "source": "Semantic Scholar",
                "title": paper.get("title", ""),
                "authors": authors,
                "year": str(paper.get("year") or ""),
                "abstract": abstract,
                "pdf_url": pdf_url,
                "page_url": f"https://www.semanticscholar.org/paper/{paper.get('paperId', '')}",
            })
    except Exception as e:
        results.append({"source": "Semantic Scholar", "error": str(e)})

    # ── PubMed / NCBI ─────────────────────────────────────────────────────────
    try:
        q = urllib.parse.quote(query)
        # Step 1: esearch for IDs
        search_url = (
            f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
            f"?db=pmc&term={q}&retmax={max_results}&retmode=json"
        )
        ids_data = _get_json(search_url)
        ids = ids_data.get("esearchresult", {}).get("idlist", [])
        if ids:
            # Step 2: esummary for titles
            ids_str = ",".join(ids)
            summary_url = (
                f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
                f"?db=pmc&id={ids_str}&retmode=json"
            )
            summary_data = _get_json(summary_url)
            for uid in ids:
                item = summary_data.get("result", {}).get(uid, {})
                title = item.get("title", "")
                if not title:
                    continue
                authors_raw = item.get("authors", [])
                authors = [a.get("name", "") for a in authors_raw[:3]]
                year = (item.get("pubdate") or "")[:4]
                pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{uid}/pdf/"
                results.append({
                    "source": "PubMed Central",
                    "title": title,
                    "authors": authors,
                    "year": year,
                    "abstract": "",
                    "pdf_url": pdf_url,
                    "page_url": f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{uid}/",
                })
    except Exception as e:
        results.append({"source": "PubMed Central", "error": str(e)})

    # ── CORE ──────────────────────────────────────────────────────────────────
    # 200M+ full-text papers from institutional repos worldwide. Free API key
    # at core.ac.uk raises limits; works without a key too.
    try:
        q = urllib.parse.quote(query)
        core_url = (
            f"https://api.core.ac.uk/v3/search/works"
            f"?q={q}&limit={max_results}&fulltext=false"
        )
        headers = dict(_HEADERS)
        if CORE_API_KEY:
            headers["Authorization"] = f"Bearer {CORE_API_KEY}"
        req = urllib.request.Request(core_url, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as r:
            core_data = json.loads(r.read())
        for work in core_data.get("results", []):
            title = work.get("title") or ""
            if not title:
                continue
            authors = [(a.get("name") or "") for a in (work.get("authors") or [])[:3]]
            year = str(work.get("yearPublished") or "")
            abstract = (work.get("abstract") or "")[:300]
            dl_url = work.get("downloadUrl") or ""
            page_url = work.get("sourceFulltextUrls", [None])[0] or ""
            results.append({
                "source": "CORE",
                "title": title,
                "authors": authors,
                "year": year,
                "abstract": abstract,
                "pdf_url": dl_url,
                "page_url": page_url or f"https://core.ac.uk/works/{work.get('id', '')}",
            })
    except Exception as e:
        results.append({"source": "CORE", "error": str(e)})

    # ── SciELO ────────────────────────────────────────────────────────────────
    # Latin America's scientific output — enormous blind spot in US/EU aggregators.
    try:
        q = urllib.parse.quote(query)
        scielo_url = (
            f"https://search.scielo.org/?q={q}&lang=en&count={max_results}"
            f"&from=0&output=json&sort=&format=summary&fb=&page=1"
        )
        scielo_data = _get_json(scielo_url)
        for hit in (scielo_data.get("hits", {}).get("hits", []) or []):
            src = hit.get("_source", {})
            title_raw = src.get("ti", {})
            title = title_raw if isinstance(title_raw, str) else (
                title_raw.get("pt") or title_raw.get("en") or
                next(iter(title_raw.values()), "") if isinstance(title_raw, dict) else ""
            )
            if not title:
                continue
            authors = src.get("au", [])[:3] if isinstance(src.get("au"), list) else []
            year = str(src.get("dp") or "")[:4]
            ab_raw = src.get("ab", {})
            abstract = (ab_raw if isinstance(ab_raw, str) else
                        next(iter(ab_raw.values()), "") if isinstance(ab_raw, dict) else "")[:300]
            pid = src.get("id", "")
            pdf_url = f"https://www.scielo.br/pdf/{pid}" if pid else ""
            page_url = f"https://doi.org/{src['doi']}" if src.get("doi") else (
                f"https://search.scielo.org/?q={q}&lang=en" if pid else "")
            results.append({
                "source": "SciELO",
                "title": title,
                "authors": authors,
                "year": year,
                "abstract": abstract,
                "pdf_url": pdf_url,
                "page_url": page_url,
            })
    except Exception as e:
        results.append({"source": "SciELO", "error": str(e)})

    if not results:
        return "No results found. Try different search terms."

    lines = [f"Found {len([r for r in results if 'title' in r])} papers:\n"]
    for i, r in enumerate(results, 1):
        if "error" in r:
            lines.append(f"[{r['source']} error: {r['error']}]")
            continue
        authors_str = ", ".join(r.get("authors", [])) or "Unknown"
        lines.append(
            f"{i}. [{r['source']}] {r['title']}\n"
            f"   Authors: {authors_str} ({r.get('year', '?')})\n"
            f"   {r.get('abstract', '')}\n"
            f"   PDF: {r.get('pdf_url') or '(not available)'}\n"
            f"   Page: {r.get('page_url', '')}\n"
        )
    return "\n".join(lines)


# ── study.find_free ────────────────────────────────────────────────────────────

@registry.tool(
    name="study.find_free",
    description=(
        "Given a paper DOI or title, find the legal free version via Unpaywall. "
        "Unpaywall checks 50,000+ repositories (institutional repos, author pages, "
        "PubMed Central, Europe PMC, CORE) for open-access copies. "
        "This is how to legally get papers that appear to be behind paywalls — "
        "many papers have a legal free copy somewhere that the publisher doesn't tell you about. "
        "Requires UNPAYWALL_EMAIL in config."
    ),
    parameters={
        "doi": {
            "type": "string",
            "description": "DOI of the paper, e.g. '10.1038/s41586-021-03819-2'. "
                           "Leave blank if you only have the title.",
        },
        "title": {
            "type": "string",
            "description": "Paper title to search for (used if DOI not provided).",
        },
    },
)
def find_free(doi: str = "", title: str = "") -> str:
    email = UNPAYWALL_EMAIL or "kai-study@example.com"

    if doi:
        doi = doi.strip()
        url = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}?email={email}"
        try:
            data = _get_json(url)
        except Exception as e:
            return f"Unpaywall lookup failed: {e}"

        title_found = data.get("title", doi)
        oa_status = data.get("oa_status", "unknown")
        best = data.get("best_oa_location") or {}
        pdf_url = best.get("url_for_pdf") or best.get("url") or ""
        license_ = best.get("license") or "unknown"
        repo = best.get("host_type") or "unknown"

        if not pdf_url:
            # try all locations
            for loc in data.get("oa_locations", []):
                if loc.get("url_for_pdf"):
                    pdf_url = loc["url_for_pdf"]
                    break

        if pdf_url:
            return (
                f"Free copy found for: {title_found}\n"
                f"Status: {oa_status} | License: {license_} | Source: {repo}\n"
                f"PDF: {pdf_url}"
            )
        elif oa_status != "closed":
            return (
                f"Paper is open-access ({oa_status}) but no direct PDF link found.\n"
                f"Try the publisher page: {data.get('doi_url', '')}"
            )
        else:
            # Unpaywall says closed — try Open Access Button as a second opinion.
            # OA Button checks different repo lists and can find copies Unpaywall misses.
            oab_result = _oab_lookup(doi=doi)
            if oab_result:
                return (
                    f"Unpaywall: no free copy — but Open Access Button found one!\n"
                    f"Title: {title_found}\n"
                    f"{oab_result}"
                )
            return (
                f"No free version found for: {title_found}\n"
                f"Checked: Unpaywall + Open Access Button.\n"
                f"Options: email the corresponding author (most will share), "
                f"check ResearchGate / Academia.edu, or request via your library's ILL."
            )

    elif title:
        # Search Semantic Scholar for the DOI then recurse
        try:
            q = urllib.parse.quote(title)
            url = (
                f"https://api.semanticscholar.org/graph/v1/paper/search"
                f"?query={q}&limit=1&fields=title,externalIds,openAccessPdf"
            )
            data = _get_json(url)
            papers = data.get("data", [])
            if not papers:
                # Fall straight through to OA Button title search
                oab = _oab_lookup(title=title)
                if oab:
                    return f"Open Access Button found a free copy:\n{oab}"
                return "Could not find this paper by title. Try providing the DOI."
            paper = papers[0]
            found_doi = (paper.get("externalIds") or {}).get("DOI", "")
            oa = paper.get("openAccessPdf") or {}
            pdf_url = oa.get("url", "")
            if pdf_url:
                return (
                    f"Free PDF found via Semantic Scholar: {paper.get('title', title)}\n"
                    f"PDF: {pdf_url}"
                )
            if found_doi:
                return find_free(doi=found_doi)
            # Last resort: OA Button by title
            oab = _oab_lookup(title=title)
            if oab:
                return f"Open Access Button found a free copy:\n{oab}"
            return f"Found paper '{paper.get('title', title)}' but no free PDF located."
        except Exception as e:
            return f"Title lookup failed: {e}"

    return "Provide either a DOI or a paper title."


def _oab_lookup(doi: str = "", title: str = "") -> str:
    """Open Access Button — checks repositories Unpaywall may miss. Returns PDF URL string or ''."""
    try:
        if doi:
            url = f"https://api.openaccessbutton.org/find?id={urllib.parse.quote(doi)}"
        elif title:
            url = f"https://api.openaccessbutton.org/find?title={urllib.parse.quote(title)}"
        else:
            return ""
        data = _get_json(url, timeout=10)
        found_url = (data.get("data") or {}).get("url", "")
        if found_url:
            return f"PDF: {found_url}"
        return ""
    except Exception:
        return ""


# ── study.search_books ─────────────────────────────────────────────────────────

@registry.tool(
    name="study.search_books",
    description=(
        "Search for freely available books across Open Library (Internet Archive) "
        "and Project Gutenberg. Returns books that are either public domain (free epub/pdf "
        "download) or available for free digital borrowing via Open Library. "
        "Great for textbooks, classics, and historical texts."
    ),
    parameters={
        "query": {
            "type": "string",
            "description": "Book title, author, or subject, e.g. 'calculus textbook' or 'Richard Feynman'",
        },
        "max_results": {
            "type": "integer",
            "description": "Max results to return (default 8)",
        },
    },
)
def search_books(query: str, max_results: int = 8) -> str:
    max_results = min(max_results, 20)
    results: list[dict] = []

    # ── Open Library ──────────────────────────────────────────────────────────
    try:
        q = urllib.parse.quote(query)
        url = (
            f"https://openlibrary.org/search.json"
            f"?q={q}&limit={max_results}&fields=key,title,author_name,first_publish_year,"
            f"ia,availability,subject,edition_count"
        )
        data = _get_json(url)
        for doc in data.get("docs", []):
            ia_id = (doc.get("ia") or [None])[0]
            avail = doc.get("availability", {}) or {}
            status = avail.get("status", "")
            borrow_url = ""
            download_url = ""
            if ia_id:
                borrow_url = f"https://archive.org/details/{ia_id}"
                if status in ("open", "public domain"):
                    download_url = f"https://archive.org/download/{ia_id}/{ia_id}.epub"
            results.append({
                "source": "Open Library",
                "title": doc.get("title", ""),
                "authors": doc.get("author_name", [])[:2],
                "year": str(doc.get("first_publish_year") or ""),
                "status": status or ("available" if ia_id else "catalog only"),
                "borrow_url": borrow_url,
                "download_url": download_url,
                "ol_key": doc.get("key", ""),
            })
    except Exception as e:
        results.append({"source": "Open Library", "error": str(e)})

    # ── Project Gutenberg ─────────────────────────────────────────────────────
    try:
        q = urllib.parse.quote(query)
        url = f"https://gutendex.com/books/?search={q}&page_size={max_results}"
        data = _get_json(url)
        for book in data.get("results", []):
            authors = [a.get("name", "") for a in book.get("authors", [])[:2]]
            formats = book.get("formats", {})
            epub_url = formats.get("application/epub+zip", "")
            pdf_url = formats.get("application/pdf", "")
            txt_url = formats.get("text/plain; charset=utf-8", "") or formats.get("text/plain", "")
            results.append({
                "source": "Project Gutenberg",
                "title": book.get("title", ""),
                "authors": authors,
                "year": "",
                "status": "public domain",
                "borrow_url": f"https://www.gutenberg.org/ebooks/{book.get('id', '')}",
                "download_url": epub_url or pdf_url or txt_url,
                "epub_url": epub_url,
            })
    except Exception as e:
        results.append({"source": "Project Gutenberg", "error": str(e)})

    if not results:
        return "No books found. Try different search terms."

    lines = [f"Found {len([r for r in results if 'title' in r])} books:\n"]
    for i, r in enumerate(results, 1):
        if "error" in r:
            lines.append(f"[{r['source']} error: {r['error']}]")
            continue
        authors_str = ", ".join(r.get("authors", [])) or "Unknown"
        status = r.get("status", "")
        dl = r.get("download_url", "")
        borrow = r.get("borrow_url", "")
        lines.append(
            f"{i}. [{r['source']}] {r['title']}\n"
            f"   {authors_str} ({r.get('year', '?')}) — {status}\n"
            f"   {'Download: ' + dl if dl else 'Borrow: ' + borrow if borrow else '(catalog only)'}\n"
        )
    return "\n".join(lines)


# ── study.get_book_url ─────────────────────────────────────────────────────────

@registry.tool(
    name="study.get_book_url",
    description=(
        "Get the direct epub/pdf download URL for a book from Project Gutenberg (by Gutenberg ID) "
        "or Open Library (by Internet Archive ID). "
        "Use this after study.search_books to get a downloadable link for a specific book."
    ),
    parameters={
        "gutenberg_id": {
            "type": "integer",
            "description": "Project Gutenberg book ID (the number in the URL, e.g. 84 for Frankenstein).",
        },
        "archive_id": {
            "type": "string",
            "description": "Internet Archive item ID for an Open Library book (e.g. 'frankenstein00shel').",
        },
    },
)
def get_book_url(gutenberg_id: int = 0, archive_id: str = "") -> str:
    if gutenberg_id:
        try:
            url = f"https://gutendex.com/books/{gutenberg_id}/"
            data = _get_json(url)
            formats = data.get("formats", {})
            epub_url = formats.get("application/epub+zip", "")
            pdf_url = formats.get("application/pdf", "")
            title = data.get("title", f"Book {gutenberg_id}")
            authors = [a.get("name", "") for a in data.get("authors", [])[:2]]
            if epub_url:
                return (
                    f"{title} by {', '.join(authors)}\n"
                    f"EPUB: {epub_url}\n"
                    f"Page: https://www.gutenberg.org/ebooks/{gutenberg_id}"
                )
            elif pdf_url:
                return (
                    f"{title} by {', '.join(authors)}\n"
                    f"PDF: {pdf_url}\n"
                    f"Page: https://www.gutenberg.org/ebooks/{gutenberg_id}"
                )
            else:
                return f"No epub/pdf found for Gutenberg ID {gutenberg_id}. Available formats: {list(formats.keys())}"
        except Exception as e:
            return f"Gutenberg lookup failed: {e}"

    if archive_id:
        archive_id = archive_id.strip()
        epub_url = f"https://archive.org/download/{archive_id}/{archive_id}.epub"
        pdf_url = f"https://archive.org/download/{archive_id}/{archive_id}.pdf"
        page_url = f"https://archive.org/details/{archive_id}"
        return (
            f"Open Library / Internet Archive: {archive_id}\n"
            f"EPUB (try): {epub_url}\n"
            f"PDF (try): {pdf_url}\n"
            f"Page: {page_url}"
        )

    return "Provide either a gutenberg_id or an archive_id."


# ── Library RAG helpers ────────────────────────────────────────────────────────

def _extract_epub_text(path: str) -> str:
    """Extract plain text from an epub file using only stdlib (zipfile + html.parser)."""
    import html.parser

    class _Strip(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts: list[str] = []
            self._skip = False
        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self._skip = True
            if tag in ("p", "div", "h1", "h2", "h3", "h4", "li", "tr", "br"):
                self.parts.append("\n")
        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self._skip = False
        def handle_data(self, data):
            if not self._skip:
                self.parts.append(data)

    texts: list[str] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            for name in sorted(zf.namelist()):
                if name.lower().endswith((".html", ".xhtml", ".htm")):
                    raw = zf.read(name).decode("utf-8", errors="replace")
                    p = _Strip()
                    p.feed(raw)
                    chunk = " ".join("".join(p.parts).split())
                    if chunk:
                        texts.append(chunk)
    except Exception:
        pass
    return "\n\n".join(texts)


def _extract_text_from_file(path: str, fmt: str) -> str:
    """Extract text from epub or pdf (best-effort). PDF uses pypdf — the same
    reader as document uploads (kai/memory/documents.py) so there's one PDF
    dependency, not two. Returns "" if pypdf isn't installed."""
    if fmt == "epub":
        return _extract_epub_text(path)
    # PDF: extract with pypdf if available, else return empty.
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""


def index_study_item(item_id: int, user_id: int, path: str, fmt: str) -> int:
    """
    Extract text from a saved file, split into chunks, store in study_chunks.
    Returns number of chunks written. Called in a background thread after download.
    """
    from kai.store.db import get_conn

    text = _extract_text_from_file(path, fmt)
    if not text or len(text) < 100:
        return 0

    chunk_size = 1200
    overlap = 150
    words = text.split()
    chunks: list[str] = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i: i + chunk_size])
        if chunk:
            chunks.append(chunk)
        i += chunk_size - overlap

    conn = get_conn()
    # Clear old chunks for this item (idempotent re-index)
    conn.execute("DELETE FROM study_chunks WHERE item_id=?", (item_id,))
    for idx, chunk in enumerate(chunks):
        conn.execute(
            "INSERT INTO study_chunks (item_id, user_id, chunk_index, content) VALUES (?,?,?,?)",
            (item_id, user_id, idx, chunk),
        )
    conn.commit()
    return len(chunks)


# ── study.ask_library ──────────────────────────────────────────────────────────

@registry.tool(
    name="study.ask_library",
    description=(
        "Search through the user's saved study library (downloaded epubs and pdfs) "
        "to find relevant passages that answer a question. "
        "This does full-text keyword search over the indexed content of saved books and papers. "
        "Use when the user asks a question and wants the answer drawn from their own saved materials."
    ),
    parameters={
        "question": {
            "type": "string",
            "description": "The question or search terms to look for in the saved library.",
        },
        "user_id": {
            "type": "integer",
            "description": "User ID (passed automatically from context).",
        },
    },
)
def ask_library(question: str, user_id: int = 0) -> str:
    from kai.store.db import get_conn

    conn = get_conn()

    # Simple FTS-style keyword search using LIKE — good enough without sqlite-fts
    # Split query into words, require all to appear in the chunk (AND logic)
    words = [w.strip() for w in question.lower().split() if len(w.strip()) > 2]
    if not words:
        return "Please provide a longer question to search the library."

    # Build a query with AND conditions across words
    conditions = " AND ".join([f"LOWER(sc.content) LIKE ?" for _ in words])
    params = [f"%{w}%" for w in words] + [user_id]

    rows = conn.execute(
        f"""
        SELECT sc.content, sl.title, sl.author, sc.chunk_index
        FROM study_chunks sc
        JOIN study_library sl ON sc.item_id = sl.id
        WHERE {conditions} AND sc.user_id = ?
        ORDER BY sc.item_id, sc.chunk_index
        LIMIT 5
        """,
        params,
    ).fetchall()

    if not rows:
        # Fallback: OR search (any word matches)
        or_conditions = " OR ".join([f"LOWER(sc.content) LIKE ?" for _ in words])
        or_params = [f"%{w}%" for w in words] + [user_id]
        rows = conn.execute(
            f"""
            SELECT sc.content, sl.title, sl.author, sc.chunk_index
            FROM study_chunks sc
            JOIN study_library sl ON sc.item_id = sl.id
            WHERE ({or_conditions}) AND sc.user_id = ?
            ORDER BY sc.item_id, sc.chunk_index
            LIMIT 5
            """,
            or_params,
        ).fetchall()

    if not rows:
        total = conn.execute(
            "SELECT COUNT(*) FROM study_chunks WHERE user_id=?", (user_id,)
        ).fetchone()[0]
        if total == 0:
            return (
                "Your library hasn't been indexed yet. "
                "Save some books or papers from the Study tab first — "
                "they'll be automatically indexed for searching."
            )
        return f"No passages found matching '{question}' across {total} indexed chunks in your library."

    lines = [f"Found {len(rows)} relevant passage(s) from your library:\n"]
    for content, title, author, chunk_idx in rows:
        byline = f" by {author}" if author else ""
        lines.append(
            f"── From: {title}{byline} (chunk {chunk_idx}) ──\n"
            f"{content[:600]}{'…' if len(content) > 600 else ''}\n"
        )
    return "\n".join(lines)
