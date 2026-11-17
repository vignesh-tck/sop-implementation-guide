"""
Zoning — the zones fronting a block, what they allow, and refreshing the data.

Reads: zones are stored by scripts/sync_block_zoning.py or an applied refresh,
and findings are computed on read from the stored zones and the use table in
force. The same findings appear on each recommendation's implementation plan.

Refresh: the Zoning tab can re-extract the use table and re-look-up block zones
from sources the user names. A refresh runs in the background into a run that
waits for review; nothing changes until someone applies it (see
backend/tools/zoning_refresh.py).
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import Client

from backend.agents.zoning import ZoningAgent
from backend.db.session import get_supabase
from backend.repositories.zoning import ZoningRunRepository, ZoningTablesMissing, ZoningVersionRepository
from backend.tools import zoning, zoning_refresh
from backend.tools.zoning_lookup import default_settings

router = APIRouter(tags=["zoning"])

MISSING_TABLES = (
    "Zoning refresh is off until its tables exist: paste the ZONING DATA VERSIONS AND "
    "REFRESH RUNS section of backend/db/schema.sql into the Supabase SQL editor and run it."
)


class UseTableInput(BaseModel):
    mode: Literal["keep", "extract"] = "keep"
    url: Optional[str] = None


class MapInput(BaseModel):
    mode: Literal["keep", "lookup"] = "keep"
    zoning_layer_url: Optional[str] = None
    zone_field: Optional[str] = None
    prior_layer_url: Optional[str] = None
    prior_field: Optional[str] = None
    buffer_m: Optional[float] = None
    blocks: Optional[list[int]] = None


class RefreshRequest(BaseModel):
    use_table: UseTableInput = UseTableInput()
    map: MapInput = MapInput()
    requested_by: str
    note: Optional[str] = None


class Review(BaseModel):
    reviewed_by: Optional[str] = None


@router.get("/zoning/overview")
def get_zoning_overview(db: Client = Depends(get_supabase)):
    """Everything behind the zoning findings: blocks, the use-table matrix, sources."""
    return ZoningAgent().overview(db)


@router.get("/blocks/{block_id}/zoning")
def get_block_zoning(block_id: int, db: Client = Depends(get_supabase)):
    result = ZoningAgent().assess_block(block_id, db)
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail=f"Block {block_id} not found")
    return result


@router.get("/zoning/settings")
def get_zoning_settings(db: Client = Depends(get_supabase)):
    """The data in force and the defaults: what the refresh form starts from."""
    version = zoning.refresh_active(db)
    try:
        history = ZoningVersionRepository(db).history()
        storage_ready = True
    except ZoningTablesMissing:
        history, storage_ready = [], False
    src = zoning.load_use_table()["source"]
    return {
        "storage_ready": storage_ready,
        "active": version,
        "settings": zoning.active_settings(),
        "defaults": default_settings(zoning.load_rules()),
        "use_table": {"url": src.get("url"), "amended_through": src.get("amended_through"),
                      "extracted": src.get("extracted")},
        "history": history,
    }


@router.get("/zoning/refresh")
def list_refreshes(db: Client = Depends(get_supabase)):
    try:
        return {"storage_ready": True, "runs": ZoningRunRepository(db).recent()}
    except ZoningTablesMissing:
        return {"storage_ready": False, "runs": []}


@router.post("/zoning/refresh")
def start_refresh(body: RefreshRequest, db: Client = Depends(get_supabase)):
    try:
        zoning.refresh_active(db)
        return zoning_refresh.start(db, body.model_dump(), body.requested_by, body.note)
    except ZoningTablesMissing:
        raise HTTPException(status_code=503, detail=MISSING_TABLES)
    except zoning_refresh.RefreshError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/zoning/refresh/{run_id}")
def get_refresh(run_id: int, db: Client = Depends(get_supabase)):
    try:
        run = ZoningRunRepository(db).get(run_id)
    except ZoningTablesMissing:
        raise HTTPException(status_code=503, detail=MISSING_TABLES)
    if not run:
        raise HTTPException(status_code=404, detail=f"Refresh #{run_id} not found")
    return run


@router.post("/zoning/refresh/{run_id}/apply")
def apply_refresh(run_id: int, body: Review, db: Client = Depends(get_supabase)):
    try:
        return zoning_refresh.apply(db, run_id, body.reviewed_by or "")
    except ZoningTablesMissing:
        raise HTTPException(status_code=503, detail=MISSING_TABLES)
    except zoning_refresh.RefreshError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/zoning/refresh/{run_id}/discard")
def discard_refresh(run_id: int, body: Review, db: Client = Depends(get_supabase)):
    try:
        zoning_refresh.discard(db, run_id, body.reviewed_by)
        return {"status": "discarded"}
    except ZoningTablesMissing:
        raise HTTPException(status_code=503, detail=MISSING_TABLES)
    except zoning_refresh.RefreshError as e:
        raise HTTPException(status_code=422, detail=str(e))
