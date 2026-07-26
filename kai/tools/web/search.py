"""
search.web — DuckDuckGo search (no API key). Returns top N titles + snippets + URLs.
Primary endpoint is the DDG HTML page; falls back to the sturdier DDG-Lite page
when the HTML layout parses nothing, so one DDG redesign doesn't mean zero results.
"""

import re
import urllib.parse
import urllib.request

from kai.config import SEARCH_MAX_RESULTS
from kai.tools.registry import registry

# recency → DuckDuckGo's `df` date-filter code.
_RECENCY_CODES = {"day": "d", "week": "w", "month": "m", "year": "y"}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


@registry.tool(
    name="search.web",
    description=(
        "Search the web with DuckDuckGo. Use this for anything that changes over time "
        "or postdates your training: current events, prices, news, latest versions, "
        "scores, recent releases. When unsure whether info is current, search rather "
        "than guess. Optional `recency` filters by day/week/month/year. "
        "PRIVACY: the query is sent to DuckDuckGo (external service, no tracking)."
    ),
    parameters={
        "query": {
            "type": "string",
            "description": "The search query.",
            "required": True,
        },
        "recency": {
            "type": "string",
            "description": "Optional time filter: 'day', 'week', 'month', or 'year'. Omit for any time.",
        },
        "max_results": {
            "type": "integer",
            "description": "How many results to return (1-10). Default 5.",
        },
    },
)
def web_search(query: str, recency: str = "", max_results: int | None = None) -> str:
    n = SEARCH_MAX_RESULTS if max_results is None else min(max(1, int(max_results)), 10)
    results = _ddg_search(query, max_results=n, recency=recency)
    if not results:
        return f"No results found for '{query}'."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   {r['snippet']}\n   {r['url']}")
    body = "\n\n".join(lines)
    return (
        body + "\n\n---\n"
        "Synthesize the above into a clear answer. Do not just repeat the snippets. "
        "Note if sources agree or conflict. End with:\n"
        "Sources:\n• [Site Name] — [url]  (one line per source used)"
    )


def _ddg_search(query: str, max_results: int = 5, recency: str = "") -> list[dict]:
    """
    Query DuckDuckGo and parse results. Tries the HTML endpoint first; if it
    returns markup but parses 0 results (a layout change), retries the sturdier
    DDG-Lite endpoint. Returns an empty list on any error.
    """
    df = _RECENCY_CODES.get((recency or "").strip().lower(), "")

    # ── Primary: html.duckduckgo.com ──
    try:
        params = {"q": query, "kl": "us-en"}
        if df:
            params["df"] = df
        url = f"https://html.duckduckgo.com/html/?{urllib.parse.urlencode(params)}"
        html = _fetch(url)
        results = _parse_results(html, max_results)
        if results:
            return results
        if len(html) > 1000:
            print(
                "[!] search.web: DuckDuckGo HTML endpoint returned "
                f"{len(html)} chars but 0 results parsed — trying DDG-Lite fallback. "
                "If this recurs, check _parse_results() regex patterns."
            )
    except Exception:
        pass

    # ── Fallback: lite.duckduckgo.com (simpler, more stable markup) ──
    try:
        params = {"q": query, "kl": "us-en"}
        if df:
            params["df"] = df
        url = f"https://lite.duckduckgo.com/lite/?{urllib.parse.urlencode(params)}"
        html = _fetch(url)
        return _parse_lite_results(html, max_results)
    except Exception:
        return []


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_results(html: str, max_results: int) -> list[dict]:
    results = []

    # DDG HTML returns results in <div class="result"> blocks
    # Title is in <a class="result__a">, snippet in <a class="result__snippet">
    title_pattern = re.compile(r'class="result__a"[^>]*>(.*?)</a>', re.DOTALL)
    snippet_pattern = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
    url_pattern = re.compile(r'class="result__url"[^>]*>(.*?)</span>', re.DOTALL)

    titles = title_pattern.findall(html)
    snippets = snippet_pattern.findall(html)
    urls = url_pattern.findall(html)

    for i in range(min(len(titles), max_results)):
        results.append(
            {
                "title": _strip_tags(titles[i]).strip(),
                "snippet": _strip_tags(snippets[i]).strip() if i < len(snippets) else "",
                "url": _strip_tags(urls[i]).strip() if i < len(urls) else "",
            }
        )

    return results


def _parse_lite_results(html: str, max_results: int) -> list[dict]:
    """Parse the DDG-Lite results page. Titles/links come from <a class="result-link">
    anchors; snippets from <td class="result-snippet"> cells."""
    link_pattern = re.compile(
        r'<a[^>]+class="result-link"[^>]+href="(.*?)"[^>]*>(.*?)</a>', re.DOTALL
    )
    snippet_pattern = re.compile(r'class="result-snippet"[^>]*>(.*?)</td>', re.DOTALL)

    links = link_pattern.findall(html)  # [(href, title), ...]
    snippets = snippet_pattern.findall(html)

    results = []
    for i in range(min(len(links), max_results)):
        href, title = links[i]
        results.append(
            {
                "title": _strip_tags(title).strip(),
                "snippet": _strip_tags(snippets[i]).strip() if i < len(snippets) else "",
                "url": _clean_lite_url(href),
            }
        )
    return results


def _clean_lite_url(href: str) -> str:
    """DDG-Lite sometimes wraps targets in a /l/?uddg= redirect — unwrap it."""
    href = _strip_tags(href).strip()
    if "uddg=" in href:
        try:
            qs = urllib.parse.urlparse(href).query
            target = urllib.parse.parse_qs(qs).get("uddg", [""])[0]
            if target:
                return urllib.parse.unquote(target)
        except Exception:
            pass
    return href


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode basic entities."""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&#x27;", "'").replace("&quot;", '"').replace("&nbsp;", " ")
    return text
