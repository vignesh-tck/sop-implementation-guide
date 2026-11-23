from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import Client

from backend.agents.zoning import analysis_label
from backend.config import settings
from backend.db.session import get_supabase
from backend.repositories.datasets import BlockAnalysisRepository
from backend.repositories.recommendations import BlockRepository
from backend.repositories.funding import FundingRepository
from backend.repositories.gold import GoldRepository
from backend.agents.deps import get_orchestrator
from backend.agents.orchestrator import OrchestratorAgent

router = APIRouter(prefix="/blocks", tags=["blocks"])


@router.get("/")
def list_blocks(db: Client = Depends(get_supabase)):
    return BlockRepository(db).list_summary()


@router.get("/{block_id}")
def get_block(block_id: int, db: Client = Depends(get_supabase)):
    block = BlockRepository(db).get_with_recs(block_id)
    if not block:
        raise HTTPException(status_code=404, detail=f"Block {block_id} not found")
    return block


@router.get("/{block_id}/profile")
def get_profile(block_id: int, db: Client = Depends(get_supabase)):
    profile = GoldRepository(db).get_for_block(block_id)
    if not profile:
        raise HTTPException(status_code=404, detail="No profile yet — run /analyze first")
    return profile


def _fit_from_signal(signal: dict) -> dict:
    """
    Reshape a stored funding_signals row into the fit shape the review view uses.

    Two columns are renamed rather than passed through: relevance_score is the
    fit score the reviewer settled on, and eligibility_notes holds the
    per-recommendation narrative write_signals put there. Everything the agent had
    no column for lives in raw_data, so it is unpacked back out here.
    """
    raw = signal.get("raw_data") or {}
    return {
        "signal_id": signal.get("id"),
        "program_name": signal.get("program_name"),
        "program_type": signal.get("program_type"),
        "source_agency": signal.get("source_agency"),
        "fit_score": signal.get("relevance_score"),
        "narrative": signal.get("eligibility_notes"),
        "recommended_action": raw.get("recommended_action"),
        "deadline": signal.get("deadline"),
        "award_amount_min": signal.get("award_amount_min"),
        "award_amount_max": signal.get("award_amount_max"),
        # What this recommendation would ask for. None on signals approved
        # before amounts were required.
        "planned_amount": raw.get("planned_amount"),
        "amount_basis": raw.get("amount_basis"),
        "application_url": signal.get("application_url"),
        "source_name": signal.get("source_name"),
        "source_url": signal.get("source_url"),
        "source_vintage": signal.get("source_vintage"),
        "is_extracted": raw.get("is_extracted"),
        "extraction_confidence": raw.get("extraction_confidence"),
        "assessment_failed": raw.get("assessment_failed", False),
        # Provenance of the number itself — a reviewer-corrected score should not
        # read the same as one the model proposed and nobody touched.
        "score_adjusted_by_reviewer": raw.get("score_adjusted_by_reviewer", False),
        # A source the reviewer added that the run never proposed.
        "added_by_reviewer": raw.get("added_by_reviewer", False),
        "human_notes": raw.get("human_notes"),
        "thread_id": signal.get("thread_id"),
        "created_at": signal.get("created_at"),
    }


