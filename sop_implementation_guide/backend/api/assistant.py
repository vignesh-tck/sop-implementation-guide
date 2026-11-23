"""
The Blocks tab's assistant: questions about a block, its recommendations, plans
and durations, or about all blocks. See backend/agents/block_assistant.py.

A turn runs as a background job, like the playbook assistant's (it shares its
in-memory job store): POST /assistant/ask, then poll GET /assistant/{job},
which reports what the assistant is reading while it works.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import Client

from backend.api.pathways import _JOBS, _JOBS_LOCK, _start_job
from backend.db.session import get_supabase

router = APIRouter(prefix="/assistant", tags=["assistant"])


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class AskRequest(BaseModel):
    question: str
    scope: Literal["block", "all"] = "block"
    block_id: Optional[int] = None
    rec_ids: list[int] = []
    history: list[Turn] = []


@router.post("/ask")
def ask(body: AskRequest, db: Client = Depends(get_supabase)):
    from backend.agents import block_assistant

    if not body.question.strip():
        raise HTTPException(status_code=422, detail="Ask a question.")
    if body.scope == "block" and body.block_id is None:
        raise HTTPException(status_code=422, detail="Pick a block, or ask across all blocks.")

    def run(job: str) -> dict:
        def progress(text: str) -> None:
            with _JOBS_LOCK:
                if job in _JOBS:
                    _JOBS[job]["progress"] = text

        return block_assistant.ask(db, body.question, body.scope, body.block_id, body.rec_ids,
                                   [t.model_dump() for t in body.history], progress)

    return {"job": _start_job(run)}


@router.get("/{job}")
def poll(job: str):
    with _JOBS_LOCK:
        j = _JOBS.get(job)
        if not j:
            raise HTTPException(status_code=404, detail="That answer is gone (the server restarted). Ask again.")
        out = {k: v for k, v in j.items() if k not in ("at", "stop")}
        if j["status"] != "running":
            del _JOBS[job]
    return out
