"""
M10 — one HTML→text extractor, shared by web fetch and epub ingest.

The epub path used to define its own weaker stripper: skip state was a boolean
(so the close tag of a nested skipped element un-skipped the rest of its parent),
only <script>/<style> were skipped at all, and block structure was flattened to a
single line. Full books entered the study library carrying nav TOCs, <head>
titles and svg labels, and that text feeds embeddings and RAG.

Run with: python -m pytest tests/test_html_extract.py -v
"""

import os
import zipfile

os.environ.setdefault("KAI_TEST_MODE", "1")

from kai.tools._html import collapse_ws, excerpt, html_to_text
from kai.tools.knowledge.study import _extract_epub_text


def _epub(tmp_path, **chapters: str) -> str:
    path = tmp_path / "book.epub"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        for name, body in chapters.items():
            zf.writestr(f"OEBPS/{name}.xhtml", body)
    return str(path)


# ── epub ingest ────────────────────────────────────────────────────────────────


def test_epub_drops_boilerplate_containers(tmp_path):
    """<head>, <nav>, <svg>, <noscript>, <form> are chrome, not prose. The old
    stripper skipped only script/style, so all of this landed in the library."""
    path = _epub(
        tmp_path,
        ch1=(
            "<html><head><title>NOISE_TITLE</title>"
            "<style>body{margin:0}</style></head>"
            "<body>"
            "<nav epub:type='toc'><ol><li>NOISE_TOC</li></ol></nav>"
            "<svg><title>NOISE_SVG</title></svg>"
            "<noscript>NOISE_NOSCRIPT</noscript>"
            "<form><button>NOISE_BUTTON</button></form>"
            "<p>Call me Ishmael.</p>"
            "</body></html>"
        ),
    )
    text = _extract_epub_text(path)
    assert "Call me Ishmael." in text
    for noise in ("NOISE_TITLE", "NOISE_TOC", "NOISE_SVG", "NOISE_NOSCRIPT", "NOISE_BUTTON"):
        assert noise not in text, f"{noise} leaked into extracted text"


def test_epub_nested_skip_does_not_un_skip_parent(tmp_path):
    """The depth counter. With a boolean, </script> and </form> clear the flag
    while still inside <noscript>/<nav>, leaking the rest of the parent."""
    path = _epub(
        tmp_path,
        ch1=(
            "<body>"
            "<noscript><script>var t='NOISE_JS';</script>NOISE_AFTER_SCRIPT</noscript>"
            "<nav>Contents<form><button>Go</button></form>NOISE_AFTER_FORM</nav>"
            "<p>Chapter One</p>"
            "</body>"
        ),
    )
    text = _extract_epub_text(path)
    assert "Chapter One" in text
    assert "NOISE_JS" not in text
    assert "NOISE_AFTER_SCRIPT" not in text
    assert "NOISE_AFTER_FORM" not in text


def test_epub_keeps_block_structure_and_collapses_runs(tmp_path):
    """Whitespace runs and blank lines collapse, but paragraph boundaries
    survive. The old stripper flattened every chapter to one long line."""
    path = _epub(
        tmp_path,
        ch1="<body><p>one</p>\n\n\n<p>   two    three   </p>\n\n<h2>four</h2></body>",
    )
    text = _extract_epub_text(path)
    assert text == "one\ntwo three\nfour"


def test_epub_resolves_entities(tmp_path):
    path = _epub(
        tmp_path,
        ch1="<body><p>Tom &amp; Jerry said &#39;hi&#39;</p><p>a&nbsp;b</p></body>",
    )
    text = _extract_epub_text(path)
    assert text == "Tom & Jerry said 'hi'\na b"


def test_epub_chapters_join_in_name_order(tmp_path):
    path = _epub(tmp_path, ch2="<body><p>second</p></body>", ch1="<body><p>first</p></body>")
    assert _extract_epub_text(path) == "first\n\nsecond"


def test_epub_unreadable_file_returns_empty(tmp_path):
    bad = tmp_path / "not-a-zip.epub"
    bad.write_bytes(b"this is not a zip archive")
    assert _extract_epub_text(str(bad)) == ""


# ── the shared stripper ────────────────────────────────────────────────────────


def test_skip_depth_survives_a_nested_close():
    html = b"<nav>a<form><button>b</button></form>LEAK</nav><p>keep</p>"
    assert html_to_text(html) == "keep"


def test_entities_are_resolved_exactly_once():
    """HTMLParser resolves charrefs itself (convert_charrefs=True). Unescaping a
    second time would turn a literal "&amp;amp;" into "&" instead of "&amp;"."""
    assert html_to_text(b"<p>Tom &amp; Jerry</p>") == "Tom & Jerry"
    assert html_to_text(b"<p>&#39;quoted&#39;</p>") == "'quoted'"
    assert html_to_text(b"<p>Write &amp;amp; for an ampersand</p>") == (
        "Write &amp; for an ampersand"
    )


def test_nbsp_normalises_to_a_plain_space():
    assert html_to_text(b"<p>a&nbsp;&nbsp;b</p>") == "a b"


def test_trailing_partial_entity_is_not_swallowed():
    """HTMLParser buffers a trailing "&..." as a possible charref; without close()
    the whole tail is lost."""
    assert html_to_text(b"<p>Fish &amp") == "Fish &"


def test_decode_falls_back_when_the_charset_is_bogus():
    assert html_to_text("<p>café</p>".encode(), encoding="utf-lol") == "café"


def test_collapse_ws_keeps_line_breaks_and_drops_blanks():
    assert collapse_ws("  a   b  \n\n\n  c \t d  \n") == "a b\nc d"


def test_excerpt_only_annotates_when_it_trims():
    assert excerpt("short", limit=10) == "short"
    trimmed = excerpt("x" * 50, limit=10)
    assert trimmed.startswith("x" * 10 + "\n\n[Excerpt — showing first 10 chars.")
    assert "x" * 11 not in trimmed
