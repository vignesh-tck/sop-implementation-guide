"""
FundingAgent — full LangGraph implementation.

Graph flow:
  START → load_block → search_funding → extract_eligibility
        → [INTERRUPT for human review]
        → apply_feedback → write_signals → END
"""

import json
import uuid
from typing import TypedDict, Optional

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command, interrupt

from backend.agents.base import SpecialistAgent
from backend.db.session import get_supabase
from backend.repositories.recommendations import BlockRepository
from backend.repositories.funding import FundingRepository
from backend.tools.epa_brownfields import (
    get_ejscreen_data,
    check_cdbg_eligibility,
    get_brownfields_programs_for_park,
)
from backend.tools.llm_extractor import extract_funding_eligibility


# ── State ────────────────────────────────────────────────────────────────────

class FundingState(TypedDict):
    block_id: int
    thread_id: str
    block_data: dict                # from blocks table
    top_recs: list[str]             # human-readable rec labels
    ejscreen_data: dict
    cdbg_check: dict
    raw_programs: list[dict]        # from get_brownfields_programs_for_park
    assessed_signals: list[dict]    # after LLM eligibility assessment
    human_feedback: Optional[str]   # set after interrupt
    final_signals: list[dict]       # signals to write to DB
    written_signal_ids: list[int]


# ── Nodes ─────────────────────────────────────────────────────────────────────

def load_block(state: FundingState) -> dict:
    """Fetch block data and top recommendations from DB."""
    db = get_supabase()
    repo = BlockRepository(db)
    block = repo.get_with_recs(state["block_id"])
    if not block:
        raise ValueError(f"Block {state['block_id']} not found")

    top_recs = [
        r["rec_label"]
        for r in block.get("recommendations", [])
        if r.get("rec_label")
    ][:5]

    return {"block_data": block, "top_recs": top_recs}


def search_funding(state: FundingState) -> dict:
    """Query EPA EJSCREEN and build program list for this block."""
    block = state["block_data"]
    geom = block.get("geometry_geojson", {})
    coords = geom.get("coordinates", [])

    # Get centroid from LineString endpoints
    ejscreen = {}
    if coords and len(coords) >= 2:
        mid_lng = (coords[0][0] + coords[-1][0]) / 2
        mid_lat = (coords[0][1] + coords[-1][1]) / 2
        ejscreen = get_ejscreen_data(mid_lat, mid_lng)

    cdbg = check_cdbg_eligibility(block.get("median_hh_income"))
    programs = get_brownfields_programs_for_park(block.get("tract_geoid", ""))

    return {
        "ejscreen_data": ejscreen,
        "cdbg_check": cdbg,
        "raw_programs": programs,
    }


def extract_eligibility(state: FundingState) -> dict:
    """Run LLM assessment of each program's eligibility for this block."""
    block = state["block_data"]
    commuters_total = block.get("commuters_total") or 1
    transit_share = round(
        ((block.get("commuters_transit") or 0) / commuters_total) * 100, 1
    )

    context = {
        "tract_geoid": block.get("tract_geoid"),
        "median_hh_income": block.get("median_hh_income"),
        "sop_index_norm": block.get("sop_index_norm"),
        "top_recs": state["top_recs"],
        "transit_share": transit_share,
        "cdbg_check": state["cdbg_check"],
    }

    assessed = extract_funding_eligibility(context, state["raw_programs"])
    return {"assessed_signals": assessed}


def human_review(state: FundingState) -> dict:
    """
    Interrupt for human review.
    The agent pauses here — resume via apply_feedback().
    """
    signals_summary = [
        {
            "program": s.get("program_name"),
            "relevance": s.get("relevance_score"),
            "assessment": s.get("eligibility_assessment"),
            "action": s.get("recommended_action"),
        }
        for s in state["assessed_signals"]
    ]
    # Pause execution — FastAPI endpoint calls graph.invoke(Command(resume=...))
    feedback = interrupt(
        {
            "message": "Review funding signals before writing to database",
            "signals": signals_summary,
            "thread_id": state["thread_id"],
        }
    )
    return {"human_feedback": feedback}


