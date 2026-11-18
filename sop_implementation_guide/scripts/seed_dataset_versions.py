"""
Bring the data that predates versioning in as version 1, once.

Run from the sop_implementation_guide folder, after pasting the DATASET
VERSIONS AND BLOCK ANALYSES section of backend/db/schema.sql into Supabase:
    python -m scripts.seed_dataset_versions --dry-run   # report only
    python -m scripts.seed_dataset_versions

Steps, each skipped when already done, so it is safe to re-run:
  1. Funding v1 "Initial catalogue": a snapshot of funding_programs.
  2. Zoning v1 "Initial zoning data": the use table, rules, GIS settings and
     zoning_signals in force.
  3. Funding signals with no analysis are grouped by block, and each block
     gets an "Imported" funding analysis on Funding v1, pinned. Those signals
     were approved before versions existed, against whatever the catalogue
     held at the time; v1 is the closest snapshot, and the note says so.
  4. Every block with no pinned zoning analysis is analysed on Zoning v1 and
     pinned.
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.agents.zoning import ZoningAgent
from backend.db.session import get_supabase
from backend.repositories.datasets import BlockAnalysisRepository, DatasetTablesMissing
from backend.tools import datasets

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("seed_dataset_versions")

SOURCE = {"type": "seed", "script": "scripts/seed_dataset_versions.py"}
IMPORTED_NOTE = ("Approved before dataset versions existed, against the catalogue in force at the "
                 "time. Funding v1 is the closest snapshot, not necessarily the exact one assessed.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Report what would be done; write nothing")
    args = ap.parse_args()
    dry = args.dry_run

    db = get_supabase()
    analyses = BlockAnalysisRepository(db)
    try:
        funding_v = datasets.resolve(db, "funding")
        zoning_v = datasets.resolve(db, "zoning")
        analyses.pinned_for_kind("zoning")
    except DatasetTablesMissing:
        log.error("The dataset tables are missing. Paste the DATASET VERSIONS AND BLOCK ANALYSES section "
                  "of backend/db/schema.sql into the Supabase SQL editor first.")
        return 1

    # 1 + 2: version 1 of each kind
    if funding_v:
        log.info("Funding: v%s already exists, not snapshotting", funding_v["version"])
    elif dry:
        n = len(db.table("funding_programs").select("id").execute().data or [])
        log.info("Funding: would snapshot %d programme(s) as v1", n)
    else:
        funding_v = datasets.snapshot_funding(db, name="Initial catalogue", source=SOURCE, created_by="seed")
        log.info("Funding: saved v%s (%s programmes)", funding_v["version"], funding_v["record_count"])

    if zoning_v:
        log.info("Zoning: v%s already exists, not snapshotting", zoning_v["version"])
    elif dry:
        log.info("Zoning: would snapshot the use table, rules, settings and zoning_signals as v1")
    else:
        zoning_v = datasets.snapshot_zoning(db, name="Initial zoning data", source=SOURCE, created_by="seed")
        log.info("Zoning: saved v%s (%s records)", zoning_v["version"], zoning_v["record_count"])

    # 3: legacy funding signals -> one imported analysis per block
    legacy = db.table("funding_signals").select("id, block_id, thread_id, rec_id").is_(
        "analysis_id", "null").execute().data or []
    by_block: dict[int, list[dict]] = {}
    for s in legacy:
        by_block.setdefault(s["block_id"], []).append(s)
    pinned_funding = analyses.pinned_for_kind("funding")
    log.info("Funding signals without an analysis: %d across %d block(s)", len(legacy), len(by_block))
    for block_id, rows in sorted(by_block.items()):
        if dry:
            log.info("  block %s: would import %d signal(s)", block_id, len(rows))
            continue
        a = analyses.create({
            "block_id": block_id, "kind": "funding",
            "dataset_version_id": funding_v["id"], "dataset_version": funding_v["version"],
            "name": "Imported", "note": IMPORTED_NOTE, "status": "complete",
            "thread_id": next((r["thread_id"] for r in rows if r.get("thread_id")), None),
            "created_by": "seed", "completed_at": datetime.now(timezone.utc).isoformat(),
            "result": {"signals": len(rows), "recommendations": len({r.get("rec_id") for r in rows}),
                       "imported": True},
        })
        db.table("funding_signals").update({"analysis_id": a["id"]}).eq("block_id", block_id).is_(
            "analysis_id", "null").execute()
        if block_id not in pinned_funding:
            analyses.pin(a)
        log.info("  block %s: %d signal(s) -> analysis %s%s", block_id, len(rows), a["id"],
                 "" if block_id in pinned_funding else " (pinned)")

    # 4: zoning analyses on v1 for blocks with none pinned
    pinned_zoning = analyses.pinned_for_kind("zoning")
    block_ids = [b["id"] for b in (db.table("blocks").select("id").order("id").execute().data or [])]
    todo = [b for b in block_ids if b not in pinned_zoning]
    if not todo:
        log.info("Zoning: every block already has a pinned analysis")
    elif dry:
        log.info("Zoning: would analyse %d block(s) on v1 and pin", len(todo))
    else:
        out = ZoningAgent().analyze(db, todo, zoning_v["version"], name="Initial", created_by="seed")
        log.info("Zoning: analysed and pinned %d block(s) on Zoning v%s",
                 len(out["analyses"]), out["dataset_version"]["version"])

    log.info("Done%s.", " (dry run, nothing written)" if dry else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
