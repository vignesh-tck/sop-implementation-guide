"""
Pipeline Development: chat-built funding (and later zoning) pipelines.

A pipeline is read whole with GET /pipelines/{id} (definition, step outputs,
coverage, load gate, chat). Agent turns and runs take minutes, so they run in
a background thread: POST returns at once, the pipeline reports `busy`, and the
client polls GET until it clears. Panel actions are refused while busy, so the
planner and the agent never edit the definition at the same time.

Uploads reuse POST /funding/discovery/uploads, which returns public URLs that
are then added here as url sources.
"""

import threading
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import Client

from backend.agents import pipeline_builder
from backend.db.session import get_supabase
from backend.pipelines import kinds, service
from backend.pipelines.engine import PipelineError
from backend.repositories.datasets import DatasetTablesMissing
from backend.repositories.pipelines import (
    PipelineMessageRepository,
    PipelineRepository,
    PipelineTablesMissing,
)

router = APIRouter(prefix="/pipelines", tags=["pipelines"])

MISSING_TABLES = ("Pipelines are off until their tables exist: paste the PIPELINES section of "
                  "backend/db/schema.sql into the Supabase SQL editor and run it.")


def _call(fn):
    try:
        return fn()
    except (PipelineTablesMissing, DatasetTablesMissing):
        raise HTTPException(status_code=503, detail=MISSING_TABLES)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PipelineError as e:
        raise HTTPException(status_code=422, detail=str(e))


def _idle(db: Client, pipeline_id: int) -> None:
    p = _call(lambda: service.get(db, pipeline_id))
    if PipelineRepository.is_busy(p):
        raise HTTPException(status_code=409, detail="The pipeline is busy (the agent or a run is working). Wait for it to finish.")


def _view(db: Client, pipeline_id: int) -> dict:
    v = service.view(db, pipeline_id)
    v["messages"] = pipeline_builder.display(PipelineMessageRepository(db).for_pipeline(pipeline_id))
    return v


@router.get("/kinds")
def list_kinds():
    return [{"kind": k, "label": s["label"], "available": s["available"]} for k, s in kinds.KINDS.items()]


@router.get("")
def list_pipelines(kind: Optional[str] = None, db: Client = Depends(get_supabase)):
    rows = _call(lambda: PipelineRepository(db).list(kind))
    out = []
    for r in rows:
        d = r.pop("definition") or {}
        out.append({**r, "busy": r["busy"] if PipelineRepository.is_busy(r) else None,
                    "fields": len((d.get("schema") or {}).get("fields") or []),
                    "sources": len(d.get("sources") or []), "steps": len(d.get("steps") or [])})
    return out


class CreatePipeline(BaseModel):
    kind: Literal["funding", "zoning"]
    name: Optional[str] = None
    created_by: Optional[str] = None


@router.post("")
def create_pipeline(body: CreatePipeline, db: Client = Depends(get_supabase)):
    p = _call(lambda: service.create(db, body.kind, body.name or "", body.created_by))
    return _view(db, p["id"])


@router.get("/{pipeline_id}")
def get_pipeline(pipeline_id: int, db: Client = Depends(get_supabase)):
    return _call(lambda: _view(db, pipeline_id))


@router.delete("/{pipeline_id}")
def delete_pipeline(pipeline_id: int, db: Client = Depends(get_supabase)):
    """Delete a pipeline and its chat. Dataset versions it loaded are kept."""
    _idle(db, pipeline_id)
    _call(lambda: PipelineRepository(db).delete(pipeline_id))
    return {"deleted": pipeline_id}


class Rename(BaseModel):
    name: str


@router.patch("/{pipeline_id}")
def rename_pipeline(pipeline_id: int, body: Rename, db: Client = Depends(get_supabase)):
    name = body.name.strip()[:80]
    if not name:
        raise HTTPException(status_code=422, detail="A name is required.")
    _call(lambda: PipelineRepository(db).update(pipeline_id, {"name": name}))
    return _view(db, pipeline_id)


# ── Chat ─────────────────────────────────────────────────────────────────────

class Message(BaseModel):
    text: str


@router.post("/{pipeline_id}/messages", status_code=202)
def send_message(pipeline_id: int, body: Message, db: Client = Depends(get_supabase)):
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Message is empty.")
    _call(lambda: service.get(db, pipeline_id))
    if not _call(lambda: PipelineRepository(db).claim(pipeline_id, "turn")):
        raise HTTPException(status_code=409, detail="The agent is still working on the last message.")
    threading.Thread(target=pipeline_builder.run_turn, args=(pipeline_id, text), daemon=True).start()
    return {"status": "working", "poll": f"GET /pipelines/{pipeline_id}"}


# ── Panel actions (the planner's) ────────────────────────────────────────────

class Fields(BaseModel):
    fields: list[dict]


@router.put("/{pipeline_id}/schema")
def set_schema(pipeline_id: int, body: Fields, db: Client = Depends(get_supabase)):
    _idle(db, pipeline_id)
    _call(lambda: service.set_schema(db, pipeline_id, body.fields, by_user=True))
    return _view(db, pipeline_id)


