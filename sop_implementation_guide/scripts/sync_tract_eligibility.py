"""
Sync per-census-tract funding designations into Supabase.

Run from the sop_implementation_guide folder:
    python -m scripts.sync_tract_eligibility

Reads the distinct tract_geoid values off the blocks table, resolves each
designation against its published dataset, and writes the verdict plus its
provenance into tract_eligibility. The agent then reads that table instead of
asking an LLM whether a tract qualifies.

Safety rail: before trusting any negative result, the script confirms the source
layer actually covers our county. A layer that returns zero qualified tracts for
all of Prince George's County would mean a schema or vintage mismatch, and every
"not designated" verdict would be a false negative.
"""

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.db.session import get_supabase
from backend.repositories.funding_programs import TractEligibilityRepository
from backend.tools import tract_eligibility as te

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
# httpx logs every request at INFO, which buries the verdicts.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("sync_tract_eligibility")

# Prince George's County, MD — the county our Bowie blocks sit in.
COUNTY_PREFIX = "24033"


def distinct_tracts(db) -> list[str]:
    rows = db.table("blocks").select("tract_geoid").execute().data or []
    return sorted({r["tract_geoid"] for r in rows if r.get("tract_geoid")})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--designation", default="NMTC",
                    choices=sorted(te.DESIGNATION_LOOKUPS), nargs="?")
    ap.add_argument("--county-prefix", default=COUNTY_PREFIX)
    args = ap.parse_args()

    db = get_supabase()
    repo = TractEligibilityRepository(db)

    tracts = distinct_tracts(db)
    if not tracts:
        log.error("No tract_geoid values on blocks — load the Bowie data first.")
        return 1
    log.info("%d distinct tracts across the blocks table: %s", len(tracts), ", ".join(tracts))

    # Coverage check before trusting negatives.
    try:
        covered = te.sanity_check_layer(args.county_prefix)
    except te.EligibilityLookupError as e:
        log.error("Could not reach the %s source layer: %s", args.designation, e)
        return 1

    log.info("Source layer lists %d qualified tracts in county %s", covered, args.county_prefix)
    if covered == 0:
        log.error(
            "Zero qualified tracts for the whole county. That is far more likely a "
            "GEOID-format or vintage mismatch than a real result — refusing to write "
            "'not designated' verdicts that would all be false negatives."
        )
        return 1

    lookup = te.DESIGNATION_LOOKUPS[args.designation]
    ok = failed = 0
    eligible_tracts: list[str] = []
    for tract in tracts:
        try:
            row = lookup(tract)
        except te.EligibilityLookupError as e:
            # Record the uncertainty explicitly: eligible=None, never False.
            log.warning("  %s -> lookup FAILED (%s); recording eligible=None", tract, e)
            row = {
                "program_key": args.designation,
                "tract_geoid": tract,
                "eligible": None,
                "basis": f"Lookup failed: {e}",
                "source_name": te.NMTC_SOURCE["source_name"],
                "source_url": te.NMTC_SOURCE["source_url"],
                "source_vintage": te.NMTC_SOURCE["source_vintage"],
                "raw_data": None,
            }
            failed += 1
        else:
            log.info("  %s -> %s", tract, "ELIGIBLE" if row["eligible"] else "not designated")
            if row["eligible"]:
                eligible_tracts.append(tract)
            ok += 1

        repo.upsert_determination(row)

    log.info("Wrote %d determinations (%d resolved, %d failed) for %s",
             ok + failed, ok, failed, args.designation)
    log.info("%s-eligible tracts: %s", args.designation,
             ", ".join(eligible_tracts) if eligible_tracts else "none")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
