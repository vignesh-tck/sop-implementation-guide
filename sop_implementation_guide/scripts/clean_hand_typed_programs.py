"""
Delete the four hand-typed rows that predate the discovery-driven catalog.

The Grants.gov synced rows carry program_key `grants.gov:*`, and rows created by
a discovery session carry `<domain>:<slug>` (a real hostname). The four legacy
entries had short slugs like `md-pos` or `epa-brownfields-assessment` — no colon
prefix in the way the new rows do.

Deleting them leaves a "watch the silver layer fill from nothing" demo: an empty
catalog for local/state programs, ready for a discovery session against a real
Maryland DNR URL. That was the point of the whole rebuild.

Usage:
    # Preview — always run this first
    python -m scripts.clean_hand_typed_programs

    # Actually delete
    python -m scripts.clean_hand_typed_programs --confirm

The script is idempotent (a second run finds nothing to delete) and never touches
rows that already have a real provenance record.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.db.session import get_supabase  # noqa: E402


def find_hand_typed(db) -> list[dict]:
    """
    Rows this script considers hand-typed. Deliberately narrow:

    - `program_key` has no ':' — every synced or extracted row uses a colon-
      prefixed key (`grants.gov:…`, `dnr.maryland.gov:…`).
    - `is_extracted` is FALSE — an extracted row is never deleted, even if its
      key somehow lacks a colon.
    - `discovery_thread_id` is NULL — a row that came through a discovery
      session is never deleted.

    Any one of those three failsafes is enough on its own to exclude a real row.
    All three together make this hard to get wrong.
    """
    result = db.table("funding_programs").select("*").execute()
    rows = result.data or []
    suspects = []
    for r in rows:
        key = r.get("program_key") or ""
        if ":" in key:
            continue  # synced or extracted — has a domain prefix
        if r.get("is_extracted"):
            continue
        if r.get("discovery_thread_id"):
            continue
        suspects.append(r)
    return suspects


def preview(rows: list[dict]) -> None:
    if not rows:
        print("Nothing to clean — catalogue already free of hand-typed rows.")
        return
    print(f"Found {len(rows)} hand-typed row(s):\n")
    for r in rows:
        print(f"  program_key={r.get('program_key')!r}")
        print(f"    name:        {r.get('program_name')}")
        print(f"    type/agency: {r.get('program_type')} / {r.get('source_agency')}")
        print(f"    source_name: {r.get('source_name')}")
        print(f"    is_extracted: {r.get('is_extracted')}  reviewed_by_user: {r.get('reviewed_by_user')}")
        print()


def delete(db, rows: list[dict]) -> int:
    """Delete by program_key, one at a time — safe with any RLS."""
    deleted = 0
    for r in rows:
        key = r["program_key"]
        db.table("funding_programs").delete().eq("program_key", key).execute()
        print(f"  deleted {key!r}")
        deleted += 1
    return deleted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete. Without this flag the script only previews.",
    )
    args = parser.parse_args()

    db = get_supabase()
    suspects = find_hand_typed(db)
    preview(suspects)

    if not suspects:
        return

    if not args.confirm:
        print("Re-run with --confirm to delete these rows.")
        return

    print(f"\nDeleting {len(suspects)} row(s)…")
    n = delete(db, suspects)
    print(f"\nDone — deleted {n} row(s).")
    print("The catalogue now only contains rows with real provenance.")


if __name__ == "__main__":
    main()
