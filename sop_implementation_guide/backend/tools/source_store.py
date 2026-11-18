"""
Saved copies of fetched pipeline sources and the pages research reads.

A source is downloaded when it is added and read from its saved copy after
that, so previews and runs never go back to the site (which may be slow, block
bots, or have changed since the planner looked). Only a fresh re-run downloads
again, and it replaces the copy. The in-memory cache in the pipeline engine
sits in front of this; this layer is what survives a server restart (the
hosted backend sleeps when idle).

Copies live in the existing public Storage bucket used for uploads. They are
copies of public pages, so a public bucket is no exposure. A failure here never
fails a run: the caller just downloads the page as before.
"""

from __future__ import annotations

import hashlib
import json
import logging

from backend.db.session import get_supabase
from backend.tools.upload_storage import BUCKET

log = logging.getLogger(__name__)

PREFIX = "pipeline-sources"


def _path(key: str) -> str:
    return f"{PREFIX}/{hashlib.sha256(key.encode()).hexdigest()[:40]}.json"


def load(key: str) -> dict | None:
    """The saved copy for this source key, or None if there is none (or Storage is unreachable)."""
    try:
        data = get_supabase().storage.from_(BUCKET).download(_path(key))
        doc = json.loads(data)
    except Exception:  # noqa: BLE001 — a missing copy is the normal case
        return None
    return doc if doc.get("key") == key else None


def save(key: str, doc: dict) -> None:
    try:
        body = json.dumps({**doc, "key": key}, default=str).encode()
        get_supabase().storage.from_(BUCKET).upload(
            _path(key), body, file_options={"content-type": "application/json", "upsert": "true"})
    except Exception as e:  # noqa: BLE001 — keep running on the in-memory copy
        log.warning("could not save a copy of %s: %s", key, e)
