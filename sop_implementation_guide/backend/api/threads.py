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
    feedback: str = Body(default="approved", embed=True),
    orchestrator: OrchestratorAgent = Depends(get_orchestrator),
):
    return orchestrator.approve_funding(thread_id, feedback)


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