def apply_feedback(state: FundingState) -> dict:
    """
    Apply human feedback to the assessed signals.
    Feedback can be 'approved' or specific notes per program.
    """
    feedback = state.get("human_feedback", "approved")
    signals = state["assessed_signals"]

    if feedback and feedback.strip().lower() != "approved":
        # Re-run extraction with feedback appended to context
        # For now: mark all as reviewed, pass feedback as notes
        for s in signals:
            s["human_notes"] = feedback

    # Filter to signals worth writing (relevance >= 0.3)
    final = [s for s in signals if (s.get("relevance_score") or 0) >= 0.3]
    return {"final_signals": final}


def write_signals(state: FundingState) -> dict:
    """Write approved funding signals to the database."""
    db = get_supabase()
    repo = FundingRepository(db)
    written_ids = []

    for s in state["final_signals"]:
        row = {
            "block_id": state["block_id"],
            "program_name": s.get("program_name"),
            "program_type": s.get("program_type"),
            "source_agency": s.get("source_agency"),
            "award_amount_min": s.get("award_amount_min"),
            "award_amount_max": s.get("award_amount_max"),
            "eligibility_notes": s.get("eligibility_assessment") or s.get("eligibility_notes"),
            "relevance_score": float(s.get("relevance_score", 0.5)),
            "application_url": s.get("application_url"),
            "raw_data": {
                "full_program": s,
                "ejscreen": state.get("ejscreen_data"),
                "cdbg_check": state.get("cdbg_check"),
                "human_feedback": state.get("human_feedback"),
            },
            "thread_id": state["thread_id"],
            "reviewed": True,
        }
        result = repo.upsert_signal(row)
        if result.get("id"):
            written_ids.append(result["id"])

    return {"written_signal_ids": written_ids}


# ── Graph ─────────────────────────────────────────────────────────────────────

class FundingAgent(SpecialistAgent):
    def __init__(self):
        self._checkpointer = MemorySaver()
        self._graph = self.build_graph()

    def build_graph(self):
        builder = StateGraph(FundingState)

        builder.add_node("load_block", load_block)
        builder.add_node("search_funding", search_funding)
        builder.add_node("extract_eligibility", extract_eligibility)
        builder.add_node("human_review", human_review)
        builder.add_node("apply_feedback", apply_feedback)
        builder.add_node("write_signals", write_signals)

        builder.add_edge(START, "load_block")
        builder.add_edge("load_block", "search_funding")
        builder.add_edge("search_funding", "extract_eligibility")
        builder.add_edge("extract_eligibility", "human_review")
        builder.add_edge("human_review", "apply_feedback")
        builder.add_edge("apply_feedback", "write_signals")
        builder.add_edge("write_signals", END)

        return builder.compile(checkpointer=self._checkpointer)

    def run(self, block_id: int, thread_id: str) -> dict:
        """Start analysis. Graph will pause at human_review node."""
        config = {"configurable": {"thread_id": thread_id}}
        initial_state: FundingState = {
            "block_id": block_id,
            "thread_id": thread_id,
            "block_data": {},
            "top_recs": [],
            "ejscreen_data": {},
            "cdbg_check": {},
            "raw_programs": [],
            "assessed_signals": [],
            "human_feedback": None,
            "final_signals": [],
            "written_signal_ids": [],
        }
        result = self._graph.invoke(initial_state, config=config)
        return result

    def get_pending_review(self, thread_id: str) -> dict:
        """Return the current graph state (signals awaiting review)."""
        config = {"configurable": {"thread_id": thread_id}}
        state = self._graph.get_state(config)
        if not state or not state.values:
            return {}
        values = state.values
        return {
            "block_id": values.get("block_id"),
            "thread_id": thread_id,
            "signals": values.get("assessed_signals", []),
            "cdbg_check": values.get("cdbg_check", {}),
        }

    def apply_feedback(self, thread_id: str, feedback: str) -> dict:
        """Resume the graph with human feedback."""
        config = {"configurable": {"thread_id": thread_id}}
        result = self._graph.invoke(Command(resume=feedback), config=config)
        return {
            "written_signal_ids": result.get("written_signal_ids", []),
            "signals_written": len(result.get("written_signal_ids", [])),
            "thread_id": thread_id,
        }
