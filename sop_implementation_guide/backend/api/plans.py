"""
Implementation plans — the steps to get one recommendation done.

Select a recommendation, get its plan: the pathway template for that kind of
change, with any approved funding for that exact recommendation attached to the
funding step and its approved zoning finding (from the block's approved Block
Zoning run) attached to the zoning steps. See backend/tools/pathways.py for how
a plan is assembled.

A reviewer (an SME) edits the steps and saves them as a draft. A draft changes
nothing anyone else reads until someone approves it; then its steps become the
plan in implementation_plans, which the block page reads.
Every draft, approval and discarded draft is kept in
implementation_plan_revisions, with what it changed, as the plan's audit trail.
"""

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import Client

from backend.agents.zoning import ZoningAgent, analysis_label, best_by_rec
from backend.api.blocks import _fit_from_signal
from backend.db.session import get_supabase
from backend.repositories.datasets import BlockAnalysisRepository, DatasetTablesMissing
from backend.repositories.funding import FundingRepository
from backend.repositories.plans import PlanRepository, PlanRevisionRepository, PlanTableMissing, RevisionTableMissing
from backend.repositories.recommendations import BlockRepository
from backend.tools.pathways import build_plan, clean_step, clean_steps, load_templates, pathway_for
from backend.tools.plan_graph import diff_steps, estimate, upgrade_steps

router = APIRouter(prefix="/blocks", tags=["plans"])

MISSING_TABLE = (
    "Plans cannot be saved yet: the implementation_plans table does not exist. "
    "Paste the IMPLEMENTATION PLANS section of backend/db/schema.sql into the "
    "Supabase SQL editor and run it."
)


MISSING_REVISIONS = (
    "Plans cannot be saved yet: the implementation_plan_revisions table (the audit "
    "trail) does not exist. Paste the IMPLEMENTATION PLAN REVISIONS section of "
    "backend/db/schema.sql into the Supabase SQL editor and run it."
)


class PlanUpdate(BaseModel):
    steps: list[dict]
    # 'final' saves the draft and approves it in one go.
    status: Literal["draft", "final"] = "draft"
    made_by: Optional[str] = None
    reviewed_by: Optional[str] = None   # older clients
    notes: Optional[str] = None


class PlanAction(BaseModel):
    made_by: str
    notes: Optional[str] = None


def _name(value: str | None, doing: str) -> str:
    name = (value or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail=f"{doing} needs your name, for the audit trail.")
    return name


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


def _analyses(db: Client, block_id: int, zoning_tie: dict) -> dict:
    """Which Block Funding / Block Zoning analysis (and dataset version) this response reads."""
    try:
        funding = analysis_label(BlockAnalysisRepository(db).pinned(block_id, "funding"))
    except DatasetTablesMissing:
        funding = None
    return {"funding": funding, "zoning": zoning_tie}


def _approved(db: Client, block_id: int, rec_id: int) -> dict | None:
    """The approved plan row, or None while the plan is still the playbook's."""
    saved = PlanRepository(db).get_for_rec(block_id, rec_id)
    # Rows saved before drafts and approval were separate could hold a draft.
    return saved if saved and saved.get("status") == "final" else None


def _pending_draft(db: Client, block_id: int, rec_id: int) -> dict | None:
    try:
        latest = PlanRevisionRepository(db).latest_for_rec(block_id, rec_id)
    except RevisionTableMissing:
        return None
    return latest if latest and latest.get("action") == "draft" else None


def _baseline(saved: dict | None, pathway: dict | None) -> tuple[list[dict], int | None]:
    """The steps a change is measured against: the approved plan, else the playbook."""
    if saved:
        return upgrade_steps(saved.get("steps") or []), saved.get("revision")
    return upgrade_steps([clean_step(s) for s in (pathway or {}).get("steps") or []]), None


