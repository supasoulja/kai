"""
design.render — turns Markdown into a designed PDF or PNG: flyers, slide decks, summaries.

The model writes only the content; the layouts here supply fonts, color and spacing. A small
model writes clean Markdown far more reliably than good-looking HTML, so the design lives in
code. Rendering runs in headless Chromium (the browser extra) with every request blocked
except the bundled fonts, and with scripts off in the final page, so a document can't fetch
or send anything while it renders.
"""

import re

import kai.config as cfg
from kai.tools.design._common import (
    CATEGORY,
    CATEGORY_DESCRIPTION,
    THEMES,
    file_entry,
    output_dir,
    stem,
    theme_name,
)
from kai.tools.registry import ToolUnavailable, registry

_MAX_CONTENT = 200_000
_FONT_HOST = "https://kai.fonts/"

_LAYOUTS = {
    "document": "document",
    "doc": "document",
    "summary": "document",
    "report": "document",
    "handout": "document",
    "slides": "slides",
    "slide": "slides",
    "presentation": "slides",
    "deck": "slides",
    "slideshow": "slides",
    "flyer": "flyer",
    "poster": "flyer",
    "sign": "flyer",
    "html": "html",
    "page": "html",
}
_FORMATS = {"pdf": "pdf", "png": "png", "image": "png", "jpg": "png", "jpeg": "png"}

# "[INSERT PHONE NUMBER]", "[Your Name]", "TBD": left where the model lacked a detail.
# Markdown links, [text](url), are not placeholders.
_PLACEHOLDER_RE = re.compile(
    r"\[(?i:insert|your|add|enter|name of)\b[^\]\n]{0,60}\]"
    r"|\[[A-Z][A-Za-z/ ]{1,40}\](?![(\[])"
    r"|\b(?:TBD|XXX+)\b"
)

_VIEWPORTS = {
    "slides": {"width": 1280, "height": 720},
    "document": {"width": 816, "height": 1056},
    "flyer": {"width": 816, "height": 1056},
    "html": {"width": 816, "height": 1056},
}

_FONTS_CSS = (
    f"@font-face{{font-family:'Inter';font-weight:300 600;"
    f"src:url({_FONT_HOST}inter-300-6.woff2) format('woff2')}}"
    f"@font-face{{font-family:'Space Grotesk';font-weight:300 700;"
    f"src:url({_FONT_HOST}space-grotesk-300-9.woff2) format('woff2')}}"
)

_BASE_CSS = """
:root{--ink:#0f172a;--muted:#475569;--rule:#e2e8f0;--paper:#ffffff}
*{box-sizing:border-box}
body{margin:0;font-family:'Inter',system-ui,sans-serif;color:var(--ink);
  -webkit-print-color-adjust:exact;print-color-adjust:exact}
h1,h2,h3{font-family:'Space Grotesk','Inter',sans-serif;letter-spacing:-0.01em}
table{border-collapse:collapse;width:100%}
th{background:var(--accent);color:#fff;text-align:left;font-weight:600}
td{border-bottom:1px solid var(--rule)}
tr:nth-child(even) td{background:#f8fafc}
li::marker{color:var(--accent)}
code{font-family:ui-monospace,monospace;background:#f1f5f9;padding:1px 4px;border-radius:3px;font-size:.9em}
blockquote{margin:0;background:var(--soft);border-left:4px solid var(--accent);border-radius:0 8px 8px 0}
blockquote p{margin:0}
"""

