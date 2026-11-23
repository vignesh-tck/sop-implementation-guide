"""
Dataset versions and block analyses.

Versions: every approved write to the funding catalogue or the zoning data is
snapshotted as Funding vN / Zoning vN (see backend/tools/datasets.py). They
can be listed, inspected and given a name; their records never change.

Analyses: a Block Funding or Block Zoning run records the version it used. Run
one block, a list, or every block, against any version; the newest run is
pinned to the block (plans, signals and gold read the pinned one), and any
earlier run can be pinned again.

Both kinds are approved by the SME before anything reads them, and only an
approved run is pinned. Block Zoning is a lookup, so a run is made in the
request and waits in_review with its findings stored in the run
(GET /analyses/{id}; approve with POST /analyses/{id}/review). Block Funding
pauses its graph for the reviewer, so each block's run returns a thread to
review (GET /threads/{id}/review). A batch of funding runs goes in the
background; their analyses show progress.
"""

import logging
import threading
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import Client

from backend.agents.deps import get_orchestrator
from backend.agents.orchestrator import OrchestratorAgent
from backend.agents.zoning import ReviewError, ZoningAgent
from backend.db.session import get_supabase
from backend.repositories.datasets import (
    BlockAnalysisRepository,
    DatasetRecordRepository,
    DatasetTablesMissing,
    DatasetVersionRepository,
)
from backend.tools import datasets

log = logging.getLogger(__name__)

router = APIRouter(tags=["datasets"])

Kind = Literal["funding", "zoning"]

MISSING_TABLES = (
    "Versioning is off until its tables exist: paste the DATASET VERSIONS AND BLOCK ANALYSES "
    "section of backend/db/schema.sql into the Supabase SQL editor and run it."
)


def _guard(fn):
    try:
        return fn()
    except DatasetTablesMissing:
        raise HTTPException(status_code=503, detail=MISSING_TABLES)


# ── Versions ─────────────────────────────────────────────────────────────────

@router.get("/datasets/{kind}/versions")
def list_versions(kind: Kind, db: Client = Depends(get_supabase)):
    return {"kind": kind, "versions": _guard(lambda: DatasetVersionRepository(db).list(kind))}


@router.get("/datasets/{kind}/versions/{version}")
def get_version(kind: Kind, version: int, db: Client = Depends(get_supabase)):
    """A version with what it holds: the programmes, or a summary of the zoning data."""
    row = _guard(lambda: DatasetVersionRepository(db).get(kind, version))
    if not row:
        raise HTTPException(status_code=404, detail=f"{kind.title()} v{version} not found")
    if kind == "funding":
        programs = datasets.funding_programs(db, row["id"])
        row["programs"] = [
            {k: p.get(k) for k in ("program_key", "program_name", "program_type", "source_agency",
                                   "award_amount_min", "award_amount_max", "deadline", "geo_scope",
                                   "source_url", "application_url")}
            for p in programs
        ]
    else:
        bundle = datasets.zoning_bundle(db, row["id"])
        src = (bundle.get("use_table") or {}).get("source") or {}
        row["zoning"] = {
            "use_table": {k: src.get(k) for k in ("name", "url", "amended_through", "extracted")},
            "rules_version": (bundle.get("rules") or {}).get("version"),
            "settings": bundle.get("settings"),
            "blocks": {str(b): sorted({r["current_zone"] for r in rows if r.get("current_zone")})
                       for b, rows in sorted(bundle["rows"].items())},
        }
    return row


class VersionLabel(BaseModel):
    name: Optional[str] = None
    note: Optional[str] = None


@router.patch("/datasets/{kind}/versions/{version}")
def label_version(kind: Kind, version: int, body: VersionLabel, db: Client = Depends(get_supabase)):
    """Name (or rename) a version. Only the name and note can change; its records cannot."""
    repo = DatasetVersionRepository(db)
    row = _guard(lambda: repo.get(kind, version))
    if not row:
        raise HTTPException(status_code=404, detail=f"{kind.title()} v{version} not found")
    patch = {k: ((v or "").strip() or None) for k, v in body.model_dump(exclude_unset=True).items()}
    return repo.update_label(row["id"], patch) if patch else row


class SnapshotRequest(BaseModel):
    name: Optional[str] = None
    note: Optional[str] = None
    created_by: Optional[str] = None


@router.post("/datasets/{kind}/versions")
def create_version(kind: Kind, body: SnapshotRequest, db: Client = Depends(get_supabase)):
    """Snapshot the current working data as a new version by hand."""
    fn = datasets.snapshot_funding if kind == "funding" else datasets.snapshot_zoning
    return _guard(lambda: fn(db, name=body.name, note=body.note, created_by=body.created_by,
                             source={"type": "manual"}))


# ── Analyses ─────────────────────────────────────────────────────────────────

@router.get("/blocks/{block_id}/analyses")
def list_block_analyses(
    block_id: int,
    kind: Optional[Kind] = None,
    db: Client = Depends(get_supabase),
    orchestrator: OrchestratorAgent = Depends(get_orchestrator),
):
    """
    The block's runs, newest first. A funding run awaiting review carries
    review_live: whether its review is still held by the server (pending
    reviews live in memory, so a restart loses them; such a run can only be
    discarded and re-run).
    """
    # A zoning run's full result (every finding) is read one run at a time, from GET /analyses/{id}.
    rows = _guard(lambda: BlockAnalysisRepository(db).list_for_block(block_id, kind, light=kind == "zoning"))
    for a in rows:
        if a["kind"] == "funding" and a["status"] in ("running", "in_review") and a.get("thread_id"):
            a["review_live"] = bool(orchestrator.get_funding_review(a["thread_id"]))
    return {"block_id": block_id, "analyses": rows}


