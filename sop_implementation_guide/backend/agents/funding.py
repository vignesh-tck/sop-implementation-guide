"""
FundingAgent — full LangGraph implementation.

Graph flow:
  START → load_block → gather_signals → assess_relevance
        → [INTERRUPT for human review]
        → apply_feedback → write_signals → END

Two-stage design, which is the point of this agent:

  1. gather_signals makes *determinations* — facts with a citable source. Candidate
     programs come from the funding_programs table (synced from Grants.gov plus a
     curated state/local catalog); eligibility comes from a rule (CDBG income
     threshold) or a published dataset (NMTC tract designation).
  2. assess_relevance asks the LLM to rank and explain those candidates, given the
     determinations as established facts.

The model is never asked whether a tract qualifies for NMTC — that is knowable, and
an earlier version of this pipeline had it guessing from income. Everything here
reads from Supabase, so no third-party API is on the demo path.
"""

from typing import TypedDict, Optional

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command, interrupt

from backend.agents.base import SpecialistAgent
from backend.db.session import get_supabase
from backend.repositories.recommendations import BlockRepository
from backend.repositories.funding import FundingRepository
from backend.repositories.funding_programs import (
    FundingProgramRepository,
    TractEligibilityRepository,
)
from backend.tools.cdbg import check_cdbg_eligibility
from backend.tools.llm_extractor import extract_funding_eligibility


# ── State ────────────────────────────────────────────────────────────────────

class FundingState(TypedDict):
    block_id: int
    thread_id: str
    block_data: dict                 # from blocks table
    top_recs: list[str]              # human-readable rec labels
    determinations: dict             # designation_key -> verdict + provenance
    candidate_programs: list[dict]   # rows from funding_programs
    assessed_signals: list[dict]     # after LLM relevance assessment
    human_feedback: Optional[str]    # set after interrupt
    final_signals: list[dict]        # signals to write to DB
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


def gather_signals(state: FundingState) -> dict:
    """
    Assemble candidate programs and resolve every hard eligibility test.

    Reads the synced catalog and the pre-computed tract designations. Produces a
    `determinations` map of designation_key -> verdict, each carrying the basis and
    source so the claim can be traced back later.
    """
    db = get_supabase()
    block = state["block_data"]

    programs = FundingProgramRepository(db).list_candidates()

    determinations: dict[str, dict] = {}

    # Rule-based: CDBG low/moderate-income area benefit, from the block's real ACS
    # median household income.
    cdbg = check_cdbg_eligibility(block.get("median_hh_income"))
    determinations["CDBG_LMI"] = {
        "eligible": cdbg.get("eligible"),
        "basis": cdbg.get("reason"),
        "source_name": cdbg.get("source"),
        "source_url": "https://www.hud.gov/programs/cdbg_entitlement",
        "source_vintage": f"AMI ${cdbg.get('area_median_income'):,.0f}"
        if cdbg.get("area_median_income") else None,
    }

    # Dataset-based: per-tract designations synced by scripts/sync_tract_eligibility.py.
    tract = block.get("tract_geoid")
    if tract:
        for key, row in TractEligibilityRepository(db).designations_for_tract(tract).items():
            determinations[key] = {
                "eligible": row.get("eligible"),
                "basis": row.get("basis"),
                "source_name": row.get("source_name"),
                "source_url": row.get("source_url"),
                "source_vintage": row.get("source_vintage"),
            }

    return {"candidate_programs": programs, "determinations": determinations}


def assess_relevance(state: FundingState) -> dict:
    """Have the LLM rank the candidates, given the determinations as facts."""
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
    }

    assessed = extract_funding_eligibility(
        context,
        state["candidate_programs"],
        determinations=state["determinations"],
    )
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
            "eligibility_confirmed": s.get("eligibility_confirmed"),
        }
        for s in state["assessed_signals"]
    ]
    # Pause execution — FastAPI endpoint calls graph.invoke(Command(resume=...))
    feedback = interrupt(
        {
            "message": "Review funding signals before writing to database",
            "signals": signals_summary,
            "determinations": state["determinations"],
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

    # Drop programs with a failed hard eligibility test outright — a program the
    # block cannot use is not a funding opportunity, regardless of how relevant the
    # model found the topic. eligibility_confirmed is None when there is no test.
    eligible = [s for s in signals if s.get("eligibility_confirmed") is not False]

    final = [s for s in eligible if (s.get("relevance_score") or 0) >= 0.3]
    return {"final_signals": final}


def write_signals(state: FundingState) -> dict:
    """Write approved funding signals to the database."""
    db = get_supabase()
    repo = FundingRepository(db)
    written_ids = []

    for s in state["final_signals"]:
        deadline = s.get("deadline")
        row = {
            "block_id": state["block_id"],
            "program_name": s.get("program_name"),
            "program_type": s.get("program_type"),
            "source_agency": s.get("source_agency"),
            "award_amount_min": s.get("award_amount_min"),
            "award_amount_max": s.get("award_amount_max"),
            "deadline": deadline,
            "eligibility_notes": s.get("eligibility_assessment") or s.get("eligibility_notes"),
            "relevance_score": float(s.get("relevance_score") or 0.5),
            "application_url": s.get("application_url"),
            # Provenance travels with the signal so "where did this come from?" is
            # answerable from the row alone.
            "source_name": s.get("source_name"),
            "source_url": s.get("source_url"),
            "source_vintage": s.get("source_vintage"),
            "fetched_at": s.get("fetched_at"),
            "eligibility_confirmed": s.get("eligibility_confirmed"),
            "determination_basis": s.get("determination_basis"),
            "raw_data": {
                "program_key": s.get("program_key"),
                "is_extracted": s.get("is_extracted"),
                "extraction_confidence": s.get("extraction_confidence"),
                "recommended_action": s.get("recommended_action"),
                "assessment_failed": s.get("assessment_failed", False),
                "determinations": state.get("determinations"),
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
        builder.add_node("gather_signals", gather_signals)
        builder.add_node("assess_relevance", assess_relevance)
        builder.add_node("human_review", human_review)
        builder.add_node("apply_feedback", apply_feedback)
        builder.add_node("write_signals", write_signals)

        builder.add_edge(START, "load_block")
        builder.add_edge("load_block", "gather_signals")
        builder.add_edge("gather_signals", "assess_relevance")
        builder.add_edge("assess_relevance", "human_review")
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
            "determinations": {},
            "candidate_programs": [],
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
            "determinations": values.get("determinations", {}),
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