_LAYOUT_CSS = {
    "document": """
@page{size:Letter;margin:.75in .8in .9in}
body{font-size:10.5pt;line-height:1.55}
@media screen{body{padding:56px 64px;max-width:816px}}
.doc-head{border-left:6px solid var(--accent);padding:2pt 0 2pt 14pt;margin-bottom:20pt}
.doc-head h1{font-size:26pt;line-height:1.1;margin:0 0 6pt}
.doc-head p{color:var(--muted);font-size:12pt;margin:0}
h2{font-size:15pt;color:var(--accent);margin:20pt 0 6pt;padding-bottom:4pt;
  border-bottom:1px solid var(--rule);break-after:avoid}
h3{font-size:11.5pt;margin:14pt 0 4pt;break-after:avoid}
table{margin:10pt 0;font-size:9.5pt;break-inside:avoid}
th{padding:6pt 8pt} td{padding:5pt 8pt}
blockquote{margin:12pt 0;padding:10pt 14pt;break-inside:avoid}
ul,ol{padding-left:18pt} li{margin:3pt 0}
""",
    "slides": """
@page{size:1280px 720px;margin:0}
.slide{width:1280px;height:720px;position:relative;overflow:hidden;break-after:page;
  background:var(--paper);padding:64px 96px 88px;display:flex;flex-direction:column}
.slide::before{content:'';position:absolute;left:0;top:0;bottom:0;width:14px;background:var(--accent)}
.slide h1,.slide h2{font-size:46px;line-height:1.1;margin:0 0 28px}
.slide .body{flex:1;min-height:0;font-size:28px;line-height:1.45;color:#1e293b}
.slide .body>*:first-child{margin-top:0}
.slide li{margin:10px 0}
.slide table{font-size:22px} .slide th,.slide td{padding:10px 16px}
.slide blockquote{padding:18px 24px;font-size:26px;margin-top:28px}
.slide footer{position:absolute;left:96px;right:96px;bottom:32px;display:flex;
  justify-content:space-between;font-size:15px;color:var(--muted)}
.slide.title{background:var(--ink);justify-content:center}
.slide.title::before{width:100%;height:14px;bottom:auto}
.slide.title::after{content:'';position:absolute;right:-160px;bottom:-200px;width:620px;height:620px;
  border-radius:50%;background:radial-gradient(circle at 30% 30%,var(--accent),var(--accent2));opacity:.9}
.slide.title .body{flex:none;position:relative;z-index:1;max-width:760px;color:#cbd5e1}
.slide.title h1{color:#fff;font-size:76px;margin-bottom:24px}
""",
    "flyer": """
@page{size:Letter;margin:0}
.flyer{width:816px;height:1056px;position:relative;overflow:hidden;background:var(--paper);
  padding:88px 80px 72px;display:flex;flex-direction:column}
.flyer::before{content:'';position:absolute;top:-180px;right:-160px;width:560px;height:560px;
  border-radius:50%;background:radial-gradient(circle at 35% 35%,var(--accent2),var(--accent));opacity:.95}
.flyer::after{content:'';position:absolute;bottom:170px;left:-120px;width:280px;height:280px;
  border-radius:50%;background:var(--soft)}
.flyer .body{flex:1;min-height:0;display:flex;flex-direction:column;position:relative;z-index:1}
.flyer h1{font-size:78px;line-height:1.0;margin:120px 0 20px;max-width:620px}
.flyer h1+p{font-size:24px;line-height:1.35;color:var(--muted);margin:0 0 36px;max-width:560px}
.flyer h2{font-family:'Inter';font-size:14px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--accent);margin:26px 0 10px}
.flyer p{font-size:19px;line-height:1.5}
.flyer ul{list-style:none;padding:0;margin:0}
.flyer li{font-size:21px;line-height:1.4;padding:8px 0 8px 22px;position:relative}
.flyer li::before{content:'';position:absolute;left:0;top:18px;width:10px;height:10px;
  border-radius:50%;background:var(--accent)}
.flyer blockquote:last-child{margin-top:auto;background:var(--accent);border:0;border-radius:14px;
  color:#fff;padding:26px 32px;font-size:25px;font-weight:600}
""",
}

# Runs in a scratch page with the network blocked, using the same marked + DOMPurify the chat
# UI ships. The Markdown arrives as data, never as HTML.
_CONVERT_JS = r"""
({md, layout}) => {
  const frag = s => {
    const t = document.createElement('template');
    t.innerHTML = DOMPurify.sanitize(marked.parse(s));
    return t;
  };
  // A ### line straight after the title is a tagline, not a section.
  const tagline = t => {
    const h1 = t.content.querySelector('h1');
    const next = h1 && h1.nextElementSibling;
    if (next && /^H[3-6]$/.test(next.tagName)) {
      const p = document.createElement('p');
      p.innerHTML = next.innerHTML;
      next.replaceWith(p);
    }
    return t;
  };
  if (layout === 'slides') {
    // No --- separators: start a new slide at every ## heading instead.
    if (!/^\s*---\s*$/m.test(md)) md = md.replace(/^(?=##\s)/gm, '---\n');
    const chunks = md.split(/^\s*---\s*$/m).map(s => s.trim()).filter(Boolean);
    const lead = frag(chunks[0] || '').content.querySelector('h1,h2');
    const deck = (lead ? lead.textContent : '').trim().replace(/&/g, '&amp;').replace(/</g, '&lt;');
    return chunks.map((c, i) => {
      const t = tagline(frag(c));
      const head = t.content.querySelector('h1,h2');
      const isTitle = i === 0 && head && head.tagName === 'H1';
      let title = '';
      if (head && !isTitle) { title = head.outerHTML; head.remove(); }
      const foot = isTitle ? '' :
        `<footer><span>${deck}</span><span>${i + 1} / ${chunks.length}</span></footer>`;
      return `<section class="slide${isTitle ? ' title' : ''}">${title}` +
        `<div class="body">${t.innerHTML}</div>${foot}</section>`;
    }).join('');
  }
  const t = tagline(frag(md));
  if (layout === 'flyer') return `<div class="flyer"><div class="body">${t.innerHTML}</div></div>`;
  const first = t.content.firstElementChild;
  if (first && first.tagName === 'H1') {
    const head = document.createElement('header');
    head.className = 'doc-head';
    const sub = first.nextElementSibling;
    t.content.insertBefore(head, first);
    head.appendChild(first);
    if (sub && sub.tagName === 'P') head.appendChild(sub);
  }
  return t.innerHTML;
}
"""

