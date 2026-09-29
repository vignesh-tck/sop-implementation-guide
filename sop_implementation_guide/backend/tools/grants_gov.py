"""
Grants.gov protocol adapter.

This is deliberately *only* an adapter. It exists because Grants.gov needs POST with
a JSON body, so the generic GET fetcher in web_source.py cannot read it — not
because Grants.gov is a privileged source.

Everything that used to make decisions here has been removed: the keyword list, the
funding-category filter, the applicant-eligibility filter, and the title relevance
ranking. Those were invisible to the user and amounted to the tool deciding what
counted as relevant funding. The user's stated goal now supplies the search terms,
and the user reviews the results.

No API key required.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.grants.gov/v1/api"

# Recognises a pasted Grants.gov URL so the discovery agent can route it here
# instead of through the plain GET fetcher.
HOST_MARKERS = ("grants.gov",)


def handles(url: str) -> bool:
    """True when this adapter should be used for a user-supplied source URL."""
    return any(m in (url or "").lower() for m in HOST_MARKERS)


class GrantsGovError(RuntimeError):
    """Grants.gov was unreachable or returned an unusable response."""


def _post(path: str, payload: dict, timeout: float = 30.0) -> dict:
    try:
        resp = httpx.post(f"{BASE}/{path}", json=payload, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPError as e:
        raise GrantsGovError(f"{path} request failed: {e}") from e
    except ValueError as e:
        raise GrantsGovError(f"{path} returned non-JSON: {e}") from e

    # The API returns HTTP 200 with a non-zero errorcode on logical failures.
    if body.get("errorcode") not in (0, None):
        raise GrantsGovError(f"{path} error {body.get('errorcode')}: {body.get('msg')}")
    return body


def clean_text(text: Any, limit: int = 2000) -> Optional[str]:
    """Strip HTML entities and tags from prose fields. Also used by web_source."""
    if not text:
        return None
    plain = html.unescape(str(text))
    plain = re.sub(r"<[^>]+>", " ", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain[:limit] or None


def search(keyword: str, rows: int = 15, **extra: Any) -> list[dict]:
    """
    Raw keyword search. The caller supplies the keyword — no defaults, no filters.

    `extra` passes through any additional Search2 parameters the user or agent
    decides on (for example fundingCategories or eligibilities), so those choices
    are made in the session rather than hidden here.
    """
    payload: dict[str, Any] = {"keyword": keyword, "rows": rows, "oppStatuses": "posted|forecasted"}
    payload.update(extra)
    body = _post("search2", payload)
    hits = (body.get("data") or {}).get("oppHits") or []
    log.info("Grants.gov %r -> %d hits", keyword, len(hits))
    return hits


def as_source_text(keyword: str, rows: int = 10) -> tuple[str, list[dict]]:
    """
    Render a search into readable text for the discovery agent to reason over,
    alongside the raw hits so nothing is lost.
    """
    hits = search(keyword, rows=rows)
    lines = [f"Grants.gov search for {keyword!r} returned {len(hits)} opportunities:"]
    for h in hits:
        lines.append(
            f"- id={h.get('id')} | {clean_text(h.get('title'), 200)} "
            f"| agency: {h.get('agency')} | opens {h.get('openDate')} | closes {h.get('closeDate')}"
        )
    return "\n".join(lines), hits
