"""
Store a user-uploaded document and hand back a public URL.

The rest of the discovery pipeline only knows how to read sources by URL
(backend/tools/web_source.py), so this module's one job is to turn uploaded bytes into
a URL that fetch() can read like any other source. Uses Supabase Storage — the same
project/credentials already wired up for pgvector in backend/db/session.py — rather than
a second storage vendor.

Requires a public bucket named BUCKET to exist already (created once via the Supabase
dashboard; not something this code can provision for itself).
"""

from __future__ import annotations

import uuid
from pathlib import Path

from backend.db.session import get_supabase

BUCKET = "discovery-uploads"

ALLOWED_EXT = {".txt": "text/plain", ".csv": "text/csv", ".pdf": "application/pdf"}
MAX_UPLOAD_BYTES = 15 * 1024 * 1024


def store(filename: str, content: bytes) -> str:
    """
    Upload one file, return its public URL.

    Raises ValueError on a bad extension or oversized file — caller's problem to
    report per-file, not a reason to fail the whole batch.
    """
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(f"unsupported file type {ext or '(none)'} — allowed: .txt, .csv, .pdf")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"file too large ({len(content)} bytes, max {MAX_UPLOAD_BYTES})")

    path = f"{uuid.uuid4().hex}-{Path(filename).name}"
    client = get_supabase()
    client.storage.from_(BUCKET).upload(
        path, content, file_options={"content-type": ALLOWED_EXT[ext]}
    )
    return client.storage.from_(BUCKET).get_public_url(path)
