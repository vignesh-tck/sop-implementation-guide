"""
Zoning — the zones fronting a block and what they allow.

Read-only: zones are written by scripts/sync_block_zoning.py, and findings are
computed on read from the stored zones and the use tables. The same findings
appear on each recommendation's implementation plan.
"""

from fastapi import APIRouter, Depends, HTTPException
from supabase import Client

from backend.agents.zoning import ZoningAgent
from backend.db.session import get_supabase

router = APIRouter(prefix="/blocks", tags=["zoning"])


@router.get("/{block_id}/zoning")
def get_block_zoning(block_id: int, db: Client = Depends(get_supabase)):
    result = ZoningAgent().assess_block(block_id, db)
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail=f"Block {block_id} not found")
    return result