@router.post("/{pipeline_id}/schema/default")
def default_schema(pipeline_id: int, db: Client = Depends(get_supabase)):
    _idle(db, pipeline_id)
    _call(lambda: service.use_default_schema(db, pipeline_id))
    return _view(db, pipeline_id)


@router.post("/{pipeline_id}/schema/approve")
def approve_schema(pipeline_id: int, db: Client = Depends(get_supabase)):
    _idle(db, pipeline_id)
    _call(lambda: service.approve_schema(db, pipeline_id))
    return _view(db, pipeline_id)


class AcceptBlank(BaseModel):
    accept: bool


@router.post("/{pipeline_id}/fields/{field}/accept-blank")
def accept_blank(pipeline_id: int, field: str, body: AcceptBlank, db: Client = Depends(get_supabase)):
    _idle(db, pipeline_id)
    _call(lambda: service.accept_blank(db, pipeline_id, field, body.accept))
    return _view(db, pipeline_id)


class Source(BaseModel):
    kind: Literal["url", "grants_gov"] = "url"
    url: Optional[str] = None
    query: Optional[str] = None
    label: Optional[str] = None


@router.post("/{pipeline_id}/sources")
def add_source(pipeline_id: int, body: Source, db: Client = Depends(get_supabase)):
    _idle(db, pipeline_id)
    _call(lambda: service.add_source(db, pipeline_id, body.kind, body.url, body.query, body.label, by_user=True))
    return _view(db, pipeline_id)


@router.delete("/{pipeline_id}/sources/{source_id}")
def remove_source(pipeline_id: int, source_id: str, db: Client = Depends(get_supabase)):
    _idle(db, pipeline_id)
    _call(lambda: service.remove_source(db, pipeline_id, source_id, by_user=True))
    return _view(db, pipeline_id)


@router.post("/{pipeline_id}/steps/{step_id}/approve")
def approve_step(pipeline_id: int, step_id: str, db: Client = Depends(get_supabase)):
    _idle(db, pipeline_id)
    _call(lambda: service.approve_step(db, pipeline_id, step_id))
    return _view(db, pipeline_id)


class StepEdit(BaseModel):
    config: Optional[dict] = None
    note: Optional[str] = None


@router.patch("/{pipeline_id}/steps/{step_id}")
def edit_step(pipeline_id: int, step_id: str, body: StepEdit, db: Client = Depends(get_supabase)):
    _idle(db, pipeline_id)
    _call(lambda: service.update_step(db, pipeline_id, step_id, body.config, body.note, by_user=True))
    return _view(db, pipeline_id)


@router.delete("/{pipeline_id}/steps/{step_id}")
def remove_step(pipeline_id: int, step_id: str, db: Client = Depends(get_supabase)):
    _idle(db, pipeline_id)
    _call(lambda: service.remove_step(db, pipeline_id, step_id, by_user=True))
    return _view(db, pipeline_id)


class Move(BaseModel):
    direction: Literal[-1, 1]


@router.post("/{pipeline_id}/steps/{step_id}/move")
def move_step(pipeline_id: int, step_id: str, body: Move, db: Client = Depends(get_supabase)):
    _idle(db, pipeline_id)
    _call(lambda: service.move_step(db, pipeline_id, step_id, body.direction))
    return _view(db, pipeline_id)


class RunRequest(BaseModel):
    upto: Optional[str] = None
    fresh: bool = False     # refetch sources and ignore cached model output: a real re-run


def _run_bg(pipeline_id: int, upto: Optional[str], fresh: bool) -> None:
    db = get_supabase()
    try:
        service.run(db, pipeline_id, upto=upto, fresh=fresh)
    except Exception as e:  # noqa: BLE001
        service.event(db, pipeline_id, f"Run failed: {type(e).__name__}: {e}", level="error")
    finally:
        PipelineRepository(db).release(pipeline_id)


@router.post("/{pipeline_id}/run", status_code=202)
def run_pipeline(pipeline_id: int, body: RunRequest, db: Client = Depends(get_supabase)):
    _call(lambda: service.get(db, pipeline_id))
    if not _call(lambda: PipelineRepository(db).claim(pipeline_id, "run")):
        raise HTTPException(status_code=409, detail="The pipeline is busy.")
    threading.Thread(target=_run_bg, args=(pipeline_id, body.upto, body.fresh), daemon=True).start()
    return {"status": "running", "poll": f"GET /pipelines/{pipeline_id}"}


@router.get("/{pipeline_id}/diff")
def get_diff(pipeline_id: int, db: Client = Depends(get_supabase)):
    """What loading the last run would change in the working data, against the latest version."""
    return _call(lambda: service.preview_diff(db, pipeline_id))


class LoadRequest(BaseModel):
    name: Optional[str] = None          # optional name for the new dataset version
    created_by: Optional[str] = None


@router.post("/{pipeline_id}/load")
def load_pipeline(pipeline_id: int, body: LoadRequest, db: Client = Depends(get_supabase)):
    _idle(db, pipeline_id)
    result = _call(lambda: service.load(db, pipeline_id, body.name, body.created_by))
    return {**result, "pipeline": _view(db, pipeline_id)}
