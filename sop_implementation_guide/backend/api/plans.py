"""
Implementation plans — the steps to get one recommendation done.

Select a recommendation, get its plan: the pathway template for that kind of
change, with any approved funding for that exact recommendation attached to the
funding step. A reviewer (an SME) edits the steps and finalises the plan. See
backend/tools/pathways.py for how a plan is assembled.
"""

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import Client

from backend.api.blocks import _fit_from_signal
from backend.db.session import get_supabase
from backend.repositories.funding import FundingRepository
from backend.repositories.plans import PlanRepository, PlanTableMissing
from backend.repositories.recommendations import BlockRepository
from backend.tools.pathways import build_plan, clean_step, load_templates, pathway_for

router = APIRouter(prefix="/blocks", tags=["plans"])

MISSING_TABLE = (
    "Plans cannot be saved yet: the implementation_plans table does not exist. "
    "Paste the IMPLEMENTATION PLANS section of backend/db/schema.sql into the "
    "Supabase SQL editor and run it."
)


class PlanUpdate(BaseModel):
    steps: list[dict]
    status: Literal["draft", "final"] = "draft"
    reviewed_by: Optional[str] = None
    notes: Optional[str] = None


def _block_and_rec(db: Client, block_id: int, rec_id: int) -> tuple[dict, dict]:
    block = BlockRepository(db).get_with_recs(block_id)
    if not block:
        raise HTTPException(status_code=404, detail=f"Block {block_id} not found")
    rec = next((r for r in block.get("recommendations", []) if r.get("id") == rec_id), None)
    if not rec:
        raise HTTPException(
            status_code=404, detail=f"Recommendation {rec_id} is not on block {block_id}"
        )
    return block, rec


def _funding_for(db: Client, block_id: int, rec_id: int) -> list[dict]:
    """Approved funding for one recommendation, strongest fit first."""
    signals = [
        s for s in FundingRepository(db).get_for_block(block_id, reviewed_only=True)
        if s.get("rec_id") == rec_id
    ]
    fits = [_fit_from_signal(s) for s in signals]
    fits.sort(key=lambda f: f.get("fit_score") or 0.0, reverse=True)
    return fits


def _plan_response(db: Client, block_id: int, rec: dict) -> dict:
    storage_ready = True
    try:
        saved = PlanRepository(db).get_for_rec(block_id, rec["id"])
    except PlanTableMissing:
        saved, storage_ready = None, False
    plan = build_plan(rec, _funding_for(db, block_id, rec["id"]), saved, load_templates(db))
    return {"block_id": block_id, "storage_ready": storage_ready, **plan}


@router.get("/{block_id}/plans")
def list_plans(block_id: int, db: Client = Depends(get_supabase)):
    """
    Every recommendation on the block with its pathway, plan status and how much
    approved funding it has — the list a reviewer picks from.
    """
    block = BlockRepository(db).get_with_recs(block_id)
    if not block:
        raise HTTPException(status_code=404, detail=f"Block {block_id} not found")

    storage_ready = True
    try:
        saved = {p["rec_id"]: p for p in PlanRepository(db).list_for_block(block_id)}
    except PlanTableMissing:
        saved, storage_ready = {}, False

    funding_counts: dict[int, int] = {}
    for s in FundingRepository(db).get_for_block(block_id, reviewed_only=True):
        if s.get("rec_id") is not None:
            funding_counts[s["rec_id"]] = funding_counts.get(s["rec_id"], 0) + 1

    templates = load_templates(db)
    rows = []
    for rec in block.get("recommendations", []):
        pathway = pathway_for(rec.get("rec_label"), templates)
        plan = saved.get(rec.get("id")) or {}
        rows.append({
            "rec_id": rec.get("id"),
            "rec_label": rec.get("rec_label"),
            "dimension": rec.get("dimension"),
            "direction": rec.get("direction"),
            "predicted_score_increase": rec.get("predicted_score_increase"),
            "pathway_key": pathway["key"] if pathway else None,
            "pathway_name": pathway["name"] if pathway else None,
            "plan_status": plan.get("status") or "template",
            "reviewed_by": plan.get("reviewed_by"),
            "funding_count": funding_counts.get(rec.get("id"), 0),
        })
    return {"block_id": block_id, "storage_ready": storage_ready, "recommendations": rows}


@router.get("/{block_id}/recommendations/{rec_id}/plan")
def get_plan(block_id: int, rec_id: int, db: Client = Depends(get_supabase)):
    _, rec = _block_and_rec(db, block_id, rec_id)
    return _plan_response(db, block_id, rec)


@router.put("/{block_id}/recommendations/{rec_id}/plan")
def save_plan(block_id: int, rec_id: int, body: PlanUpdate, db: Client = Depends(get_supabase)):
    """
    Save the reviewer's version of the plan, as a draft or as final.

    Only the step wording is stored. Funding is attached on read, so it stays
    current after the plan is saved.
    """
    _, rec = _block_and_rec(db, block_id, rec_id)
    templates = load_templates(db)
    pathway = pathway_for(rec.get("rec_label"), templates)
    if not pathway:
        raise HTTPException(
            status_code=422,
            detail=f"'{rec.get('rec_label')}' is not mapped to a pathway. Map it in the Pathways tab.",
        )
    reviewer = (body.reviewed_by or "").strip() or None
    if body.status == "final" and not reviewer:
        raise HTTPException(status_code=422, detail="A final plan needs the reviewer's name.")

    now = datetime.now(timezone.utc).isoformat()
    try:
        PlanRepository(db).upsert_plan({
            "block_id": block_id,
            "rec_id": rec_id,
            "rec_label": rec.get("rec_label"),
            "pathway_key": pathway["key"],
            "template_version": templates.get("version"),
            "steps": [clean_step(s) for s in body.steps],
            "status": body.status,
            "reviewed_by": reviewer,
            "notes": (body.notes or "").strip() or None,
            "updated_at": now,
            "finalized_at": now if body.status == "final" else None,
        })
    except PlanTableMissing:
        raise HTTPException(status_code=503, detail=MISSING_TABLE)
    return _plan_response(db, block_id, rec)
