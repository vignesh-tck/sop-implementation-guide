"""
Full reset: empty every table, then reload the Bowie source data from scratch.

For when the data in Supabase is wrong rather than merely stale — this leaves no
row behind that predates the reload, so nothing downstream can reference a value
you no longer trust.

Wipes all eight tables explicitly, children first, rather than leaning on the
ON DELETE CASCADE from blocks. Two reasons the cascade is not enough on its own:
block_id is nullable on all four signal tables, so an orphaned row with a NULL
block_id is reached by no cascade at all; and funding_programs and
tract_eligibility do not reference blocks in the first place.

Then reloads blocks + block_recommendations by calling load_bowie_data directly,
so there is exactly one copy of the parsing logic.

What this does NOT do: reset the identity sequences. The Supabase client speaks
PostgREST, which has no TRUNCATE, so new funding_signals ids continue from the
old high-water mark instead of restarting at 1. Harmless — ids are opaque — but
if you want them to restart, run this in the SQL Editor instead of step 1:

    truncate table
        block_implementation_profiles,
        funding_signals, zoning_signals, policy_signals,
        funding_programs, tract_eligibility,
        block_recommendations, blocks
    restart identity cascade;

and then re-run this script with --skip-wipe.

Usage:
    # Preview — shows what would be deleted, touches nothing
    python -m scripts.reset_and_reload

    # Wipe and reload
    python -m scripts.reset_and_reload --confirm

    # Also re-sync per-tract NMTC designations (makes network calls)
    python -m scripts.reset_and_reload --confirm --with-tracts

Run from the sop_implementation_guide folder. Restart the API afterwards: the
LangGraph checkpointer is in-memory, so threads created before the wipe survive
the reset and would write pre-reset signals back on approval.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.db.session import get_supabase  # noqa: E402
from scripts.load_bowie_data import load_csv, load_geojson  # noqa: E402

# Children before parents. The cascade would handle the ordering, but deleting
# explicitly is the point of this script — see the module docstring.
TABLES = [
    "block_implementation_profiles",
    "funding_signals",
    "zoning_signals",
    "policy_signals",
    "funding_programs",
    "tract_eligibility",
    "block_recommendations",
    "blocks",
]

# PostgREST refuses an unqualified DELETE, so every row-matching delete needs a
# predicate. Every table here has a positive BIGINT id, so "id <> -1" matches
# all of them and excludes nothing.
MATCH_ALL_COLUMN = "id"
MATCH_ALL_SENTINEL = -1


def count_rows(db, table: str) -> int:
    result = db.table(table).select("id", count="exact").limit(1).execute()
    return result.count or 0


def survey(db) -> dict[str, int]:
    return {t: count_rows(db, t) for t in TABLES}


def print_survey(counts: dict[str, int], header: str) -> None:
    print(f"\n{header}")
    width = max(len(t) for t in TABLES)
    for table, n in counts.items():
        print(f"  {table.ljust(width)}  {n:>6}")
    print(f"  {'TOTAL'.ljust(width)}  {sum(counts.values()):>6}")


def wipe(db) -> None:
    print("\nDeleting…")
    for table in TABLES:
        db.table(table).delete().neq(MATCH_ALL_COLUMN, MATCH_ALL_SENTINEL).execute()
        print(f"  cleared {table}")

    # Verify rather than assume. A silent RLS policy or a failed filter would
    # otherwise leave rows behind and the reload would look successful.
    remaining = {t: n for t, n in survey(db).items() if n}
    if remaining:
        detail = ", ".join(f"{t}={n}" for t, n in remaining.items())
        raise RuntimeError(
            f"Wipe incomplete — rows still present: {detail}. "
            "Check RLS policies on these tables, or truncate from the SQL Editor."
        )
    print("  ✓ all tables empty")


def reload(db) -> None:
    print("\nReloading Bowie source data…")
    load_geojson(db)
    load_csv(db)


def sync_tracts() -> int:
    """Re-run the tract designation sync in-process. Returns its exit code."""
    print("\nSyncing per-tract designations…")
    from scripts.sync_tract_eligibility import main as sync_main

    # sync_tract_eligibility parses its own args; give it none of ours.
    argv, sys.argv = sys.argv, [sys.argv[0]]
    try:
        return sync_main()
    finally:
        sys.argv = argv


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Wipe every table and reload the Bowie source data.",
    )
    ap.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete and reload. Without this flag the script only previews.",
    )
    ap.add_argument(
        "--skip-wipe",
        action="store_true",
        help="Reload only — use after truncating from the SQL Editor.",
    )
    ap.add_argument(
        "--with-tracts",
        action="store_true",
        help="Also re-run scripts.sync_tract_eligibility (makes network calls).",
    )
    args = ap.parse_args()

    db = get_supabase()
    before = survey(db)
    print_survey(before, "Current row counts:")

    if not args.confirm:
        print(
            "\nPreview only — nothing was changed."
            "\nRe-run with --confirm to delete all of the above and reload from"
            "\n  bowie_features_scores_tracts_acs.geojson"
            "\n  Final_Bowie_OrderedBlockwiseRecs_20240314 Overall.csv"
        )
        return 0

    if args.skip_wipe:
        print("\n--skip-wipe: leaving existing rows in place.")
    else:
        wipe(db)

    reload(db)

    code = sync_tracts() if args.with_tracts else 0

    print_survey(survey(db), "Row counts after reload:")
    print(
        "\nDone. Next:"
        "\n  1. Restart the API — the in-memory checkpointer still holds pre-reset threads."
        + ("" if args.with_tracts else
           "\n  2. python -m scripts.sync_tract_eligibility   (needs the blocks just loaded)")
        + "\n  " + ("2" if args.with_tracts else "3")
        + ". Repopulate funding_programs via the Funding Discovery panel."
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
