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
"""

from supabase import Client

from backend.agents.base import SpecialistAgent
from backend.config import settings
from backend.db.session import get_supabase
from backend.repositories.recommendations import BlockRepository
from backend.repositories.zoning import ZoningRepository
from backend.tools import zoning

REVIEW_NOTE = (
    "Zoning findings are reviewed in each recommendation's implementation plan, "
    "where the SME confirms or edits the zoning steps."
)


class ZoningAgent(SpecialistAgent):
    def build_graph(self):
        # Deterministic: a lookup plus a table, so there is no graph to build.
        return None

    def assess_block(self, block_id: int, db: Client | None = None) -> dict:
        db = db or get_supabase()
        zoning.refresh_active(db)
        block = BlockRepository(db).get_with_recs(block_id)
        if not block:
            return {"status": "not_found", "agent": "ZoningAgent", "block_id": block_id}
        rows = ZoningRepository(db).get_for_block(block_id)
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
        """(score or None, detail, zoning_signals ids) for the gold layer."""
        db = db or get_supabase()
        zoning.refresh_active(db)
        block = BlockRepository(db).get_with_recs(block_id) or {}
        rows = ZoningRepository(db).get_for_block(block_id)
        score, detail = zoning.score_block(block.get("recommendations", []), rows)
        return score, detail, [r["id"] for r in rows if r.get("id") is not None]

    def run(self, block_id: int, thread_id: str) -> dict:
        return self.assess_block(block_id)

    def get_pending_review(self, thread_id: str) -> dict:
        return {"status": "review_in_plan", "agent": "ZoningAgent", "note": REVIEW_NOTE}

    def apply_feedback(self, thread_id: str, feedback: str) -> dict:
        return {"status": "review_in_plan", "agent": "ZoningAgent", "note": REVIEW_NOTE}