class AnalysisRun(BaseModel):
    kind: Kind
    version: Optional[int] = None          # latest when omitted
    block_ids: Optional[list[int]] = None  # every block when omitted
    name: Optional[str] = None
    created_by: Optional[str] = None


def _run_funding_batch(orchestrator: OrchestratorAgent, block_ids: list[int], body: AnalysisRun) -> None:
    for block_id in block_ids:
        try:
            orchestrator.run_funding(block_id, body.version, body.name, body.created_by)
        except Exception as e:  # noqa: BLE001  — one block failing must not stop the rest
            log.warning("batch funding analysis failed for block %s: %s", block_id, e)


@router.post("/analyses")
def run_analyses(
    body: AnalysisRun,
    db: Client = Depends(get_supabase),
    orchestrator: OrchestratorAgent = Depends(get_orchestrator),
):
    """
    Run Block Zoning or Block Funding for one block, a list, or all blocks,
    against a dataset version.
    """
    if _guard(lambda: datasets.resolve(db, body.kind, body.version)) is None:
        raise HTTPException(status_code=422, detail=(
            f"{body.kind.title()} v{body.version} does not exist." if body.version is not None
            else f"There is no {body.kind.title()} version yet: run scripts/seed_dataset_versions.py."))

    if body.kind == "zoning":
        try:
            return _guard(lambda: ZoningAgent().analyze(db, body.block_ids, body.version, body.name, body.created_by))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    block_ids = body.block_ids or [b["id"] for b in (db.table("blocks").select("id").order("id").execute().data or [])]
    if len(block_ids) == 1:
        try:
            r = orchestrator.run_funding(block_ids[0], body.version, body.name, body.created_by)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        return {"kind": "funding", "block_id": block_ids[0], "thread_id": r["thread_id"],
                "analysis_id": r["state"].get("analysis_id"), "status": "awaiting human review"}

    # Several LLM calls per block: far too long for one request, so it runs in
    # the background. Each block's analysis appears as running, then in_review.
    threading.Thread(target=_run_funding_batch, args=(orchestrator, block_ids, body), daemon=True).start()
    return {"kind": "funding", "started": len(block_ids), "status": "running in background",
            "next_step": "GET /blocks/{id}/analyses?kind=funding, then review each thread"}


@router.get("/analyses/{analysis_id}")
def get_analysis(analysis_id: int, db: Client = Depends(get_supabase)):
    """One run in full. For a zoning run, result.findings holds each finding with its proposal and decision."""
    a = _guard(lambda: BlockAnalysisRepository(db).get(analysis_id))
    if not a:
        raise HTTPException(status_code=404, detail=f"Analysis {analysis_id} not found")
    return a


class ZoningDecision(BaseModel):
    key: str
    action: Literal["accept", "set", "open"]
    outcome: Optional[str] = None
    note: Optional[str] = None


class ZoningReview(BaseModel):
    decisions: list[ZoningDecision] = []
    approved_by: str
    note: Optional[str] = None


@router.post("/analyses/{analysis_id}/review")
def approve_zoning_review(analysis_id: int, body: ZoningReview, db: Client = Depends(get_supabase)):
    """
    Approve a Block Zoning run: apply the SME's decisions, complete the run and make it the block's current
    zoning. Findings the SME did not decide stay as proposed (pre-accepted) or open (flagged).
    """
    try:
        return _guard(lambda: ZoningAgent().approve(
            db, analysis_id, [d.model_dump() for d in body.decisions], body.approved_by, body.note))
    except ReviewError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/analyses/{analysis_id}/discard")
def discard_analysis(analysis_id: int, db: Client = Depends(get_supabase)):
    """
    Drop a run that will not be finished: one still running, awaiting a review
    that was lost to a server restart, or failed. A complete run is kept as
    history; pin another one instead.
    """
    repo = BlockAnalysisRepository(db)
    a = _guard(lambda: repo.get(analysis_id))
    if not a:
        raise HTTPException(status_code=404, detail=f"Analysis {analysis_id} not found")
    if a["status"] == "complete":
        raise HTTPException(status_code=422, detail="A complete analysis is history and is kept; pin another run instead.")
    repo.update(analysis_id, {"status": "discarded", "pinned": False})
    return repo.get(analysis_id)


@router.post("/analyses/{analysis_id}/pin")
def pin_analysis(analysis_id: int, db: Client = Depends(get_supabase)):
    """Make an earlier analysis the block's current one for its kind."""
    repo = BlockAnalysisRepository(db)
    a = _guard(lambda: repo.get(analysis_id))
    if not a:
        raise HTTPException(status_code=404, detail=f"Analysis {analysis_id} not found")
    if a["status"] != "complete":
        raise HTTPException(status_code=422, detail=(
            f"Analysis {analysis_id} is {a['status']}; only a complete analysis can be pinned "
            "(a run completes when its review is approved)."))
    repo.pin(a)
    return repo.get(analysis_id)
