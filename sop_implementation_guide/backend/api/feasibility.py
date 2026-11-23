"""
Feasibility assessments: an LLM judgement, per recommendation, of how likely it is to get done.

A recommendation can be assessed once its implementation plan is approved and the block has an approved
funding analysis and zoning review (see backend/agents/feasibility.py). A run takes 30-60 s, so it goes in
the background: POST starts it, and the recommendation's GET shows it running, then awaiting review. The
SME accepts the score or sets another with a note (POST /feasibility/{id}/review); that makes it current and
recomputes the block's gold profile, whose feasibility_score is the best current assessment. Or the SME sends
it back with their own input (POST /feasibility/{id}/rework), and a new run reworks the judgement.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import Client

from backend.agents import feasibility
from backend.agents.deps import get_orchestrator
from backend.agents.orchestrator import OrchestratorAgent
from backend.db.session import get_supabase
from backend.repositories.feasibility import FeasibilityTableMissing

router = APIRouter(tags=["feasibility"])

MISSING_TABLE = (
    "Feasibility assessments are off until their table exists: paste the FEASIBILITY ASSESSMENTS section of "
    "backend/db/schema.sql into the Supabase SQL editor and run it."
)


def _guard(fn):
    try:
        return fn()
    except FeasibilityTableMissing:
        raise HTTPException(status_code=503, detail=MISSING_TABLE)
    except feasibility.FeasibilityError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


class StartRequest(BaseModel):
    rec_ids: Optional[list[int]] = None   # every ready recommendation when omitted
    created_by: Optional[str] = None


@router.post("/blocks/{block_id}/feasibility")
def start_block(block_id: int, body: Optional[StartRequest] = None, db: Client = Depends(get_supabase)):
    """Assess the block's ready recommendations (or the ones named), each in the background."""
    body = body or StartRequest()
    return _guard(lambda: feasibility.start(db, block_id, body.rec_ids, body.created_by))


@router.get("/blocks/{block_id}/recommendations/{rec_id}/feasibility")
def get_for_rec(block_id: int, rec_id: int, db: Client = Depends(get_supabase)):
    """One recommendation's feasibility: readiness, the current assessment, any open one, and whether it is stale."""
    try:
        return _guard(lambda: feasibility.for_rec(db, block_id, rec_id))
    except HTTPException as e:
        if e.status_code != 503:
            raise
        # Without the table the readiness still shows, so the page can say what to do.
        ctx = feasibility.context(db, block_id)
        rec = ctx["recs"].get(rec_id) or {"id": rec_id}
        return {"block_id": block_id, "rec_id": rec_id, "storage_ready": False, "detail": MISSING_TABLE,
                "missing": feasibility.missing_for(ctx, rec), "current": None, "open": None, "stale": [],
                "open_stale": [], "history": []}


@router.post("/blocks/{block_id}/recommendations/{rec_id}/feasibility")
def start_rec(block_id: int, rec_id: int, body: Optional[StartRequest] = None, db: Client = Depends(get_supabase)):
    """Assess one recommendation. 422 with the reasons when it is not ready."""
    body = body or StartRequest()
    out = _guard(lambda: feasibility.start(db, block_id, [rec_id], body.created_by))
    if not out["started"]:
        reasons = [m["text"] for s in out["skipped"] for m in s["missing"]]
        raise HTTPException(status_code=422, detail=" ".join(reasons) or "Not ready to assess.")
    return out


class Review(BaseModel):
    action: Literal["accept", "set"]
    approved_by: str
    score: Optional[int] = None
    cost_low: Optional[float] = None
    cost_high: Optional[float] = None
    note: Optional[str] = None


@router.post("/feasibility/{assessment_id}/review")
def review(
    assessment_id: int,
    body: Review,
    db: Client = Depends(get_supabase),
    orchestrator: OrchestratorAgent = Depends(get_orchestrator),
):
    """Approve an assessment: it becomes the recommendation's current one, and the block's gold profile is recomputed."""
    a = _guard(lambda: feasibility.review(db, assessment_id, body.action, body.approved_by, body.score,
                                          body.cost_low, body.cost_high, body.note))
    profile = orchestrator.compute_gold_for_block(a["block_id"], db=db)
    return {"assessment": a, "profile": profile}


class Rework(BaseModel):
    guidance: str
    made_by: str


@router.post("/feasibility/{assessment_id}/rework")
def rework(assessment_id: int, body: Rework, db: Client = Depends(get_supabase)):
    """
    Send an assessment back with the SME's input. A new run reads the input and this assessment and reworks the
    judgement; it waits for review like any other. The current assessment stays current until the rework is approved.
    """
    return _guard(lambda: feasibility.rework(db, assessment_id, body.guidance, body.made_by))


@router.post("/feasibility/{assessment_id}/discard")
def discard(assessment_id: int, db: Client = Depends(get_supabase)):
    return _guard(lambda: feasibility.discard(db, assessment_id))
