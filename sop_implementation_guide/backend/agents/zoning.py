"""
ZoningAgent — is each recommendation allowed on its block, and what does that
mean for the plan.

Unlike FundingAgent there is no model and no LangGraph graph here, on purpose.
Whether a use is allowed in a zone is written in the Zoning Ordinance, and a
parcel's zone is on the county map, so both are looked up and cited rather
than inferred (see backend/tools/zoning.py). The agent reads the zones that
scripts/sync_block_zoning.py stored, applies the use tables, and returns a
finding per recommendation plus the block's zoning sub-score.

Human review happens in the implementation plan, not in a separate interrupt:
each finding is attached to the plan's zoning steps with its references and
any questions, and the SME confirms or edits it there when finalising.

Versions: a Block Zoning analysis (analyze()) is tied to one Zoning dataset
version and pinned to the block. Every per-block read here (the block's
findings, its plan, its gold score) evaluates against the pinned analysis's
version, falling back to the latest version for a block never analysed. The
Zoning tab's overview() shows the data in force, which is what the next
snapshot is taken from.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from supabase import Client

from backend.agents.base import SpecialistAgent
from backend.config import settings
from backend.db.session import get_supabase
from backend.repositories.datasets import BlockAnalysisRepository, DatasetTablesMissing, DatasetVersionRepository
from backend.repositories.recommendations import BlockRepository
from backend.repositories.zoning import ZoningRepository
from backend.tools import datasets, zoning

REVIEW_NOTE = (
    "Zoning findings are reviewed in each recommendation's implementation plan, "
    "where the SME confirms or edits the zoning steps."
)


def analysis_label(a: dict | None) -> dict | None:
    """The short form of an analysis that travels with block responses."""
    if not a:
        return None
    return {"id": a["id"], "kind": a.get("kind"), "name": a.get("name"), "status": a.get("status"),
            "dataset_version": a.get("dataset_version"), "created_at": a.get("created_at"),
            "created_by": a.get("created_by")}


def _statuses(recs: list[dict], rows: list[dict], block: dict) -> dict[str, int]:
    """How many of the block's mapped recommendations land in each finding status."""
    out: dict[str, int] = {}
    for rec in recs:
        if zoning.mapping_for(rec.get("rec_label")):
            st = zoning.assess({**rec, "block_id": block["id"]}, rows, block)["status"]
            out[st] = out.get(st, 0) + 1
    return out