def _group_signals(recommendations: list[dict], signals: list[dict]) -> dict:
    """
    Group written signals under the recommendation each one funds.

    Only recommendations that carry at least one signal are returned. A block can
    hold 26 recommendations while an analysis assesses 5, so listing them all would
    imply 21 negative findings that were never made.

    Signals written before rec_id existed carry NULL there and match no
    recommendation. They are returned separately rather than dropped or silently
    folded into a group they were never assessed against.
    """
    by_rec: dict[int, list[dict]] = {}
    unlinked: list[dict] = []
    for s in signals:
        rec_id = s.get("rec_id")
        if rec_id is None:
            unlinked.append(_fit_from_signal(s))
        else:
            by_rec.setdefault(rec_id, []).append(_fit_from_signal(s))

    def strongest(fits: list[dict]) -> float:
        return max((f.get("fit_score") or 0.0) for f in fits) if fits else 0.0

    groups = []
    for rec in recommendations:
        fits = by_rec.get(rec.get("id"))
        if not fits:
            continue
        fits.sort(key=lambda f: f.get("fit_score") or 0.0, reverse=True)
        groups.append({
            "rec_id": rec.get("id"),
            "rec_label": rec.get("rec_label"),
            "dimension": rec.get("dimension"),
            "direction": rec.get("direction"),
            "predicted_score_increase": rec.get("predicted_score_increase"),
            "fits": fits,
        })

    # Strongest funding match first: the reason to open this view is "which
    # improvement is most fundable", not the order recommendations happen to load in.
    groups.sort(key=lambda g: strongest(g["fits"]), reverse=True)
    unlinked.sort(key=lambda f: f.get("fit_score") or 0.0, reverse=True)

    return {
        "recommendations": groups,
        "unlinked": unlinked,
        "signal_count": len(signals),
        "recommendations_total": len(recommendations),
        "default_floor": settings.relevance_floor,
    }


@router.post("/{block_id}/gold")
def compute_block_gold(
    block_id: int,
    db: Client = Depends(get_supabase),
    orchestrator: OrchestratorAgent = Depends(get_orchestrator),
):
    """Recompute the block's gold profile from its pinned funding and zoning analyses."""
    if not BlockRepository(db).get_by_id(block_id):
        raise HTTPException(status_code=404, detail=f"Block {block_id} not found")
    return orchestrator.compute_gold_for_block(block_id, db=db)


@router.get("/{block_id}/signals")
def get_signals(block_id: int, analysis_id: Optional[int] = None, db: Client = Depends(get_supabase)):
    """
    Written funding signals for a block, grouped by the recommendation they fund.

    The read-only counterpart to GET /threads/{id}/review. That endpoint serves a
    live thread's proposal from an in-memory checkpointer and disappears on
    restart; this one serves what was actually approved and written, so a past
    analysis stays inspectable. The shapes match so one renderer covers both.

    An empty result is a 200, not a 404 — "this block has no funding signals yet"
    is a normal state, distinct from the block not existing.

    By default the block's pinned (current) funding analysis; analysis_id shows
    any other run of this block, read-only, without changing which is current.
    """
    block = BlockRepository(db).get_with_recs(block_id)
    if not block:
        raise HTTPException(status_code=404, detail=f"Block {block_id} not found")

    repo = BlockAnalysisRepository(db)
    pinned = repo.pinned(block_id, "funding")
    shown = pinned
    if analysis_id is not None:
        shown = repo.get(analysis_id)
        if not shown or shown["block_id"] != block_id or shown["kind"] != "funding":
            raise HTTPException(status_code=404, detail=f"Funding analysis {analysis_id} is not on block {block_id}")
    signals = FundingRepository(db).get_for_block(block_id, analysis_id=shown["id"] if shown else None)
    return {"block_id": block_id, "analysis": analysis_label(shown),
            "pinned_analysis_id": pinned["id"] if pinned else None,
            **_group_signals(block.get("recommendations", []), signals)}


class AnalyzeRequest(BaseModel):
    version: Optional[int] = None      # Funding dataset version; latest when omitted
    name: Optional[str] = None
    created_by: Optional[str] = None


@router.post("/{block_id}/analyze")
def start_analysis(
    block_id: int,
    body: Optional[AnalyzeRequest] = None,
    db: Client = Depends(get_supabase),
    orchestrator: OrchestratorAgent = Depends(get_orchestrator),
):
    """Start a Block Funding analysis against a Funding version (the latest unless named)."""
    if not BlockRepository(db).get_by_id(block_id):
        raise HTTPException(status_code=404, detail=f"Block {block_id} not found")

    body = body or AnalyzeRequest()
    try:
        result = orchestrator.run_funding(block_id, body.version, body.name, body.created_by)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {
        "block_id": block_id,
        "thread_id": result["thread_id"],
        "analysis_id": result["state"].get("analysis_id"),
        "status": "awaiting human review",
        "next_step": f"GET /threads/{result['thread_id']}/review",
    }
