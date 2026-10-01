"""
Funding discovery session endpoints.

Three routes, deliberately generic: the agent's current stage decides what the client
renders and what it sends back, so adding or reordering stages needs no new routes.

    POST /funding/discovery                    start a session
    GET  /funding/discovery/sessions           every past session, newest first
    GET  /funding/discovery/sessions/{id}      one past session and what it wrote
    GET  /funding/discovery/{thread_id}        what needs reviewing right now
    POST /funding/discovery/{thread_id}/respond   submit the review, advance a stage

The two /sessions routes are history, not control: they read what past sessions
left behind and never resume or re-run anything. Re-running a past session means
starting a new one from its goal and sources, which leaves the old one intact.
"""

import logging
import uuid
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.agents.funding_discovery import FundingDiscoveryAgent, extraction_fields
from backend.db.session import get_supabase
from backend.repositories.funding_programs import (
    DiscoverySessionRepository,
    FundingProgramRepository,
)

log = logging.getLogger(__name__)

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


@router.get("/fields")
def list_fields():
    """
    What a session records per programme, so the start form can say so up front.

    Served from the extraction schema rather than duplicated here — the console
    shows the user a promise, and a promise maintained in two places stops being
    true on the first change to either.
    """
    return {"fields": extraction_fields()}


# ── Session history ──────────────────────────────────────────────────────────
#
# Declared before /{thread_id}: FastAPI matches in declaration order, so putting
# these after it would make "sessions" bind as a thread_id and 404.


def _merged_sessions() -> list[dict]:
    """
    Past sessions, recorded ones taking precedence over reconstructed ones.

    Two sources because there are two eras. Sessions run since discovery_sessions
    existed have an exact record. Older ones are recovered by grouping the
    catalogue on discovery_thread_id, which is lossy but is the only trace they
    left. `record` on each row says which kind it is, so the console can be
    honest about it rather than presenting a reconstruction as fact.
    """
    db = get_supabase()
    try:
        recorded = DiscoverySessionRepository(db).list_sessions()
    except Exception as e:  # noqa: BLE001 — table may not exist yet (schema not re-run)
        # Logged, not swallowed: falling back to reconstruction is correct when the
        # table is simply absent, but silently doing it on a real DB fault would
        # hide history loss behind a thinner-looking list.
        log.warning("discovery_sessions unreadable, falling back to catalogue: %s", e)
        recorded = []

    known = {s["thread_id"] for s in recorded}
    derived = [
        s for s in FundingProgramRepository(db).sessions_from_catalogue()
        if s["thread_id"] not in known
    ]
    return sorted(
        recorded + derived,
        key=lambda s: s.get("created_at") or "",
        reverse=True,
    )


@router.get("/sessions")
def list_sessions():
    """
    Every past discovery session, newest first.

    Read-only. Listing or opening a session never re-runs it — re-running is an
    explicit new session started from the prefilled form.
    """
    return {"sessions": _merged_sessions()}


@router.get("/sessions/{thread_id}")
def get_session(thread_id: str):
    """
    One past session, with its programs checked against the live catalogue.

    The stored programs are the snapshot this session wrote. A program can since
    have been edited, or re-found by a later session that now owns the catalogue
    row — `still_in_catalogue` and `current` make that visible instead of
    implying the snapshot is still true.
    """
    db = get_supabase()
    try:
        session = DiscoverySessionRepository(db).get_session(thread_id)
    except Exception as e:  # noqa: BLE001 — table may not exist yet
        log.warning("discovery_sessions unreadable for %s: %s", thread_id, e)
        session = None

    if not session:
        session = next(
            (s for s in FundingProgramRepository(db).sessions_from_catalogue()
             if s["thread_id"] == thread_id),
            None,
        )
    if not session:
        raise HTTPException(status_code=404, detail=f"No discovery session {thread_id}.")

    programs = session.get("programs") or []
    live = FundingProgramRepository(db).get_by_keys(
        [p["program_key"] for p in programs if p.get("program_key")]
    )
    session["programs"] = [
        {
            **p,
            "still_in_catalogue": p.get("program_key") in live,
            "current": live.get(p.get("program_key")),
            # TRUE when a later session re-extracted this program and took over
            # the catalogue row. The snapshot above is still what THIS session wrote.
            "superseded_by": (
                (live.get(p.get("program_key")) or {}).get("discovery_thread_id")
                if (live.get(p.get("program_key")) or {}).get("discovery_thread_id")
                not in (None, thread_id)
                else None
            ),
        }
        for p in programs
    ]
    return session


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
