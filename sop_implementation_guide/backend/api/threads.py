from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from backend.agents.deps import get_orchestrator
from backend.agents.orchestrator import OrchestratorAgent

router = APIRouter(prefix="/threads", tags=["threads"])


@router.get("/{thread_id}/review")
def get_review(
    thread_id: str,
    orchestrator: OrchestratorAgent = Depends(get_orchestrator),
):
    data = orchestrator.get_funding_review(thread_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
    return data


@router.post("/{thread_id}/approve")
def approve(
    thread_id: str,
    fits: Optional[list[dict[str, Any]]] = Body(default=None, embed=True),
    feedback: str = Body(default="approved", embed=True),
    orchestrator: OrchestratorAgent = Depends(get_orchestrator),
):
    """
    Approve a thread's funding fits.

    Send `fits` to decide per (rec_id, program_name) — each entry needs `rec_id`,
    `program_name` and `approved`, and may carry an adjusted `fit_score`. Only
    approved entries are written; rejected ones are deleted if an earlier review
    of the same pair wrote them, so re-reviewing a block can take a signal back
    and not only add one.

    An empty `fits` list is a real answer meaning "reject everything", so it is
    passed through rather than treated as absent. Omitting `fits` entirely falls
    back to `feedback`, where 'approved' keeps every fit above the default floor.

    `feedback` is carried alongside `fits` rather than replaced by it — the note is
    recorded on every signal that gets written.
    """
    payload: Any = {"fits": fits, "feedback": feedback} if fits is not None else feedback
    return orchestrator.approve_funding(thread_id, payload)


@router.post("/{thread_id}/gold")
def compute_gold(
    thread_id: str,
    orchestrator: OrchestratorAgent = Depends(get_orchestrator),
):
    """
    Compute the gold-layer profile for this thread's block.

    Takes no body — block_id is read from the thread's graph state so it cannot
    disagree with the thread that produced the signals.
    """
    try:
        return orchestrator.compute_gold(thread_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
