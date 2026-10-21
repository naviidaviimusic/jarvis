#!/usr/local/bin/python3.11
"""
JARVIS Web Search — DuckDuckGo instant answers + web results
─────────────────────────
Free, no API key. Used for [ACTION:WEBSEARCH] queries.
"""

import logging
import re
from typing import Optional
from urllib.parse import quote_plus

log = logging.getLogger("jarvis.web_search")

DDG_API_URL = "https://api.duckduckgo.com/?q={query}&format=json&no_redirect=1&no_html=1&skip_disambig=1"
DDG_HTML_URL = "https://html.duckduckgo.com/html/?q={query}"

TIMEOUT = 5.0
MAX_RESULT_CHARS = 500


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_client():
    """Return a configured httpx client."""
    import httpx  # type: ignore
    return httpx.Client(
        timeout=TIMEOUT,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36"
        },
        follow_redirects=True,
    )


def _parse_ddg_html_results(html: str, max_results: int = 3) -> list[dict]:
    """Extract web results from DuckDuckGo HTML page using regex (no BeautifulSoup)."""
    results: list[dict] = []

    # DDG HTML results are in <a class="result__a"> ... </a> blocks
    # with snippets in <a class="result__snippet">
    title_pattern = re.compile(
        r'class="result__a"[^>]*>(.*?)</a>', re.DOTALL
    )
    snippet_pattern = re.compile(
        r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL
    )

    titles = title_pattern.findall(html)
    snippets = snippet_pattern.findall(html)

    # Strip HTML tags from extracted text
    def strip_tags(s: str) -> str:
        return re.sub(r"<[^>]+>", "", s).strip()

    for i in range(min(max_results, len(titles))):
        title = strip_tags(titles[i])
        snippet = strip_tags(snippets[i]) if i < len(snippets) else ""
        if title:
            results.append({"title": title, "snippet": snippet})

    return results


# ── Public API ────────────────────────────────────────────────────────────────

def instant_answer(query: str) -> Optional[str]:
    """Get DuckDuckGo instant answer only (Abstract field). Returns None if no answer."""
    try:
        import httpx  # type: ignore
        url = DDG_API_URL.format(query=quote_plus(query))
        with _get_client() as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()

        abstract = (data.get("AbstractText") or "").strip()
        if abstract:
            return abstract

        # Fallback: Answer field (e.g. calculator results)
        answer = (data.get("Answer") or "").strip()
        if answer:
            return answer

        # Fallback: Definition
        definition = (data.get("Definition") or "").strip()
        if definition:
            return definition

        return None

    except Exception as exc:
        log.warning("instant_answer failed for '%s': %s", query, exc)
        return None


def search(query: str, max_results: int = 3) -> str:
    """Search DuckDuckGo, return voice-friendly result string.

    Format: "According to DuckDuckGo: [Abstract]. Top results: [title1] — [snippet1]. ..."
    If instant answer available, lead with that.
    Truncated to 500 chars total for TTS.
    Never raises — returns error string on failure.
    """
    try:
        import httpx  # type: ignore

        # 1. Try instant answer
        ia = instant_answer(query)

        # 2. Fetch HTML results
        web_results: list[dict] = []
        try:
            html_url = DDG_HTML_URL.format(query=quote_plus(query))
            with _get_client() as client:
                resp = client.get(html_url)
                resp.raise_for_status()
            web_results = _parse_ddg_html_results(resp.text, max_results=max_results)
        except Exception as exc:
            log.warning("DDG HTML fetch failed for '%s': %s", query, exc)

        # 3. Assemble voice-friendly response
        parts: list[str] = []

        if ia:
            parts.append(f"According to DuckDuckGo: {ia}.")

        if web_results:
            result_strs: list[str] = []
            for r in web_results:
                title = r.get("title", "").strip()
                snippet = r.get("snippet", "").strip()
                if title and snippet:
                    result_strs.append(f"{title} — {snippet}")
                elif title:
                    result_strs.append(title)
            if result_strs:
                parts.append("Top results: " + ". ".join(result_strs))

        if not parts:
            return f"No results found for '{query}', sir."

        response = " ".join(parts)
        return response[:MAX_RESULT_CHARS] if len(response) > MAX_RESULT_CHARS else response

    except Exception as exc:
        log.error("search failed for '%s': %s", query, exc)
        return f"Web search unavailable at the moment, sir. ({exc})"