# Shrinks a fixed-size page's text until it fits. Measures where the content ends, not the
# page's scroll height, so the decorative shapes never count as overflow.
_FIT_JS = r"""
() => {
  let shrunk = 0;
  for (const page of document.querySelectorAll('.slide, .flyer')) {
    const target = page.querySelector(':scope > .body');
    if (!target || !target.children.length) continue;
    const pad = parseFloat(getComputedStyle(page).paddingBottom);
    const limit = () => page.getBoundingClientRect().bottom - pad;
    const end = () => Math.max(...[...target.children].map(c => c.getBoundingClientRect().bottom));
    let scale = 1;
    while (end() > limit() + 1 && scale > 0.55) {
      scale -= 0.05;
      target.style.zoom = scale;
      shrunk++;
    }
  }
  return shrunk;
}
"""

_PAGE_NUMBERS = (
    '<div style="font-size:8px;color:#94a3b8;width:100%;text-align:center">'
    '<span class="pageNumber"></span> / <span class="totalPages"></span></div>'
)


def _route(route):
    """Every request the page makes lands here: bundled fonts are served, the rest refused."""
    url = route.request.url
    if url.startswith(_FONT_HOST):
        name = url[len(_FONT_HOST) :]
        font = cfg.STATIC_DIR / "fonts" / name
        if re.fullmatch(r"[\w.-]+\.woff2", name) and font.is_file():
            return route.fulfill(path=str(font), content_type="font/woff2")
    return route.abort()


def _markdown_to_html(browser, md: str, layout: str) -> str:
    ctx = browser.new_context(java_script_enabled=True)
    try:
        ctx.route("**/*", _route)
        page = ctx.new_page()
        page.set_content("<!doctype html><html><body></body></html>")
        page.add_script_tag(path=str(cfg.STATIC_DIR / "marked.min.js"))
        page.add_script_tag(path=str(cfg.STATIC_DIR / "purify.min.js"))
        return page.evaluate(_CONVERT_JS, {"md": md, "layout": layout})
    finally:
        ctx.close()


def _page_html(body: str, layout: str, theme: str) -> str:
    if layout == "html":
        return f"<style>{_FONTS_CSS}</style>{body}"
    accent, accent2, soft = THEMES[theme]
    css = (
        _FONTS_CSS
        + _BASE_CSS
        + _LAYOUT_CSS[layout]
        + f":root{{--accent:{accent};--accent2:{accent2};--soft:{soft}}}"
    )
    return (
        f"<!doctype html><html><head><meta charset=utf-8><style>{css}</style></head>"
        f"<body>{body}</body></html>"
    )


def _launch(pw):
    try:
        return pw.chromium.launch(headless=True)
    except Exception as e:
        if "playwright install" in str(e) or "Executable doesn't exist" in str(e):
            raise ToolUnavailable(
                "design.render needs Chromium. Run: python -m playwright install chromium"
            ) from e
        raise


