"""
Researcher tools — give the researcher model the ability to actually read
the web, not just get search snippets.

  research.fetch_url — fetch a URL and return clean readable text
"""
import html
import re
import urllib.parse
from html.parser import HTMLParser

import httpx

from kai.tools.registry import registry

# Tags whose entire content we skip (scripts, styles, nav boilerplate).
# Only include paired tags (open + close) — void elements like <meta> and <link>
# have no closing tag, so they'd permanently increment the skip depth counter.
_SKIP_TAGS = {"script", "style", "noscript", "head", "nav", "footer",
              "aside", "form", "button", "svg", "iframe"}

_MAX_CHARS  = 12_000   # truncation limit — keeps it inside context budget
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


# ── Tool ───────────────────────────────────────────────────────────────────────

@registry.tool(
    name="research.fetch_url",
    description=(
        "Fetch the full readable text content of any URL. "
        "Use this when search.web gives a snippet but you need the full article, "
        "documentation page, or web resource. Handles HTML pages and plain text. "
        "Returns cleaned text with scripts, ads, and navigation stripped out."
    ),
    parameters={
        "url": {
            "type": "string",
            "description": "The full URL to fetch (must start with http:// or https://)",
        },
    },
)
def fetch_url(url: str) -> str:
    url = url.strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"Invalid URL scheme '{parsed.scheme}'. Only http:// and https:// are supported."

    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=15.0,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as client:
            resp = client.get(url)

        content_type = resp.headers.get("content-type", "")

        # Plain text — return directly
        if "text/plain" in content_type or "application/json" in content_type:
            text = resp.text[:_MAX_CHARS]
            return f"[{resp.status_code}] {url}\n\n{text}"

        # PDF — can't parse, tell the model
        if "application/pdf" in content_type:
            return (
                f"[{resp.status_code}] {url}\n\n"
                "This URL returns a PDF file. Direct PDF reading is not supported yet. "
                "Try finding an HTML version of this content."
            )

        # HTML — strip to readable text
        text = _html_to_text(resp.content, resp.encoding or "utf-8")

        if not text:
            return f"[{resp.status_code}] {url}\n\nPage returned no readable text."

        truncated = len(text) > _MAX_CHARS
        text = text[:_MAX_CHARS]

        result = f"[{resp.status_code}] {url}\n\n{text}"
        if truncated:
            result += f"\n\n[Truncated — page exceeded {_MAX_CHARS} character limit]"
        return result

    except httpx.TimeoutException:
        return f"Timeout fetching {url} (15s limit). The server may be slow or unreachable."
    except httpx.ConnectError as e:
        return f"Could not connect to {url}: {e}"
    except Exception as e:
        return f"Error fetching {url}: {e}"
