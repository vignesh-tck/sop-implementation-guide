"""
Fetch whatever source the user pasted, and reduce it to text the LLM can read.

Deliberately plain httpx rather than a server-side web_fetch tool: it works with any
model, needs no beta header, and — the real reason — the fetched text stays visible to
the user, so an extracted claim can be checked against what was actually retrieved.

No source list, no keywords, no relevance rules live here. This module fetches what it
is told to fetch.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from backend.tools.grants_gov import clean_text

log = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (compatible; SoP-implementation-engine/0.1)"}

# Enough of a page to extract from without burning the context window. The reviewer
# sees the same slice the model did.
MAX_TEXT = 20_000


class FetchError(RuntimeError):
    """The source could not be retrieved."""


def fetch(url: str, timeout: float = 30.0, max_text: int = MAX_TEXT) -> dict:
    """
    Retrieve one source.

    Returns {url, final_url, status, content_type, text, truncated, fetched_at}.
    Raises FetchError on transport failure or a non-200 status — a source the user
    pasted that cannot be read is something they need told about, not a silent skip.
    """
    if not (url or "").strip():
        raise ValueError("url is required")

    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True, headers=UA)
    except httpx.HTTPError as e:
        raise FetchError(f"Could not reach {url}: {e}") from e

    if resp.status_code != 200:
        raise FetchError(f"{url} returned HTTP {resp.status_code}")

    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    text = _to_text(resp, ctype, url)
    truncated = len(text) > max_text

    return {
        "url": url,
        "final_url": str(resp.url),
        "status": resp.status_code,
        "content_type": ctype,
        "text": text[:max_text],
        "truncated": truncated,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _to_text(resp: httpx.Response, ctype: str, url: str) -> str:
    if "json" in ctype:
        try:
            return json.dumps(resp.json(), indent=2)[:MAX_TEXT * 2]
        except ValueError:
            return resp.text

    if "pdf" in ctype:
        # No PDF parser installed. Say so rather than handing the model bytes and
        # letting it invent a plausible-looking programme from nothing.
        raise FetchError(
            f"{url} is a PDF. PDF extraction is not supported yet — paste an HTML page, "
            "or copy the relevant text in as a note."
        )

    # HTML and anything else textual: drop script/style, then tags and entities.
    body = resp.text
    for tag in ("script", "style", "noscript", "svg"):
        body = _strip_block(body, tag)
    return clean_text(body, limit=MAX_TEXT * 2) or ""


def _strip_block(html_text: str, tag: str) -> str:
    """Remove <tag>...</tag> blocks whose content is never page copy."""
    out, low, cursor = [], html_text.lower(), 0
    open_t, close_t = f"<{tag}", f"</{tag}>"
    while True:
        start = low.find(open_t, cursor)
        if start == -1:
            out.append(html_text[cursor:])
            return "".join(out)
        end = low.find(close_t, start)
        if end == -1:
            out.append(html_text[cursor:start])
            return "".join(out)
        out.append(html_text[cursor:start])
        cursor = end + len(close_t)


def fetch_many(urls: list[str]) -> tuple[list[dict], list[dict]]:
    """
    Fetch several sources. Returns (fetched, failures).

    Failures are returned rather than raised so one bad URL does not abandon a
    session — the user sees which ones failed and can fix or drop them.
    """
    fetched, failures = [], []
    for u in urls:
        try:
            fetched.append(fetch(u))
        except (FetchError, ValueError) as e:
            log.warning("source failed: %s (%s)", u, e)
            failures.append({"url": u, "error": str(e)})
    return fetched, failures


def excerpt_is_genuine(excerpt: Optional[str], sources: list[dict], min_len: int = 24) -> bool:
    """
    Check that a model-supplied excerpt really appears in the fetched text.

    This is the guard that separates extraction from invention. Compared on
    collapsed whitespace, since the model tends to tidy spacing.
    """
    if not excerpt or len(excerpt.strip()) < min_len:
        return False
    needle = " ".join(excerpt.split()).lower()
    for s in sources:
        haystack = " ".join((s.get("text") or "").split()).lower()
        if needle in haystack:
            return True
    return False


def search(query: str, max_results: int = 5) -> dict:
    """
    Optional research step: look for additional official sources.

    Uses Anthropic's server-side web search. If it is unavailable for the configured
    model, that is reported back rather than raised, so the session continues using
    only the sources the user supplied.
    """
    from backend.tools.llm_extractor import MODEL, _get_client

    client = _get_client()
    for tool_version in ("web_search_20260209", "web_search_20250305"):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=1500,
                tools=[{"type": tool_version, "name": "web_search", "max_uses": max_results}],
                messages=[{"role": "user", "content": query}],
            )
            text = " ".join(b.text for b in resp.content if b.type == "text")
            results: list[dict[str, Any]] = []
            for block in resp.content:
                if block.type == "web_search_tool_result":
                    content = getattr(block, "content", None)
                    if isinstance(content, list):
                        for r in content:
                            results.append(
                                {"title": getattr(r, "title", None), "url": getattr(r, "url", None)}
                            )
            return {"available": True, "tool": tool_version, "summary": text, "results": results}
        except Exception as e:  # noqa: BLE001 - any failure means "fall back"
            log.info("web search via %s unavailable: %s", tool_version, str(e)[:160])
            continue

    return {
        "available": False,
        "tool": None,
        "summary": "Web search is unavailable for the configured model; "
                   "working only from the sources you supplied.",
        "results": [],
    }
