"""
Fetch whatever source the user pasted, and reduce it to text the LLM can read.

Deliberately plain httpx rather than a server-side web_fetch tool: it works with any
model, needs no beta header, and — the real reason — the fetched text stays visible to
the user, so an extracted claim can be checked against what was actually retrieved.

No source list, no keywords, no relevance rules live here. This module fetches what it
is told to fetch.
"""

from __future__ import annotations

import html
import json
import logging
import re
import unicodedata
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Optional
from urllib.parse import urljoin, urldefrag

import httpx
import pypdf

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

    Returns {url, final_url, status, content_type, text, truncated, fetched_at,
    links}. links are the page's outgoing links as [{text, url}] (anchors for
    HTML, URLs printed in the text for PDFs and plain text), so a research step
    can follow a programme to its own page.
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
    limit = max(max_text, MAX_TEXT) * 2
    text, links = _to_text(resp, ctype, url, limit)
    truncated = len(text) > max_text

    return {
        "url": url,
        "final_url": str(resp.url),
        "status": resp.status_code,
        "content_type": ctype,
        "text": text[:max_text],
        "truncated": truncated,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "links": links,
    }


def _to_text(resp: httpx.Response, ctype: str, url: str, limit: int) -> tuple[str, list[dict]]:
    if "json" in ctype:
        try:
            return json.dumps(resp.json(), indent=2)[:limit], []
        except ValueError:
            return resp.text, []

    # Suffix checked alongside content-type: our own uploads set the right type, but
    # a third-party URL serving a plain-text/CSV file with a generic or missing
    # content-type header shouldn't fall through to the HTML-stripping branch below.
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        text = _pdf_to_text(resp.content, url, limit)
        return text, text_links(text)

    if "csv" in ctype or url.lower().endswith(".csv") or ctype == "text/plain" or url.lower().endswith(".txt"):
        text = resp.text[:limit]
        return text, text_links(text)

    # HTML and anything else textual: drop script/style, then tags and entities.
    body = resp.text
    for tag in ("script", "style", "noscript", "svg"):
        body = _strip_block(body, tag)
    return clean_text(body, limit=limit) or "", html_links(body, str(resp.url))


def _pdf_to_text(data: bytes, url: str, limit: int) -> str:
    try:
        reader = pypdf.PdfReader(BytesIO(data))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        raise FetchError(f"Could not read PDF at {url}: {e}") from e
    if not text.strip():
        raise FetchError(f"{url} is a PDF with no extractable text (likely scanned/image-only).")
    return text[:limit]


# ── Links ────────────────────────────────────────────────────────────────────

_ANCHOR = re.compile(r"<a\b[^>]*?href\s*=\s*([\"'])(.*?)\1[^>]*>(.*?)</a>", re.I | re.S)
_SKIP_HREF = ("mailto:", "tel:", "javascript:", "#")
MAX_LINKS = 300


def html_links(body: str, base_url: str) -> list[dict]:
    """A page's outgoing links as [{text, url}], absolute, de-duplicated, in page order."""
    out, seen = [], set()
    for _q, href, inner in _ANCHOR.findall(body):
        href = html.unescape(href.strip())
        if not href or href.lower().startswith(_SKIP_HREF):
            continue
        absolute = urldefrag(urljoin(base_url, href))[0]
        if not absolute.lower().startswith(("http://", "https://")) or absolute in seen:
            continue
        seen.add(absolute)
        text = clean_text(inner, limit=160) or ""
        out.append({"text": text, "url": absolute})
        if len(out) >= MAX_LINKS:
            break
    return out


# Full URLs, www. hosts, and bare government/organisation hosts with a path, as
# reports print them ("dnr.maryland.gov/land/Pages/...").
_TEXT_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]+|\b[a-z0-9.-]+\.(?:gov|org|us|edu)/[^\s<>\"')\]]*", re.I)


# A URL that a PDF wrapped at a hyphen: "https://mht.maryland.gov/planning-protection/maryland-\nheritage-areas".
_WRAPPED_URL = re.compile(r"((?:https?://|www\.|\b[a-z0-9.-]+\.(?:gov|org|us|edu)/)[^\s<>\"')\]]*-)[ \t]*\r?\n[ \t]*(?=[a-z0-9])", re.I)


def text_links(text: str) -> list[dict]:
    """URLs printed in plain or PDF text, as [{text, url}]."""
    out, seen = [], set()
    text = text or ""
    for _ in range(4):   # a URL can wrap more than once
        joined = _WRAPPED_URL.sub(r"\1", text)
        if joined == text:
            break
        text = joined
    for m in _TEXT_URL.finditer(text):
        raw = m.group(0).rstrip(".,;:")
        url = raw if raw.lower().startswith("http") else "https://" + raw
        if url in seen:
            continue
        seen.add(url)
        # The words just before the URL usually name what it is.
        context = " ".join(text[max(0, m.start() - 100):m.start()].split())[-100:]
        out.append({"text": context, "url": url})
        if len(out) >= MAX_LINKS:
            break
    return out


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


_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"', "„": '"',
                         "–": "-", "—": "-", "‐": "-", "‑": "-", " ": " ", "…": "...", "­": ""})


def _norm(s: str) -> str:
    """Compare text as a reader would: case, spacing, curly quotes, dashes and PDF hyphen breaks ignored."""
    s = unicodedata.normalize("NFKC", s or "").translate(_QUOTES).lower()
    s = re.sub(r"(\w)-\s+(\w)", r"\1\2", s)     # "pro- gramme" -> "programme"
    return " ".join(s.split())


# Sentence ends, ellipses the model uses to mark a skipped span, and line breaks.
_SPLIT = re.compile(r"(?<=[.!?;:])\s+|\s*\.\.\.\s*|\s*\[\.\.\.\]\s*|\n+")
MIN_PART = 12


def check_excerpt(excerpt: Optional[str], sources: list[dict], min_len: int = 24) -> dict:
    """
    Check that a model-supplied quote is really in the fetched text.

    This is the guard that separates extraction from invention. A quote passes
    when every sentence of it appears in one source, in any order: the model
    often joins sentences that sit either side of a link list or a table, and
    that is quoting, not inventing. A sentence that appears nowhere fails the
    quote, and is returned so the reviewer can see exactly what was not found.

    Returns {verified, missing: [sentences not found], source_index}.
    """
    if not excerpt or len(excerpt.strip()) < min_len:
        return {"verified": False, "missing": [excerpt or ""], "source_index": None, "reason": "too short"}
    parts = [p.strip() for p in _SPLIT.split(excerpt) if p and len(_norm(p)) >= MIN_PART]
    if not parts:
        return {"verified": False, "missing": [excerpt], "source_index": None, "reason": "too short"}
    best = None
    for i, s in enumerate(sources):
        haystack = _norm(s.get("text") or "")
        missing = [p for p in parts if _norm(p).rstrip(".;:!?") not in haystack]
        if not missing:
            return {"verified": True, "missing": [], "source_index": i}
        if best is None or len(missing) < len(best[1]):
            best = (i, missing)
    return {"verified": False, "missing": best[1] if best else parts, "source_index": best[0] if best else None}


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
