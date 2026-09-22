"""
Researcher tools — give the model the ability to actually read the web.

  research.fetch_url        — fetch a URL, return a clean readable EXCERPT (search mode)
  research.add_to_library   — read a URL in FULL and file it in the RAG library (deep mode)

Two reading modes share one fetcher (_extract_url_text). Search mode returns a
tight excerpt so the orchestrator model isn't fed a whole page; library mode reads
everything on the way into the searchable document store.
"""

import io
import urllib.parse

import httpx

from kai.tools._html import excerpt, html_to_text
from kai.tools.registry import registry

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _pdf_to_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes. Same pypdf idiom as kai/memory/documents.py
    and kai/tools/knowledge/study.py. Raises if pypdf isn't installed."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


# ── Shared fetcher ───────────────────────────────────────────────────────────────


def _extract_url_text(url: str) -> tuple[int | None, str, str | None]:
    """Fetch a URL and return (status_code, full_text, error).

    Handles HTML, plain text/JSON, and PDF. Returns the FULL text (no truncation);
    callers decide whether to excerpt. On failure, status_code is None and error
    holds a human-readable message (full_text is "").
    """
    url = url.strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None, "", f"Invalid URL scheme {parsed.scheme!r}. Use http:// or https://."

    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=15.0,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as client:
            resp = client.get(url)
    except httpx.TimeoutException:
        return (
            None,
            "",
            f"Timeout fetching {url} (15s limit). The server may be slow or unreachable.",
        )
    except httpx.ConnectError as e:
        return None, "", f"Could not connect to {url}: {e}"
    except Exception as e:
        return None, "", f"Error fetching {url}: {e}"

    content_type = resp.headers.get("content-type", "")

    if "application/pdf" in content_type or url.lower().endswith(".pdf"):
        try:
            return resp.status_code, _pdf_to_text(resp.content), None
        except ImportError:
            return (
                resp.status_code,
                "",
                (
                    "This URL is a PDF and PDF support isn't installed. "
                    "Run: pip install -r requirements-documents.txt"
                ),
            )
        except Exception as e:
            return resp.status_code, "", f"Could not read PDF at {url}: {e}"

    if "text/plain" in content_type or "application/json" in content_type:
        return resp.status_code, resp.text, None

    return resp.status_code, html_to_text(resp.content, resp.encoding or "utf-8"), None


# ── research.fetch_url (search mode — excerpt) ────────────────────────────────────


@registry.tool(
    name="research.fetch_url",
    description=(
        "Fetch a URL and return a clean readable excerpt of its content. "
        "Use this when search.web gives a snippet but you need more of the actual "
        "article, doc page, or PDF. Returns a trimmed excerpt (not the whole page) "
        "to stay focused — to capture a long page in full, add it to the library instead."
    ),
    parameters={
        "url": {
            "type": "string",
            "description": "The full URL to fetch (must start with http:// or https://)",
        },
    },
)
def fetch_url(url: str) -> str:
    status, text, error = _extract_url_text(url)
    if error:
        return error
    if not text.strip():
        return f"[{status}] {url}\n\nPage returned no readable text."

    return f"[{status}] {url.strip()}\n\n{excerpt(text)}"


# ── research.add_to_library (deep mode — full read + index) ───────────────────────


@registry.tool(
    name="research.add_to_library",
    description=(
        "Read a web page or PDF in FULL and file it in the searchable library. "
        "Use when the user wants to save an article/doc to refer back to, or asks you "
        "to study a long page in depth. After this, docs.search can find its contents. "
        "PRIVACY: fetches the URL from an external server."
    ),
    parameters={
        "url": {
            "type": "string",
            "description": "The full URL to read and save (must start with http:// or https://)",
            "required": True,
        },
    },
)
def add_to_library(url: str) -> str:
    from kai.core._app_state import get_current_user_id
    from kai.core._app_state import get_embed_fn as _get_embed_fn
    from kai.memory import documents as _docs

    status, text, error = _extract_url_text(url)
    if error:
        return error
    if not text.strip():
        return (
            f"[{status}] {url.strip()}\n\nNothing readable to save — page had no extractable text."
        )

    try:
        meta = _docs.ingest_text(
            text,
            source_name=url.strip(),
            embed_fn=_get_embed_fn(),
            user_id=get_current_user_id(),
            file_type="url",
        )
    except Exception as e:
        return f"Could not add {url.strip()} to the library: {e}"

    kb = round(meta["char_count"] / 1000, 1)
    return (
        f"Saved to library: {url.strip()}\n"
        f"~{kb}k chars in {meta['chunk_count']} searchable chunks. "
        f"Use docs.search to find anything in it."
    )
