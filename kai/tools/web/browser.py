"""
Browser automation tools — JS-rendered pages that research.fetch_url can't read.

  browser.read_page  — navigate to a URL, wait for JS to render, return clean text
  browser.screenshot — capture a screenshot of a page (base64 PNG, feeds into vision)

Uses Playwright headless Chromium. Falls back gracefully if Playwright isn't available.
"""
import re
import tempfile
from pathlib import Path

from kai.tools.registry import registry

_PLAYWRIGHT_ERR = (
    "Browser automation requires Playwright. "
    "Run: pip install playwright && python -m playwright install chromium"
)


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _clean_text(raw: str) -> str:
    """Collapse whitespace runs in extracted browser text."""
    raw = re.sub(r"[ \t]+", " ", raw)
    lines = [ln.strip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


# ── browser.read_page ──────────────────────────────────────────────────────────

@registry.tool(
    name="browser.read_page",
    description=(
        "Open a URL in a real browser and return the fully rendered page text. "
        "Use this when research.fetch_url returns empty content or a JS-required notice, "
        "meaning the page needs JavaScript to load its content. "
        "Waits for the page to finish loading before extracting text. "
        "Slower than fetch_url (~3-5s) — only use when fetch_url fails."
    ),
    parameters={
        "url": {
            "type": "string",
            "description": "The full URL to open (must start with http:// or https://)",
        },
        "wait_for": {
            "type": "string",
            "description": (
                "Optional CSS selector to wait for before reading the page. "
                "Useful for SPAs that load content after a spinner disappears. "
                "Leave empty to just wait for network idle."
            ),
        },
    },
)
def read_page(url: str, wait_for: str = "") -> str:
    if not _playwright_available():
        return _PLAYWRIGHT_ERR

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return f"Invalid URL — must start with http:// or https://"

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})

            try:
                page.goto(url, wait_until="networkidle", timeout=20_000)
            except PWTimeout:
                # networkidle timed out — page is probably usable, continue
                pass

            if wait_for.strip():
                try:
                    page.wait_for_selector(wait_for.strip(), timeout=8_000)
                except PWTimeout:
                    pass  # selector never appeared — grab what we have

            # Extract visible text from the body
            raw = page.inner_text("body")
            browser.close()

        text = _clean_text(raw)
        if not text:
            return f"[browser] {url}\n\nPage rendered but no readable text was found."

        # Search mode: return an excerpt, not the whole page (same contract as
        # research.fetch_url). Deep/full reads go through research.add_to_library.
        from kai.config import WEB_EXCERPT_CHARS
        truncated = len(text) > WEB_EXCERPT_CHARS
        result = f"[browser] {url}\n\n{text[:WEB_EXCERPT_CHARS]}"
        if truncated:
            result += (
                f"\n\n[Excerpt — showing first {WEB_EXCERPT_CHARS} chars. "
                "Ask to add this page to the library for the full text.]"
            )
        return result

    except Exception as e:
        return f"Browser error navigating to {url}: {e}"


# ── browser.screenshot ─────────────────────────────────────────────────────────

@registry.tool(
    name="browser.screenshot",
    description=(
        "Take a screenshot of a webpage and save it to a temp file for vision analysis. "
        "Use this when you need to SEE what a page looks like — charts, images, layouts, "
        "or content that doesn't translate to text. "
        "Returns the file path — pass it to vision.describe to analyze the image."
    ),
    parameters={
        "url": {
            "type": "string",
            "description": "The full URL to screenshot",
        },
        "full_page": {
            "type": "boolean",
            "description": "Capture the full scrollable page instead of just the viewport. Default false.",
        },
    },
)
def screenshot(url: str, full_page: bool = False) -> str:
    if not _playwright_available():
        return _PLAYWRIGHT_ERR

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return "Invalid URL — must start with http:// or https://"

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        # Save to a known temp location so vision.describe can find it
        out_path = Path(tempfile.gettempdir()) / "kai_screenshot.png"

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})

            try:
                page.goto(url, wait_until="networkidle", timeout=20_000)
            except PWTimeout:
                pass

            page.screenshot(path=str(out_path), full_page=bool(full_page))
            browser.close()

        size_kb = out_path.stat().st_size // 1024
        return (
            f"Screenshot saved: {out_path}  ({size_kb} KB)\n"
            f"Pass this path to vision.describe to analyze the image."
        )

    except Exception as e:
        return f"Screenshot error for {url}: {e}"