def _plan_response(db: Client, block: dict, rec: dict) -> dict:
    block_id = block["id"]
    storage_ready = history_ready = True
    try:
        saved = _approved(db, block_id, rec["id"])
    except PlanTableMissing:
        saved, storage_ready = None, False
    draft_row = None
    try:
        latest = PlanRevisionRepository(db).latest_for_rec(block_id, rec["id"])
        draft_row = latest if latest and latest.get("action") == "draft" else None
    except RevisionTableMissing:
        history_ready = False

    templates = load_templates(db)
    funding = _funding_for(db, block_id, rec["id"])
    finding, tie = ZoningAgent().finding_for(block_id, rec["id"], db)
    plan = build_plan(rec, funding, saved, templates, zoning=finding, block=block)

    draft = None
    if draft_row:
        as_saved = {"steps": draft_row.get("steps") or [], "status": "draft", "reviewed_by": draft_row.get("made_by"),
                    "updated_at": draft_row.get("created_at"), "notes": draft_row.get("notes"),
                    "template_version": draft_row.get("template_version")}
        dplan = build_plan(rec, funding, as_saved, templates, zoning=finding, block=block)
        base_steps, _ = _baseline(saved, pathway_for(rec.get("rec_label"), templates))
        draft = {
            "revision": draft_row["revision"],
            "made_by": draft_row.get("made_by"),
            "created_at": draft_row.get("created_at"),
            "notes": draft_row.get("notes"),
            "base_revision": draft_row.get("base_revision"),
            "steps": dplan["steps"],
            "estimate": dplan["estimate"],
            # Against the approved plan as it stands now, not as it stood when the draft was saved.
            "changes": diff_steps(base_steps, upgrade_steps(draft_row.get("steps") or [])),
        }
    return {
        "block_id": block_id, "storage_ready": storage_ready, "history_ready": history_ready,
        "analyses": _analyses(db, block_id, tie), **plan,
        "revision": saved.get("revision") if saved else None,
        "draft": draft,
    }


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
        saved = {p["rec_id"]: p for p in PlanRepository(db).list_for_block(block_id) if p.get("status") == "final"}
    except PlanTableMissing:
        saved, storage_ready = {}, False
    try:
        latest = PlanRevisionRepository(db).latest_actions_for_block(block_id)
    except RevisionTableMissing:
        latest = {}

    funding_counts: dict[int, int] = {}
    for s in FundingRepository(db).get_for_block(block_id, reviewed_only=True):
        if s.get("rec_id") is not None:
            funding_counts[s["rec_id"]] = funding_counts.get(s["rec_id"], 0) + 1

    templates = load_templates(db)
    rows = []
    pinned, items = ZoningAgent().approved(block_id, db)
    zoning_best_by_rec = best_by_rec(items)
    tie = {"analysis": analysis_label(pinned) if items is not None else None,
           "dataset_version": pinned.get("dataset_version") if items is not None else None,
           "basis": "approved" if items is not None else "not_reviewed"}
    for rec in block.get("recommendations", []):
        pathway = pathway_for(rec.get("rec_label"), templates)
        plan = saved.get(rec.get("id")) or {}
        zoning_best = zoning_best_by_rec.get(rec.get("id"))
        last = latest.get(rec.get("id")) or {}
        steps, _ = _baseline(plan or None, pathway)
        rows.append({
            "rec_id": rec.get("id"),
            "rec_label": rec.get("rec_label"),
            "dimension": rec.get("dimension"),
            "direction": rec.get("direction"),
            "predicted_score_increase": rec.get("predicted_score_increase"),
            "pathway_key": pathway["key"] if pathway else None,
            "pathway_name": pathway["name"] if pathway else None,
            # 'final' once a plan is approved; 'template' while it is the playbook's.
            "plan_status": plan.get("status") or "template",
            "reviewed_by": plan.get("reviewed_by"),
            "revision": plan.get("revision"),
            "draft_pending": last.get("action") == "draft",
            "draft_by": last.get("made_by") if last.get("action") == "draft" else None,
            # The approved plan's rough length (the playbook's before approval).
            "estimate": estimate(steps) if steps else None,
            "funding_count": funding_counts.get(rec.get("id"), 0),
            # Best zoning outcome on the block (by_right / special_exception /
            # prohibited), or None when unmapped, unresolved or not looked up.
            "zoning_best": zoning_best,
        })
    return {"block_id": block_id, "storage_ready": storage_ready,
            "analyses": _analyses(db, block_id, tie), "recommendations": rows}


@router.get("/{block_id}/recommendations/{rec_id}/plan")
def get_plan(block_id: int, rec_id: int, db: Client = Depends(get_supabase)):
    block, rec = _block_and_rec(db, block_id, rec_id)
    return _plan_response(db, block, rec)


def _next_revision(revs: PlanRevisionRepository, block_id: int, rec_id: int) -> int:
    latest = revs.latest_for_rec(block_id, rec_id)
    return (latest["revision"] if latest else 0) + 1


def _pathway_or_422(rec: dict, templates: dict) -> dict:
    pathway = pathway_for(rec.get("rec_label"), templates)
    if not pathway:
        raise HTTPException(
            status_code=422,
            detail=f"'{rec.get('rec_label')}' is not in a playbook, so it has no implementation plan. Add it to one in the Playbooks tab.",
        )
    return pathway


def _record(db: Client, block_id: int, rec: dict, action: str, made_by: str, notes: str | None,
            steps: list[dict] | None, templates: dict, pathway: dict) -> int:
    """Append one revision, with what it changes against the approved plan."""
    revs = PlanRevisionRepository(db)
    saved = _approved(db, block_id, rec["id"])
    base_steps, base_rev = _baseline(saved, pathway)
    revision = _next_revision(revs, block_id, rec["id"])
    revs.insert({
        "block_id": block_id,
        "rec_id": rec["id"],
        "rec_label": rec.get("rec_label"),
        "revision": revision,
        "action": action,
        "steps": steps,
        "changes": diff_steps(base_steps, steps) if steps is not None else None,
        "base_revision": base_rev,
        "pathway_key": pathway["key"],
        "template_version": templates.get("version"),
        "made_by": made_by,
        "notes": notes,
    })
    return revision


