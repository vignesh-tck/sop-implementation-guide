"""
Look up the zones fronting every block and store them in zoning_signals.

Run from the sop_implementation_guide folder:
    python -m scripts.sync_block_zoning            # every block
    python -m scripts.sync_block_zoning --block 15 # one block

For each block, queries the county GIS (backend/tools/zoning_lookup.py) for the
zones within 25 m of the street centerline, plus the prior zones, development
case numbers and overlays, and writes one zoning_signals row per zone. The API
and agents read only that table, never the county server.

Safety rails, as in sync_tract_eligibility:
  - Before trusting any result, confirm the zoning layer has polygons in our
    study area. Zero would mean the layer moved, and every block would wrongly
    come back "no zone".
  - A block whose lookup fails keeps its previous rows and is reported. It is
    never overwritten with "no zone".
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.db.session import get_supabase
from backend.repositories.zoning import ZoningRepository
from backend.tools import zoning, zoning_lookup

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("sync_block_zoning")


def bbox(blocks: list[dict]) -> tuple[float, float, float, float]:
    pts = []
    for b in blocks:
        for path in zoning_lookup._paths(b["geometry_geojson"]):
            pts += path
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def rows_for(block_id: int, lookup: dict, run_id: str) -> list[dict]:
    """One row per zone; a lookup that found no zone still writes one row, with current_zone NULL."""
    rules = zoning.load_rules()
    park = zoning.mapping_for("Park/Playground")
    mixed = zoning.mapping_for("Vertical mixed used buildings")

    def allows(code: str, mapping: dict) -> bool | None:
        outcome = zoning.zone_finding(code, mapping)["outcome"]
        if outcome in ("by_right", "special_exception"):
            return True
        return False if outcome == "prohibited" else None

    raw = {"lookup": lookup, "rules_version": rules["version"]}
    if not lookup["zones"]:
        return [{
            "block_id": block_id, "current_zone": None, "zone_description": None,
            "barrier_notes": f"No zoning polygon within {lookup['buffer_m']} m of the block.",
            "raw_data": raw, "thread_id": run_id, "reviewed": False,
        }]
    out = []
    for code in lookup["zones"]:
        zinfo = rules["zones"].get(code, {})
        out.append({
            "block_id": block_id,
            "current_zone": code,
            "zone_description": zinfo.get("name"),
            "allows_parks": allows(code, park),
            "allows_mixed_use": allows(code, mixed),
            "barrier_notes": (
                "Legacy zone: uses are set by the site's approved plans (§27-4205(c)(3))."
                if zinfo.get("legacy") else None
            ),
            "raw_data": raw,
            "thread_id": run_id,
            "reviewed": False,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--block", type=int, action="append", help="Only these block ids (repeatable)")
    args = ap.parse_args()

    db = get_supabase()
    repo = ZoningRepository(db)
    rules = zoning.load_rules()

    query = db.table("blocks").select("id, street_name, geometry_geojson").order("id")
    if args.block:
        query = query.in_("id", args.block)
    blocks = [b for b in (query.execute().data or []) if b.get("geometry_geojson")]
    if not blocks:
        log.error("No blocks with geometry found — load the Bowie data first.")
        return 1

    try:
        covered = zoning_lookup.sanity_check(bbox(blocks))
    except zoning_lookup.ZoningLookupError as e:
        log.error("Could not reach the county zoning layer: %s", e)
        return 1
    log.info("Zoning layer has %d polygons in the study area", covered)
    if covered == 0:
        log.error("Zero zoning polygons around our blocks. The layer has likely moved or changed; "
                  "refusing to record every block as having no zone.")
        return 1

    run_id = f"zoning-sync-{date.today().isoformat()}"
    ok = failed = 0
    for b in blocks:
        try:
            lookup = zoning_lookup.lookup_block(b["geometry_geojson"], rules)
        except zoning_lookup.ZoningLookupError as e:
            log.warning("  block %s -> lookup FAILED (%s); keeping its previous rows", b["id"], e)
            failed += 1
            continue
        repo.replace_for_block(b["id"], rows_for(b["id"], lookup, run_id))
        extras = []
        if lookup["cases"]:
            extras.append(f"{sum(len(v) for v in lookup['cases'].values())} cases")
        if lookup["overlays"]:
            extras.append("overlays: " + ", ".join(o["layer"] for o in lookup["overlays"]))
        log.info("  block %s (%s) -> %s%s", b["id"], b.get("street_name") or "",
                 ", ".join(lookup["zones"]) or "no zone", f" [{'; '.join(extras)}]" if extras else "")
        ok += 1

    log.info("Looked up %d blocks (%d failed)", ok, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
