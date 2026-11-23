"""
Pathway templates — the editable steps every implementation plan starts from.

The Playbooks tab reads and saves these. A save never overwrites: it adds a new
version to pathway_templates, so a bad edit can be undone by restoring an older
version (version 0 is backend/data/pathways.json). See backend/tools/pathways.py
for how the templates in force are chosen.

Plans nobody has saved yet follow the templates live. A saved plan keeps its own
steps, so a template edit never changes a plan a reviewer has already worked on.

The tab's agent (backend/agents/playbook_agent.py) runs here too. A turn can
take a minute, longer than a proxy may hold a request open, so it runs in a
background thread: POST /pathways/agent starts it and GET /pathways/agent/{job}
returns it once done. Jobs live in memory only; the chat itself is held by the
browser, and nothing the agent does is saved until the planner saves.
"""

import logging
import threading
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import Client

from backend.db.session import get_supabase
from backend.repositories.pathway_templates import PathwayTemplateRepository, TemplateTableMissing
from backend.repositories.recommendations import BlockRepository
from backend.tools.pathways import load_file_templates, load_templates, normalize_templates

log = logging.getLogger(__name__)

router = APIRouter(prefix="/pathways", tags=["pathways"])

MISSING_TABLE = (
    "Template edits cannot be saved yet: the pathway_templates table does not exist. "
    "Paste the PATHWAY TEMPLATES section of backend/db/schema.sql into the "
    "Supabase SQL editor and run it."
)


class TemplatesUpdate(BaseModel):
    pathways: list[dict]
    rec_types: dict[str, str]
    status: Optional[str] = None
    updated_by: str
    note: Optional[str] = None
    # The version the editor started from. A save against an older version is
    # refused, so two reviewers editing at once can't silently undo each other.
    base_version: Optional[str] = None


class StepsUpdate(BaseModel):
    steps: list[dict]
    updated_by: str
    note: Optional[str] = None


class Restore(BaseModel):
    updated_by: str