def _approve(db: Client, block_id: int, rec: dict, made_by: str, notes: str | None,
             steps: list[dict], templates: dict, pathway: dict) -> None:
    revision = _record(db, block_id, rec, "approved", made_by, notes, steps, templates, pathway)
    now = datetime.now(timezone.utc).isoformat()
    PlanRepository(db).upsert_plan({
        "block_id": block_id,
        "rec_id": rec["id"],
        "rec_label": rec.get("rec_label"),
        "pathway_key": pathway["key"],
        "template_version": templates.get("version"),
        "steps": steps,
        "status": "final",
        "reviewed_by": made_by,
        "notes": notes,
        "updated_at": now,
        "finalized_at": now,
        "revision": revision,
    })


@router.put("/{block_id}/recommendations/{rec_id}/plan")
def save_plan(block_id: int, rec_id: int, body: PlanUpdate, db: Client = Depends(get_supabase)):
    """
    Save the reviewer's steps as a draft, or (status 'final') save and approve them.

    Only the steps are stored. Funding and zoning are attached on read, so they
    stay current after the plan is approved.
    """
    block, rec = _block_and_rec(db, block_id, rec_id)
    templates = load_templates(db)
    pathway = _pathway_or_422(rec, templates)
    made_by = _name(body.made_by or body.reviewed_by, "Saving a plan")
    notes = (body.notes or "").strip() or None
    try:
        steps = clean_steps(body.steps)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    try:
        _record(db, block_id, rec, "draft", made_by, notes, steps, templates, pathway)
        if body.status == "final":
            _approve(db, block_id, rec, made_by, notes, steps, templates, pathway)
    except RevisionTableMissing:
        raise HTTPException(status_code=503, detail=MISSING_REVISIONS)
    except PlanTableMissing:
        raise HTTPException(status_code=503, detail=MISSING_TABLE)
    return _plan_response(db, block, rec)


@router.post("/{block_id}/recommendations/{rec_id}/plan/approve")
def approve_plan(block_id: int, rec_id: int, body: PlanAction, db: Client = Depends(get_supabase)):
    """Approve the draft waiting on this plan: its steps become the plan everything reads."""
    block, rec = _block_and_rec(db, block_id, rec_id)
    templates = load_templates(db)
    pathway = _pathway_or_422(rec, templates)
    made_by = _name(body.made_by, "Approving a plan")
    try:
        draft = _pending_draft(db, block_id, rec_id)
        if not draft:
            raise HTTPException(status_code=409, detail="No draft is waiting for approval. Reload to see the latest.")
        notes = (body.notes or "").strip() or draft.get("notes")
        _approve(db, block_id, rec, made_by, notes, upgrade_steps(draft.get("steps") or []), templates, pathway)
    except RevisionTableMissing:
        raise HTTPException(status_code=503, detail=MISSING_REVISIONS)
    except PlanTableMissing:
        raise HTTPException(status_code=503, detail=MISSING_TABLE)
    return _plan_response(db, block, rec)


@router.post("/{block_id}/recommendations/{rec_id}/plan/discard")
def discard_draft(block_id: int, rec_id: int, body: PlanAction, db: Client = Depends(get_supabase)):
    """Drop the waiting draft. The audit trail keeps it, marked as discarded."""
    block, rec = _block_and_rec(db, block_id, rec_id)
    templates = load_templates(db)
    pathway = _pathway_or_422(rec, templates)
    made_by = _name(body.made_by, "Discarding a draft")
    try:
        if not _pending_draft(db, block_id, rec_id):
            raise HTTPException(status_code=409, detail="No draft is waiting. Reload to see the latest.")
        _record(db, block_id, rec, "discarded", made_by, (body.notes or "").strip() or None, None, templates, pathway)
    except RevisionTableMissing:
        raise HTTPException(status_code=503, detail=MISSING_REVISIONS)
    return _plan_response(db, block, rec)


@router.get("/{block_id}/recommendations/{rec_id}/plan/history")
def plan_history(block_id: int, rec_id: int, db: Client = Depends(get_supabase)):
    """
    The plan's audit trail, newest first, and the playbook it started from.

    Each revision holds its steps as they stood and what it changed, including
    the whole of any removed step, so the history can be drawn on the canvas.
    """
    _, rec = _block_and_rec(db, block_id, rec_id)
    templates = load_templates(db)
    pathway = pathway_for(rec.get("rec_label"), templates)
    try:
        rows = PlanRevisionRepository(db).list_for_rec(block_id, rec_id)
        ready = True
    except RevisionTableMissing:
        rows, ready = [], False
    revisions = [{**r, "steps": upgrade_steps(r["steps"]) if r.get("steps") is not None else None} for r in rows]
    playbook = None
    if pathway:
        steps, _ = _baseline(None, pathway)
        playbook = {"name": pathway["name"], "version": templates.get("version"), "steps": steps}
    return {"history_ready": ready, "revisions": revisions, "playbook": playbook}