def _render(content: str, layout: str, fmt: str, theme: str, base: str) -> dict:
    from playwright.sync_api import sync_playwright

    folder = output_dir()
    outputs, preview = [], None
    with sync_playwright() as pw:
        browser = _launch(pw)
        try:
            body = content if layout == "html" else _markdown_to_html(browser, content, layout)
            ctx = browser.new_context(
                java_script_enabled=False, device_scale_factor=2, viewport=_VIEWPORTS[layout]
            )
            ctx.route("**/*", _route)
            page = ctx.new_page()
            page.set_content(_page_html(body, layout, theme), wait_until="load")
            page.evaluate("document.fonts.ready.then(() => true)")
            shrunk = page.evaluate(_FIT_JS)
            pages = page.query_selector_all(".slide, .flyer")

            if fmt == "pdf":
                pdf = folder / f"{base}.pdf"
                opts = {"path": str(pdf), "print_background": True, "prefer_css_page_size": True}
                if layout == "document":
                    opts.update(
                        display_header_footer=True,
                        header_template="<span></span>",
                        footer_template=_PAGE_NUMBERS,
                    )
                elif layout == "html":
                    opts["format"] = "Letter"
                page.pdf(**opts)
                outputs.append(pdf)
                preview = folder / f"{base}.preview.png"
                if pages:
                    pages[0].screenshot(path=str(preview))
                else:
                    page.screenshot(path=str(preview))
            elif pages:
                for old in folder.glob(f"{base}-[0-9][0-9].png"):
                    old.unlink()
                for i, el in enumerate(pages, 1):
                    png = folder / (f"{base}-{i:02d}.png" if len(pages) > 1 else f"{base}.png")
                    el.screenshot(path=str(png))
                    outputs.append(png)
            else:
                png = folder / f"{base}.png"
                page.screenshot(path=str(png), full_page=True)
                outputs.append(png)
        finally:
            browser.close()

    result = {
        "files": [file_entry(p) for p in outputs]
        + ([file_entry(preview, preview=True)] if preview else []),
        "layout": layout,
        "theme": theme,
    }
    if layout == "slides":
        result["slides"] = len(pages)
    if shrunk:
        result["shrunk_to_fit"] = True
    return result


@registry.tool(
    name="design.render",
    description=(
        "Make a designed document and save it as a PDF or PNG: flyers, posters, slide decks, "
        "presentations, summaries, reports, handouts. You write only the content, in Markdown; "
        "fonts, colors and layout are applied for you. Layouts: "
        "'flyer' = one page. # headline, one short tagline line, ## sections with - bullet "
        "details, and a final > line as the call to action. "
        "'slides' = a deck. Separate slides with a line containing only ---. The first slide is "
        "# Title plus a subtitle line; every other slide starts with a ## heading and holds "
        "3-6 short bullets or a table. "
        "'document' = summary or report. # title, a subtitle line, ## sections, lists, tables, "
        "> for key callouts. "
        "'html' = content is a complete HTML page, rendered as-is. "
        "Never drop a detail the design needs (a phone number, a date, a place): if the user "
        "hasn't given it, write it as a [bracketed placeholder] and you will be told to ask."
    ),
    parameters={
        "filename": {
            "type": "string",
            "description": "Short name without extension, e.g. 'bake-sale-flyer'.",
            "required": True,
        },
        "content": {
            "type": "string",
            "description": "The Markdown (or HTML for layout 'html').",
            "required": True,
        },
        "layout": {
            "type": "string",
            "enum": ["document", "slides", "flyer", "html"],
            "required": True,
        },
        "format": {
            "type": "string",
            "enum": ["pdf", "png"],
            "description": "Default pdf. A PDF also gets a PNG preview of its first page.",
        },
        "theme": {
            "type": "string",
            "enum": list(THEMES),
            "description": "Color theme. Pick one that suits the subject.",
        },
    },
    category=CATEGORY,
    category_description=CATEGORY_DESCRIPTION,
    label="Designing the document",
    risk="caution",
)
def design_render(
    filename: str, content: str, layout: str = "document", format: str = "pdf", theme: str = ""
) -> dict:
    try:
        import playwright  # noqa: F401
    except ImportError as e:
        raise ToolUnavailable(
            "design.render needs Playwright. Run: pip install playwright && "
            "python -m playwright install chromium"
        ) from e

    layout_key = _LAYOUTS.get((layout or "document").strip().lower())
    if not layout_key:
        raise ValueError(f"Unknown layout {layout!r}. Use one of: document, slides, flyer, html.")
    fmt = _FORMATS.get((format or "pdf").strip().lower())
    if not fmt:
        raise ValueError(f"Unknown format {format!r}. Use pdf or png.")
    if not (content or "").strip():
        raise ValueError("content is empty. Write the document in Markdown and pass it here.")
    if len(content) > _MAX_CONTENT:
        raise ValueError(f"content is {len(content):,} characters; the limit is {_MAX_CONTENT:,}.")

    base = stem(filename)
    theme_key = theme_name(theme)
    source = output_dir() / f"{base}.{'html' if layout_key == 'html' else 'md'}"
    source.write_text(content, encoding="utf-8")

    result = _render(content, layout_key, fmt, theme_key, base)
    result["source"] = str(source)
    placeholders = sorted(set(_PLACEHOLDER_RE.findall(content)))
    if placeholders:
        result["placeholders"] = placeholders
        result["note"] = (
            "The design still has placeholders. Ask the user for these details, then render "
            "it again with the same filename."
        )
    return result
