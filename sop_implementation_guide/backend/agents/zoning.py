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

    def score_block(self, block_id: int, db: Client | None = None) -> tuple[float | None, dict, list]:
        """(score or None, detail, zoning_signals ids) for the gold layer."""
        db = db or get_supabase()
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
