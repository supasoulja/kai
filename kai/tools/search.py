"""
search.web — DuckDuckGo search via the DDG HTML endpoint.
No API key required. Returns top N result titles + snippets + URLs.
"""
import json
import urllib.request
import urllib.parse
import re

from kai.config import SEARCH_MAX_RESULTS
from kai.tools.registry import registry


@registry.tool(
    name="search.web",
    description=(
        "Search the web using DuckDuckGo. Use this whenever the user asks about "
        "something that changes over time or that your training data may not have: "
        "current events, recent game releases, latest software versions, prices, "
        "news, trending topics, sports scores, or anything from the past year. "
        "When in doubt about whether information is current, search rather than guess. "
        "After retrieving results: synthesize the findings into a clear answer — "
        "do not just repeat the snippets. Note where sources agree or conflict. "
        "End your response with a Sources list: one line per result used, "
        "formatted as '• Site Name — url'."
    ),
    parameters={
        "query": {
            "type": "string",
            "description": "The search query.",
            "required": True,
        },
    },
)
def web_search(query: str) -> str:
    results = _ddg_search(query, max_results=SEARCH_MAX_RESULTS)
    if not results:
        return f"No results found for '{query}'."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   {r['snippet']}\n   {r['url']}")
    body = "\n\n".join(lines)
    return (
        body
        + "\n\n---\n"
        "Synthesize the above into a clear answer. Do not just repeat the snippets. "
        "Note if sources agree or conflict. End with:\n"
        "Sources:\n• [Site Name] — [url]  (one line per source used)"
    )


def _ddg_search(query: str, max_results: int = 5) -> list[dict]:
    """
    Hits DuckDuckGo's HTML search endpoint and parses results.
    Falls back to an empty list on any error.
    """
    try:
        params = urllib.parse.urlencode({"q": query, "kl": "us-en"})
        url = f"https://html.duckduckgo.com/html/?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        return _parse_results(html, max_results)
    except Exception:
        return []


def _parse_results(html: str, max_results: int) -> list[dict]:
    results = []

    # DDG HTML returns results in <div class="result"> blocks
    # Title is in <a class="result__a">, snippet in <a class="result__snippet">
    title_pattern   = re.compile(r'class="result__a"[^>]*>(.*?)</a>', re.DOTALL)
    snippet_pattern = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
    url_pattern     = re.compile(r'class="result__url"[^>]*>(.*?)</span>', re.DOTALL)

    titles   = title_pattern.findall(html)
    snippets = snippet_pattern.findall(html)
    urls     = url_pattern.findall(html)

    for i in range(min(len(titles), max_results)):
        results.append({
            "title":   _strip_tags(titles[i]).strip(),
            "snippet": _strip_tags(snippets[i]).strip() if i < len(snippets) else "",
            "url":     _strip_tags(urls[i]).strip() if i < len(urls) else "",
        })

    return results


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode basic entities."""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&#x27;", "'").replace("&quot;", '"').replace("&nbsp;", " ")
    return text
