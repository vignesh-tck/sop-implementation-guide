"""
Funding discovery session endpoints.

Three routes, deliberately generic: the agent's current stage decides what the client
renders and what it sends back, so adding or reordering stages needs no new routes.

    POST /funding/discovery                    start a session
    GET  /funding/discovery/{thread_id}        what needs reviewing right now
    POST /funding/discovery/{thread_id}/respond   submit the review, advance a stage
"""

import uuid
from functools import lru_cache
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.agents.funding_discovery import FundingDiscoveryAgent

router = APIRouter(prefix="/funding/discovery", tags=["funding discovery"])


@lru_cache(maxsize=1)
def get_discovery_agent() -> FundingDiscoveryAgent:
    """One instance, so its in-memory checkpointer is shared across requests."""
    return FundingDiscoveryAgent()


class StartSession(BaseModel):
    goal: str = Field(..., description="What the planner is trying to fund, in their words.")
    sources: list[str] = Field(..., min_length=1,
                              description="Websites or API endpoints to read.")


@router.post("")
def start_session(
    body: StartSession,
    agent: FundingDiscoveryAgent = Depends(get_discovery_agent),
):
    """
    Begin a session. Returns the first stage awaiting review.

    Nothing is written to the silver layer until the final stage is confirmed, so an
    abandoned session leaves no trace.
    """
    thread_id = f"discovery-{uuid.uuid4().hex[:8]}"
    try:
        return agent.start(thread_id, body.goal, body.sources)
    except ValueError as e:
        # Raised by structured_call when the model declines or returns nothing usable.
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/{thread_id}")
def get_stage(
    thread_id: str,
    agent: FundingDiscoveryAgent = Depends(get_discovery_agent),
):
    state = agent.current(thread_id)
    if not state:
        raise HTTPException(
            status_code=404,
            detail=f"No session {thread_id}. Sessions are held in memory, so they do not "
                   "survive a server restart — start a new one.",
        )
    return state


@router.post("/{thread_id}/respond")
def respond(
    thread_id: str,
    value: Any = Body(..., embed=True,
                      description="Whatever the current stage's `expects` field asked for."),
    agent: FundingDiscoveryAgent = Depends(get_discovery_agent),
):
    """
    Submit the review for the current stage and advance.

    The body is intentionally untyped: each stage states what it wants in its `expects`
    field (free text, a source list, edited programmes, geo overrides).
    """
    if not agent.current(thread_id):
        raise HTTPException(status_code=404, detail=f"No session {thread_id}.")
    try:
        return agent.respond(thread_id, value)
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
