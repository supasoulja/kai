"""
Researcher tools — give the model the ability to actually read the web.

  research.fetch_url        — fetch a URL, return a clean readable EXCERPT (search mode)
  research.add_to_library   — read a URL in FULL and file it in the RAG library (deep mode)

Two reading modes share one fetcher (_extract_url_text). Search mode returns a
tight excerpt so the orchestrator model isn't fed a whole page; library mode reads
everything on the way into the searchable document store.
"""
import html
import io
import re
import urllib.parse
from html.parser import HTMLParser

import httpx

from kai.config import WEB_EXCERPT_CHARS
from kai.tools.registry import registry

# Tags whose entire content we skip (scripts, styles, nav boilerplate).
# Only include paired tags (open + close) — void elements like <meta> and <link>
# have no closing tag, so they'd permanently increment the skip depth counter.
_SKIP_TAGS = {"script", "style", "noscript", "head", "nav", "footer",
              "aside", "form", "button", "svg", "iframe"}

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ── HTML → plain text ──────────────────────────────────────────────────────────

class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in _SKIP_TAGS:
            self._skip_depth += 1
        # Block elements — add a newline so text doesn't run together
        if tag.lower() in {"p", "div", "br", "h1", "h2", "h3",
                            "h4", "h5", "h6", "li", "tr", "blockquote"}:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag.lower() in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        raw = html.unescape(raw)
        # Collapse horizontal whitespace, strip blank lines, collapse gaps
        raw = re.sub(r"[ \t]+", " ", raw)
        lines = [ln.strip() for ln in raw.splitlines()]
        lines = [ln for ln in lines if ln]   # drop blank lines
        return "\n".join(lines)


def _html_to_text(html_bytes: bytes, encoding: str = "utf-8") -> str:
    try:
        text = html_bytes.decode(encoding, errors="replace")
    except Exception:
        text = html_bytes.decode("utf-8", errors="replace")
    stripper = _Stripper()
    stripper.feed(text)
    return stripper.get_text()


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
        return None, "", f"Timeout fetching {url} (15s limit). The server may be slow or unreachable."
    except httpx.ConnectError as e:
        return None, "", f"Could not connect to {url}: {e}"
    except Exception as e:
        return None, "", f"Error fetching {url}: {e}"

    content_type = resp.headers.get("content-type", "")

    if "application/pdf" in content_type or url.lower().endswith(".pdf"):
        try:
            return resp.status_code, _pdf_to_text(resp.content), None
        except ImportError:
            return resp.status_code, "", (
                "This URL is a PDF and PDF support isn't installed. "
                "Run: pip install -r requirements-documents.txt"
            )
        except Exception as e:
            return resp.status_code, "", f"Could not read PDF at {url}: {e}"

    if "text/plain" in content_type or "application/json" in content_type:
        return resp.status_code, resp.text, None

    return resp.status_code, _html_to_text(resp.content, resp.encoding or "utf-8"), None


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

    truncated = len(text) > WEB_EXCERPT_CHARS
    excerpt = text[:WEB_EXCERPT_CHARS]
    result = f"[{status}] {url.strip()}\n\n{excerpt}"
    if truncated:
        result += (
            f"\n\n[Excerpt — showing first {WEB_EXCERPT_CHARS} chars. "
            "Ask to add this page to the library for the full text.]"
        )
    return result


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
    from kai.memory import documents as _docs
    from kai.core._app_state import get_embed_fn as _get_embed_fn, get_current_user_id

    status, text, error = _extract_url_text(url)
    if error:
        return error
    if not text.strip():
        return f"[{status}] {url.strip()}\n\nNothing readable to save — page had no extractable text."

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
