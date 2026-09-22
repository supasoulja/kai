"""
Shared HTML → plain text for tool modules.

The stripper used to exist twice: once for web fetch, once nested inside
study._extract_epub_text in a weaker form (boolean skip state, script/style the
only skipped tags, block structure flattened). The weak copy was the one feeding
whole books into the RAG library, so it carried nav TOCs and <head> titles into
every embedding.
"""

import re
from html.parser import HTMLParser

from kai.config import WEB_EXCERPT_CHARS

# Tags whose entire content we skip (scripts, styles, nav boilerplate).
# Only include paired tags (open + close) — void elements like <meta> and <link>
# have no closing tag, so they'd permanently increment the skip depth counter.
_SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "head",
    "nav",
    "footer",
    "aside",
    "form",
    "button",
    "svg",
    "iframe",
}

# Block elements — emit a newline so their text doesn't run together.
_BLOCK_TAGS = {
    "p",
    "div",
    "br",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "tr",
    "blockquote",
}


def collapse_ws(text: str) -> str:
    """Collapse horizontal whitespace (NBSP included) and drop blank lines."""
    text = re.sub(r"[ \t\xa0]+", " ", text)
    lines = (ln.strip() for ln in text.splitlines())
    return "\n".join(ln for ln in lines if ln)


class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        # No html.unescape here: HTMLParser defaults to convert_charrefs=True, so
        # handle_data already receives resolved entities. Unescaping a second time
        # turns a literal "&amp;amp;" into "&" instead of "&amp;".
        return collapse_ws("".join(self._parts))


def html_to_text(html_bytes: bytes, encoding: str = "utf-8") -> str:
    try:
        text = html_bytes.decode(encoding, errors="replace")
    except LookupError:  # bogus charset in a Content-Type header
        text = html_bytes.decode("utf-8", errors="replace")
    stripper = _Stripper()
    stripper.feed(text)
    stripper.close()  # flush the tail HTMLParser holds back as a possible partial entity
    return stripper.get_text()


def excerpt(text: str, limit: int = WEB_EXCERPT_CHARS) -> str:
    """Trim page text for the orchestrator model, saying so when there was more."""
    if len(text) <= limit:
        return text
    return (
        f"{text[:limit]}\n\n[Excerpt — showing first {limit} chars. "
        "Ask to add this page to the library for the full text.]"
    )