def _reviewer(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Saving a template needs your name.")
    return name


def _response(db: Client) -> dict:
    templates = load_templates(db)
    history = []
    if templates["storage_ready"]:
        try:
            history = PathwayTemplateRepository(db).history()
        except TemplateTableMissing:
            pass
    known = BlockRepository(db).distinct_rec_labels()
    return {
        **templates,
        "history": history,
        "unmapped_rec_labels": [l for l in known if l not in templates["rec_types"]],
    }


def _save(db: Client, current: dict, doc: dict, updated_by: str, note: str | None) -> dict:
    if not current["storage_ready"]:
        raise HTTPException(status_code=503, detail=MISSING_TABLE)
    clean, errors = normalize_templates(doc, current["filled_by"])
    if errors:
        raise HTTPException(status_code=422, detail=" ".join(errors))
    try:
        PathwayTemplateRepository(db).insert({
            "version": current["version_number"] + 1,
            "doc": clean,
            "updated_by": updated_by,
            "note": (note or "").strip() or None,
        })
    except TemplateTableMissing:
        raise HTTPException(status_code=503, detail=MISSING_TABLE)
    return _response(db)


@router.get("")
def get_pathways(db: Client = Depends(get_supabase)):
    """
    The templates in force, their version history, and any recommendation types
    in the data that no pathway covers yet.
    """
    return _response(db)


@router.put("")
def save_pathways(body: TemplatesUpdate, db: Client = Depends(get_supabase)):
    """Save the whole template set as a new version."""
    updated_by = _reviewer(body.updated_by)
    current = load_templates(db)
    if body.base_version and body.base_version != current["version"]:
        raise HTTPException(
            status_code=409,
            detail=f"Someone saved {current['version']} while you were editing "
                   f"{body.base_version}. Reload to see their changes, then edit again.",
        )
    doc = {"status": body.status, "pathways": body.pathways, "rec_types": body.rec_types}
    return _save(db, current, doc, updated_by, body.note)


@router.put("/{key}/steps")
def save_pathway_steps(key: str, body: StepsUpdate, db: Client = Depends(get_supabase)):
    """
    Replace one pathway's steps, for example with a reviewer's finalised plan,
    so the next block starts from what the SME settled on.
    """
    updated_by = _reviewer(body.updated_by)
    current = load_templates(db)
    if not any(p["key"] == key for p in current["pathways"]):
        raise HTTPException(status_code=404, detail=f"No playbook '{key}'")
    pathways = [{**p, "steps": body.steps} if p["key"] == key else p for p in current["pathways"]]
    doc = {"status": current.get("status"), "pathways": pathways, "rec_types": current["rec_types"]}
    return _save(db, current, doc, updated_by, body.note)


@router.post("/versions/{version}/restore")
def restore_version(version: int, body: Restore, db: Client = Depends(get_supabase)):
    """Make an earlier version current again, by saving a copy of it as the newest."""
    updated_by = _reviewer(body.updated_by)
    current = load_templates(db)
    if version == 0:
        doc = load_file_templates()
        label = "the original file (v0)"
    else:
        try:
            row = PathwayTemplateRepository(db).get_version(version)
        except TemplateTableMissing:
            raise HTTPException(status_code=503, detail=MISSING_TABLE)
        if not row:
            raise HTTPException(status_code=404, detail=f"No template version v{version}")
        doc = row["doc"]
        label = f"v{version}"
    return _save(db, current, doc, updated_by, f"Restored {label}")


# ── The playbook assistant ───────────────────────────────────────────────────
#
# Chat turns and playbook drafts can take a minute, longer than a proxy may
# hold a request open, so they run in background threads: the POST returns a
# job id and GET /pathways/agent/{job} returns its progress, then its result.
# Jobs live in memory only. Quick step actions (/assist) are short and answer
# directly.

class FileRef(BaseModel):
    name: str
    url: str


class AgentTurn(BaseModel):
    text: str
    # The conversation so far, exactly as the last turn returned it.
    messages: list[dict[str, Any]] = []
    # The planner's draft with the suggestions still pending applied: {status, pathways, rec_types}.
    working: dict[str, Any]
    selected: Optional[str] = None
    mode: str = "propose"   # or "answer"
    files: list[FileRef] = []


class AssistRequest(BaseModel):
    action: str             # rewrite | estimate | owners
    working: dict[str, Any]
    playbook: str
    step_id: str
    variant: Optional[str] = None    # shorter | plainer | again
    previous: Optional[str] = None


class FitRequest(BaseModel):
    working: dict[str, Any]
    playbook: str


class DraftRequest(BaseModel):
    purpose: str
    working: dict[str, Any]
    files: list[FileRef] = []
    rec_types: list[str] = []
    model_on: Optional[str] = None


_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
JOB_TTL = 3600


def _start_job(target, *args) -> str:
    job = uuid.uuid4().hex
    now = time.time()
    with _JOBS_LOCK:
        for k in [k for k, v in _JOBS.items() if now - v["at"] > JOB_TTL]:
            del _JOBS[k]
        _JOBS[job] = {"status": "running", "at": now, "stop": False}

    def body() -> None:
        try:
            update = {"status": "done", "result": target(job, *args)}
        except Exception as e:  # noqa: BLE001 — reported in the panel, never lost in a thread
            log.exception("playbook assistant job failed")
            update = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
        with _JOBS_LOCK:
            if job in _JOBS:
                _JOBS[job].update(update)

    threading.Thread(target=body, daemon=True).start()
    return job


def _context(db: Client) -> tuple[dict, list[str]]:
    templates = load_templates(db)
    labels = sorted(set(BlockRepository(db).distinct_rec_labels()) | set(templates["rec_types"]))
    return templates["filled_by"], labels


@router.post("/agent")
def agent_start(body: AgentTurn, db: Client = Depends(get_supabase)):
    """Start one chat turn of the playbook assistant; poll GET /pathways/agent/{job} for the reply."""
    from backend.agents import playbook_agent

    if not body.text.strip():
        raise HTTPException(status_code=422, detail="Say what you would like changed.")
    sources, labels = _context(db)
    job = _start_job(lambda _job: playbook_agent.run_turn(
        body.messages, body.text, body.working, body.selected, sources, labels,
        mode="answer" if body.mode == "answer" else "propose", files=[f.model_dump() for f in body.files]))
    return {"job": job}


@router.post("/draft")
def draft_start(body: DraftRequest, db: Client = Depends(get_supabase)):
    """Start drafting a new playbook; poll GET /pathways/agent/{job} for the outline as it grows."""
    from backend.agents import playbook_agent

    if not body.purpose.strip():
        raise HTTPException(status_code=422, detail="Say what the playbook is for.")
    sources, labels = _context(db)
    model_on = next((p for p in body.working.get("pathways") or [] if p.get("key") == body.model_on), None)

    def run(job: str) -> dict:
        def progress(p: dict) -> None:
            with _JOBS_LOCK:
                if job in _JOBS:
                    _JOBS[job]["progress"] = p

        def stopped() -> bool:
            with _JOBS_LOCK:
                return bool(_JOBS.get(job, {}).get("stop"))

        return playbook_agent.draft_playbook(body.purpose, [f.model_dump() for f in body.files], body.rec_types,
                                             model_on, body.working, sources, labels, progress, stopped)

    return {"job": _start_job(run)}


@router.post("/agent/{job}/stop")
def agent_stop(job: str):
    """Stop a draft where it is; what it has written so far is kept."""
    with _JOBS_LOCK:
        if job in _JOBS:
            _JOBS[job]["stop"] = True
    return {"ok": True}


@router.get("/agent/{job}")
def agent_poll(job: str):
    with _JOBS_LOCK:
        j = _JOBS.get(job)
        if not j:
            raise HTTPException(status_code=404, detail="That assistant job is gone (the server restarted). Try again.")
        out = {k: v for k, v in j.items() if k not in ("at", "stop")}
        if j["status"] != "running":
            del _JOBS[job]
    return out


@router.post("/assist")
def assist(body: AssistRequest):
    """One quick suggestion for one step: a rewritten description, a duration, or owners."""
    from backend.agents import playbook_agent

    try:
        return playbook_agent.assist(body.action, body.working, body.playbook, body.step_id, body.variant, body.previous)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/fit")
def fit(body: FitRequest, db: Client = Depends(get_supabase)):
    """Check whether each recommendation type in a playbook belongs there, and which unmapped ones do."""
    from backend.agents import playbook_agent

    _, labels = _context(db)
    try:
        return playbook_agent.check_fit(body.working, body.playbook, labels)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
