from fastapi import APIRouter, Depends, HTTPException
from supabase import Client

from backend.db.session import get_supabase
from backend.repositories.recommendations import BlockRepository
from backend.repositories.gold import GoldRepository
from backend.agents.deps import get_orchestrator
from backend.agents.orchestrator import OrchestratorAgent

router = APIRouter(prefix="/blocks", tags=["blocks"])


@router.get("/")
def list_blocks(db: Client = Depends(get_supabase)):
    return BlockRepository(db).list_summary()


@router.get("/{block_id}")
def get_block(block_id: int, db: Client = Depends(get_supabase)):
    block = BlockRepository(db).get_with_recs(block_id)
    if not block:
        raise HTTPException(status_code=404, detail=f"Block {block_id} not found")
    return block


@router.get("/{block_id}/profile")
def get_profile(block_id: int, db: Client = Depends(get_supabase)):
    profile = GoldRepository(db).get_for_block(block_id)
    if not profile:
        raise HTTPException(status_code=404, detail="No profile yet — run /analyze first")
    return profile


@router.post("/{block_id}/analyze")
def start_analysis(
    block_id: int,
    db: Client = Depends(get_supabase),
    orchestrator: OrchestratorAgent = Depends(get_orchestrator),
):
    if not BlockRepository(db).get_by_id(block_id):
        raise HTTPException(status_code=404, detail=f"Block {block_id} not found")

    result = orchestrator.run_funding(block_id)
    return {
        "block_id": block_id,
        "thread_id": result["thread_id"],
        "status": "awaiting human review",
        "next_step": f"GET /threads/{result['thread_id']}/review",
    }