class ZoningAgent(SpecialistAgent):
    def build_graph(self):
        # Deterministic: a lookup plus a table, so there is no graph to build.
        return None

    @contextmanager
    def scope(self, block_id: int, db: Client) -> Iterator[tuple[list[dict], dict]]:
        """
        The zoning data this block is tied to, made current for the block of code.

        Yields (the block's zone rows, tie). tie names the pinned analysis and
        the dataset version in use, and its basis: 'pinned', 'latest' (never
        analysed, so the newest version applies) or 'in_force' (no versions
        exist yet, so the working data applies, as before versioning).
        """
        try:
            analysis = BlockAnalysisRepository(db).pinned(block_id, "zoning")
            version = (DatasetVersionRepository(db).get_by_id(analysis["dataset_version_id"])
                       if analysis and analysis.get("dataset_version_id") else datasets.resolve(db, "zoning"))
        except DatasetTablesMissing:
            analysis, version = None, None
        with datasets.zoning_scope(db, version) as bundle:
            rows = bundle["rows"].get(block_id, []) if bundle else ZoningRepository(db).get_for_block(block_id)
            tie = {
                "analysis": analysis_label(analysis),
                "dataset_version": datasets.label(version),
                "basis": "pinned" if analysis else ("latest" if version else "in_force"),
            }
            yield rows, tie

    def assess_block(self, block_id: int, db: Client | None = None) -> dict:
        db = db or get_supabase()
        block = BlockRepository(db).get_with_recs(block_id)
        if not block:
            return {"status": "not_found", "agent": "ZoningAgent", "block_id": block_id}
        with self.scope(block_id, db) as (rows, tie):
            lookup = zoning.block_lookup(rows)
            recs = block.get("recommendations", [])
            score, detail = zoning.score_block(recs, rows)
            findings = [
                {"rec_id": r.get("id"), "rec_label": r.get("rec_label"), "direction": r.get("direction"),
                 **zoning.assess(r, rows, block)}
                for r in recs if zoning.mapping_for(r.get("rec_label"))
            ]
        return {
            "status": "ok" if lookup else "not_looked_up",
            "agent": "ZoningAgent",
            "block_id": block_id,
            "zones": lookup["zones"] if lookup else [],
            "lookup": lookup,
            "zoning_score": score,
            "score_detail": detail,
            "findings": findings,
            **tie,
        }

    def analyze(self, db: Client, block_ids: list[int] | None = None, version: int | None = None,
                name: str | None = None, created_by: str | None = None, pin: bool = True) -> dict:
        """
        Run Block Zoning against one Zoning dataset version and record it.

        block_ids None means every block; version None means the latest. Each
        block gets an analysis row with its score, zones and finding counts.
        With pin (the default) it becomes the block's pinned zoning analysis,
        which plans and the gold layer read. Raises ValueError for an unknown
        version or block.
        """
        v = datasets.resolve(db, "zoning", version)
        if v is None:
            raise ValueError(f"Zoning version {version} does not exist." if version is not None
                             else "There is no Zoning dataset version yet: run scripts/seed_dataset_versions.py.")
        query = db.table("blocks").select("id, street_name").order("id")
        if block_ids:
            query = query.in_("id", block_ids)
        blocks = query.execute().data or []
        if block_ids and len(blocks) != len(set(block_ids)):
            raise ValueError(f"Unknown block(s): {sorted(set(block_ids) - {b['id'] for b in blocks})}")

        recs_by_block: dict[int, list[dict]] = {}
        for r in BlockRepository(db).all_recommendations():
            recs_by_block.setdefault(r["block_id"], []).append(r)

        now = datetime.now(timezone.utc).isoformat()
        new_rows = []
        with datasets.zoning_scope(db, v) as bundle:
            for b in blocks:
                rows = bundle["rows"].get(b["id"], [])
                recs = recs_by_block.get(b["id"], [])
                score, detail = zoning.score_block(recs, rows)
                new_rows.append({
                    "block_id": b["id"], "kind": "zoning",
                    "dataset_version_id": v["id"], "dataset_version": v["version"],
                    "name": (name or "").strip() or None,
                    "status": "complete", "pinned": False,
                    "created_by": (created_by or "").strip() or None,
                    "completed_at": now,
                    "result": {
                        "zoning_score": score, "score_detail": detail,
                        "zones": (zoning.block_lookup(rows) or {}).get("zones", []),
                        "statuses": _statuses(recs, rows, b),
                    },
                })

        repo = BlockAnalysisRepository(db)
        created = repo.create_many(new_rows)
        if pin and created:
            repo.unpin([a["block_id"] for a in created], "zoning")
            for a in created:
                repo.update(a["id"], {"pinned": True})
        return {
            "kind": "zoning",
            "dataset_version": datasets.label(v),
            "pinned": bool(pin),
            "analyses": [{"block_id": a["block_id"], "analysis_id": a["id"],
                          "zoning_score": (a.get("result") or {}).get("zoning_score")} for a in created],
        }

    def overview(self, db: Client | None = None) -> dict:
        """
        Everything behind the zoning findings, for the Zoning tab.

        Per block: zones, prior zones, case numbers, overlays, when it was looked
        up, its score and how many findings still need the SME. Per
        recommendation type: the ordinance uses it is read as, and the outcome in
        every zone found on our blocks. Plus every source with its date. Three
        queries in all; the rest is computed here from the stored rows.
        """
        db = db or get_supabase()
        data_version = zoning.refresh_active(db)
        rules = zoning.load_rules()
        use_src = zoning.load_use_table()["source"]
        block_repo = BlockRepository(db)

        streets = {
            b["id"]: b.get("street_name")
            for b in (db.table("blocks").select("id, street_name").order("id").execute().data or [])
        }
        rows_by_block: dict[int, list[dict]] = {}
        for r in ZoningRepository(db).get_all():
            rows_by_block.setdefault(r["block_id"], []).append(r)
        recs_by_block: dict[int, list[dict]] = {}
        for r in block_repo.all_recommendations():
            recs_by_block.setdefault(r["block_id"], []).append(r)

        blocks, zone_blocks = [], {}
        statuses_total: dict[str, int] = {}
        for block_id, street in streets.items():
            rows = rows_by_block.get(block_id, [])
            lookup = zoning.block_lookup(rows)
            recs = recs_by_block.get(block_id, [])
            score, detail = zoning.score_block(recs, rows)
            statuses: dict[str, int] = {}
            block = {"id": block_id, "street_name": street}
            for rec in recs:
                if zoning.mapping_for(rec.get("rec_label")):
                    st = zoning.assess(rec, rows, block)["status"]
                    statuses[st] = statuses.get(st, 0) + 1
                    statuses_total[st] = statuses_total.get(st, 0) + 1
            for code in (lookup or {}).get("zones", []):
                zone_blocks.setdefault(code, []).append(block_id)
            cases = (lookup or {}).get("cases") or {}
            blocks.append({
                "id": block_id,
                "street_name": street,
                "looked_up": lookup is not None,
                "zones": (lookup or {}).get("zones", []),
                "prior_zones": (lookup or {}).get("prior_zones", []),
                "cases": cases,
                "case_count": sum(len(v) for v in cases.values()),
                "overlays": (lookup or {}).get("overlays", []),
                "fetched_at": (lookup or {}).get("fetched_at"),
                "zoning_score": score,
                "scored": detail["scored"],
                "unscorable": detail["unscorable"],
                "statuses": statuses,
            })

        # Zones in the order they front the most blocks.
        present = sorted(zone_blocks, key=lambda c: (-len(zone_blocks[c]), c))
        zones = []
        for code in present:
            zinfo = rules["zones"].get(code, {})
            zones.append({
                "code": code,
                "name": zinfo.get("name"),
                "legacy": bool(zinfo.get("legacy")),
                "blocks": zone_blocks[code],
                "context": [
                    {"text": c["text"], "ref": zoning.source_ref(c["ref"]["source"], c["ref"].get("page"),
                                                                 c["ref"].get("detail"))}
                    for c in zinfo.get("context", [])
                ],
            })

        rec_counts: dict[str, int] = {}
        for recs in recs_by_block.values():
            for r in recs:
                rec_counts[r.get("rec_label")] = rec_counts.get(r.get("rec_label"), 0) + 1
        matrix_rows = []
        for label, mapping in rules["rec_uses"].items():
            cells = {}
            for code in present:
                f = zoning.zone_finding(code, mapping)
                cells[code] = {"outcome": f["outcome"], "uses": f["uses"]}
            matrix_rows.append({
                "rec_label": label,
                "mode": mapping["mode"],
                "confidence": mapping.get("confidence"),
                "note": mapping.get("note"),
                "uses": mapping["uses"],
                "rec_count": rec_counts.get(label, 0),
                "cells": cells,
            })

        sources = [
            {**zoning.source_ref(k), "key": k} for k in rules["sources"]
        ] + [{
            "key": "use_table",
            "label": use_src["name"],
            "url": use_src["url"],
            "detail": f"Current through {use_src['amended_through']}; extracted {use_src['extracted']} "
                      f"by {use_src['extracted_by']}",
        }]
        fetched = sorted(b["fetched_at"] for b in blocks if b["fetched_at"])
        return {
            "data_version": data_version,
            "settings": zoning.active_settings(),
            "rules_version": rules["version"],
            "rules_status": rules["status"],
            "buffer_m": rules.get("buffer_m", 25),
            "use_table": {
                "amended_through": use_src["amended_through"],
                "extracted": use_src["extracted"],
                "url": use_src["url"],
                "key": zoning.load_use_table()["key"],
                "unlisted_rule": zoning.load_use_table()["unlisted_rule"],
            },
            "legacy_rule": {**rules["legacy_rule"], "ref": zoning.source_ref(
                rules["legacy_rule"]["ref"]["source"], detail=rules["legacy_rule"]["ref"].get("detail"))},
            "overlays_checked": sorted(rules.get("overlay_layers", {}).values()),
            "overlays_not_checked": rules.get("overlays_not_checked"),
            "case_layers": sorted(rules.get("case_layers", {}).values()),
            "score_points": {
                "by_right": settings.zoning_points_by_right,
                "special_exception": settings.zoning_points_special_exception,
                "prohibited": settings.zoning_points_prohibited,
            },
            "stats": {
                "blocks": len(blocks),
                "looked_up": sum(b["looked_up"] for b in blocks),
                "first_lookup": fetched[0] if fetched else None,
                "last_lookup": fetched[-1] if fetched else None,
                "legacy_blocks": sum(any(rules["zones"].get(z, {}).get("legacy") for z in b["zones"]) for b in blocks),
                "overlay_blocks": sum(bool(b["overlays"]) for b in blocks),
                "findings": statuses_total,
            },
            "sources": sources,
            "zones": zones,
            "blocks": blocks,
            "matrix": {"zones": present, "rows": matrix_rows},
        }

    def score_block(self, block_id: int, db: Client | None = None) -> tuple[float | None, dict, list]:
        """(score or None, detail plus the version tie, zoning_signals ids) for the gold layer."""
        db = db or get_supabase()
        block = BlockRepository(db).get_with_recs(block_id) or {}
        with self.scope(block_id, db) as (rows, tie):
            score, detail = zoning.score_block(block.get("recommendations", []), rows)
        return score, {**detail, **tie}, [r["id"] for r in rows if r.get("id") is not None]

    def run(self, block_id: int, thread_id: str) -> dict:
        return self.assess_block(block_id)

    def get_pending_review(self, thread_id: str) -> dict:
        return {"status": "review_in_plan", "agent": "ZoningAgent", "note": REVIEW_NOTE}

    def apply_feedback(self, thread_id: str, feedback: str) -> dict:
        return {"status": "review_in_plan", "agent": "ZoningAgent", "note": REVIEW_NOTE}
