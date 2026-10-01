"""
FundingAgent — full LangGraph implementation.

Graph flow:
  START → load_block → load_programs → assess_fit
        → [INTERRUPT for human review]
        → apply_feedback → write_signals → END

The unit of analysis is a single block recommendation, not the block. For each
recommendation the agent asks the LLM how well every catalogued programme could
fund *that* improvement, and returns a score plus a narrative saying why. The
reviewer then adjusts scores and approves recommendation by recommendation.

Scoring a programme against a whole block produced narratives too vague to act on
("supports community development"); scoring it against one recommendation gives
the reviewer something concrete to agree or disagree with. The score is a proposal,
not a verdict — nothing is written that the reviewer did not keep, and nothing an
earlier review kept survives a later one that takes it back.

Everything here reads from Supabase, so no third-party API is on the demo path.
"""

from typing import TypedDict, Optional, Any

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command, interrupt

from backend.agents.base import SpecialistAgent
from backend.config import settings
from backend.db.session import get_supabase
from backend.repositories.recommendations import BlockRepository
from backend.repositories.funding import FundingRepository
from backend.repositories.funding_programs import FundingProgramRepository
from backend.tools.llm_extractor import assess_recommendation_fit


# ── State ────────────────────────────────────────────────────────────────────

class FundingState(TypedDict):
    block_id: int
    thread_id: str
    block_data: dict                 # from blocks table
    recommendations: list[dict]       # block_recommendations rows, rank order
    candidate_programs: list[dict]    # rows from funding_programs
    fits: list[dict]                  # one row per (recommendation, programme)
    human_feedback: Optional[Any]     # set after interrupt
    final_signals: list[dict]         # signals to write to DB
    rejected_signals: list[dict]      # (rec_id, program_name) pairs to remove
    written_signal_ids: list[int]
    removed_signal_ids: list[int]


# How many recommendations to assess. Each one is its own LLM call, so this is the
# ceiling on both latency and the reviewer's reading load.
MAX_RECS = 5


# ── Nodes ─────────────────────────────────────────────────────────────────────

def _pick_recs(recommendations: list[dict], limit: int = MAX_RECS) -> list[dict]:
    """
    The highest-impact recommendations, one per distinct improvement.

    Two things make naive slicing wrong here. rec_rank is unique per *dimension*,
    so a block with 26 recommendations has 26 rows all ranked 1 and "the first
    five" is arbitrary — ordering by predicted gain picks the five that matter.
    And the same improvement recurs under several dimensions (one block lists
    "Playing or Sport Field" under both Recreational Facilities and Parks &
    Public Spaces), which would spend two LLM calls to reach the same funding
    answer and show the reviewer the same decision twice.

    Deduped on (label, direction), not label alone: adding a feature and removing
    it are opposite asks that happen to share a name.
    """
    ranked = sorted(
        (r for r in recommendations if r.get("rec_label")),
        key=lambda r: r.get("predicted_score_increase") or 0,
        reverse=True,
    )
    seen, picked = set(), []
    for r in ranked:
        key = (r.get("rec_label"), r.get("direction"))
        if key in seen:
            continue
        seen.add(key)
        picked.append(r)
        if len(picked) == limit:
            break
    return picked


def load_block(state: FundingState) -> dict:
    """Fetch block data and the recommendations worth finding money for."""
    db = get_supabase()
    block = BlockRepository(db).get_with_recs(state["block_id"])
    if not block:
        raise ValueError(f"Block {state['block_id']} not found")

    # Full rows, not just labels: the assessment prompt uses dimension, direction
    # and predicted gain, and the written signal needs the recommendation's id.
    return {"block_data": block, "recommendations": _pick_recs(block.get("recommendations", []))}


def load_programs(state: FundingState) -> dict:
    """Read the funding catalogue filled by discovery sessions."""
    programs = FundingProgramRepository(get_supabase()).list_candidates()
    return {"candidate_programs": programs}


def assess_fit(state: FundingState) -> dict:
    """One LLM call per recommendation, scoring every programme against it."""
    block = state["block_data"]
    recs = state["recommendations"]
    programs = state["candidate_programs"]

    if not recs or not programs:
        return {"fits": []}

    commuters_total = block.get("commuters_total") or 1
    context = {
        "tract_geoid": block.get("tract_geoid"),
        "median_hh_income": block.get("median_hh_income"),
        "sop_index_norm": block.get("sop_index_norm"),
        "transit_share": round(
            ((block.get("commuters_transit") or 0) / commuters_total) * 100, 1
        ),
    }

    fits: list[dict] = []
    for rec in recs:
        fits.extend(assess_recommendation_fit(context, rec, programs))
    return {"fits": fits}


def _grouped(fits: list[dict], recs: list[dict]) -> list[dict]:
    """Fits grouped by recommendation — the shape the reviewer works in."""
    groups = []
    for rec in recs:
        rows = [f for f in fits if f.get("rec_id") == rec.get("id")]
        groups.append({
            "rec_id": rec.get("id"),
            "rec_label": rec.get("rec_label"),
            "dimension": rec.get("dimension"),
            "direction": rec.get("direction"),
            "predicted_score_increase": rec.get("predicted_score_increase"),
            "fits": [
                {
                    "program_name": f.get("program_name"),
                    "program_type": f.get("program_type"),
                    "source_agency": f.get("source_agency"),
                    "fit_score": f.get("fit_score"),
                    "narrative": f.get("narrative"),
                    "recommended_action": f.get("recommended_action"),
                    "deadline": f.get("deadline"),
                    "award_amount_min": f.get("award_amount_min"),
                    "award_amount_max": f.get("award_amount_max"),
                    "application_url": f.get("application_url"),
                    "source_name": f.get("source_name"),
                    "source_url": f.get("source_url"),
                    "is_extracted": f.get("is_extracted"),
                    "extraction_confidence": f.get("extraction_confidence"),
                    "assessment_failed": f.get("assessment_failed", False),
                }
                for f in rows
            ],
        })
    return groups


def human_review(state: FundingState) -> dict:
    """
    Interrupt for human review, one group per recommendation.

    The agent pauses here — resume via apply_feedback().
    """
    feedback = interrupt(
        {
            "message": "Review each recommendation's funding fits. Adjust any score, "
                       "then approve the ones worth keeping.",
            "block_id": state["block_id"],
            "recommendations": _grouped(state["fits"], state["recommendations"]),
            "default_floor": settings.relevance_floor,
            "expects": "{'fits': [{'rec_id': 1, 'program_name': '…', "
                       "'fit_score': 0.8, 'approved': true}, …]} — or 'approved' to "
                       f"keep every fit scoring {settings.relevance_floor} or above.",
            "thread_id": state["thread_id"],
        }
    )
    return {"human_feedback": feedback}


def _decisions(feedback: Any) -> Optional[dict[tuple, dict]]:
    """
    Index explicit reviewer decisions by (rec_id, program_name).

    Returns None when the reviewer gave no per-fit decisions, which means "accept
    the proposal as scored". An empty dict is a different answer: it means every
    fit was rejected, and must not fall back to accepting them.
    """
    if not isinstance(feedback, dict):
        return None
    rows = feedback.get("fits")
    if not isinstance(rows, list):
        return None
    return {
        (r.get("rec_id"), r.get("program_name")): r
        for r in rows
        if isinstance(r, dict)
    }


def _note(feedback: Any) -> Optional[str]:
    """The reviewer's free-text note, from either resume shape.

    'approved' is a control word, not a note, so it is not recorded as one.
    """
    if isinstance(feedback, dict):
        text = feedback.get("feedback") or feedback.get("note")
    else:
        text = feedback
    if not isinstance(text, str) or text.strip().lower() in ("", "approved"):
        return None
    return text.strip()


def apply_feedback(state: FundingState) -> dict:
    """
    Keep what the reviewer approved, at the score the reviewer settled on.

    Two paths. With explicit per-fit decisions, only approved fits survive and a
    supplied fit_score overrides the model's — the reviewer's number is the one
    that gets written. Without them ('approved', or free-text notes), fits at or
    above relevance_floor are kept, because writing all ~50 programmes × every
    recommendation would bury the signal in rows nobody chose.

    Everything assessed and not kept is returned as a rejection, not just dropped.
    A review is a statement about every pair it saw, so an earlier run's row for a
    pair rejected here has to go — otherwise unticking a fit on a re-review leaves
    the old row standing and the decision appears not to have taken.
    """
    feedback = state.get("human_feedback")
    decisions = _decisions(feedback)
    notes = _note(feedback)

    final, rejected = [], []

    def reject(row: dict) -> None:
        rejected.append({
            "rec_id": row.get("rec_id"),
            "program_name": row.get("program_name"),
        })

    for f in state["fits"]:
        row = dict(f)
        if notes:
            row["human_notes"] = notes

        if decisions is None:
            if (row.get("fit_score") or 0) >= settings.relevance_floor:
                final.append(row)
            else:
                reject(row)
            continue

        d = decisions.get((row.get("rec_id"), row.get("program_name")))
        if not d or not d.get("approved"):
            reject(row)
            continue
        if d.get("fit_score") is not None:
            proposed = row.get("fit_score")
            settled = max(0.0, min(1.0, float(d["fit_score"])))
            row["fit_score"] = settled
            # Only a real change counts as an adjustment. The console echoes every
            # score back on approve, including untouched ones, so flagging on
            # presence alone marked the whole run as reviewer-adjusted.
            if proposed is None or abs(settled - float(proposed)) > 1e-9:
                row["score_adjusted_by_reviewer"] = True
        if d.get("narrative"):
            row["narrative"] = d["narrative"]
        final.append(row)

    return {"final_signals": final, "rejected_signals": rejected}


def write_signals(state: FundingState) -> dict:
    """
    Write approved fits, one row per (recommendation, programme), and remove the
    rejected ones so a re-review's unticks actually take.

    Deletes run first: the two sets are disjoint by construction, but if a pair
    ever appeared in both, keeping it is the safer outcome than dropping it.
    """
    db = get_supabase()
    repo = FundingRepository(db)
    written_ids = []
    removed_ids = []

    by_rec: dict[Any, list[str]] = {}
    for s in state.get("rejected_signals") or []:
        if s.get("program_name"):
            by_rec.setdefault(s.get("rec_id"), []).append(s["program_name"])
    for rec_id, names in by_rec.items():
        removed_ids.extend(repo.delete_for_rec(state["block_id"], rec_id, names))

    for s in state["final_signals"]:
        row = {
            "block_id": state["block_id"],
            "rec_id": s.get("rec_id"),
            "rec_label": s.get("rec_label"),
            "program_name": s.get("program_name"),
            "program_type": s.get("program_type"),
            "source_agency": s.get("source_agency"),
            "award_amount_min": s.get("award_amount_min"),
            "award_amount_max": s.get("award_amount_max"),
            "deadline": s.get("deadline"),
            # The narrative is the reason this programme suits this recommendation,
            # which is what a planner needs; the programme's generic eligibility
            # text is still on the funding_programs row if anyone wants it.
            "eligibility_notes": s.get("narrative") or s.get("eligibility_notes"),
            "relevance_score": float(s.get("fit_score") or 0.5),
            "application_url": s.get("application_url"),
            # Provenance travels with the signal so "where did this come from?" is
            # answerable from the row alone.
            "source_name": s.get("source_name"),
            "source_url": s.get("source_url"),
            "source_vintage": s.get("source_vintage"),
            "fetched_at": s.get("fetched_at"),
            "raw_data": {
                "program_key": s.get("program_key"),
                "is_extracted": s.get("is_extracted"),
                "extraction_confidence": s.get("extraction_confidence"),
                "recommended_action": s.get("recommended_action"),
                "assessment_failed": s.get("assessment_failed", False),
                "score_adjusted_by_reviewer": s.get("score_adjusted_by_reviewer", False),
                "rec_dimension": s.get("rec_dimension"),
                "rec_direction": s.get("rec_direction"),
                "human_notes": s.get("human_notes"),
            },
            "thread_id": state["thread_id"],
            "reviewed": True,
        }
        result = repo.upsert_signal(row)
        if result.get("id"):
            written_ids.append(result["id"])

    return {"written_signal_ids": written_ids, "removed_signal_ids": removed_ids}


# ── Graph ─────────────────────────────────────────────────────────────────────

class FundingAgent(SpecialistAgent):
    def __init__(self):
        self._checkpointer = MemorySaver()
        self._graph = self.build_graph()

    def build_graph(self):
        builder = StateGraph(FundingState)

        builder.add_node("load_block", load_block)
        builder.add_node("load_programs", load_programs)
        builder.add_node("assess_fit", assess_fit)
        builder.add_node("human_review", human_review)
        builder.add_node("apply_feedback", apply_feedback)
        builder.add_node("write_signals", write_signals)

        builder.add_edge(START, "load_block")
        builder.add_edge("load_block", "load_programs")
        builder.add_edge("load_programs", "assess_fit")
        builder.add_edge("assess_fit", "human_review")
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
            "recommendations": [],
            "candidate_programs": [],
            "fits": [],
            "human_feedback": None,
            "final_signals": [],
            "rejected_signals": [],
            "written_signal_ids": [],
            "removed_signal_ids": [],
        }
        return self._graph.invoke(initial_state, config=config)

    def get_pending_review(self, thread_id: str) -> dict:
        """Return the current graph state (fits awaiting review)."""
        config = {"configurable": {"thread_id": thread_id}}
        state = self._graph.get_state(config)
        if not state or not state.values:
            return {}
        values = state.values
        return {
            "block_id": values.get("block_id"),
            "thread_id": thread_id,
            "recommendations": _grouped(
                values.get("fits", []), values.get("recommendations", [])
            ),
            "default_floor": settings.relevance_floor,
        }

    def apply_feedback(self, thread_id: str, feedback: Any = "approved") -> dict:
        """Resume the graph with reviewer decisions."""
        config = {"configurable": {"thread_id": thread_id}}
        result = self._graph.invoke(Command(resume=feedback), config=config)
        written = result.get("written_signal_ids", [])
        removed = result.get("removed_signal_ids", [])
        return {
            "written_signal_ids": written,
            "signals_written": len(written),
            "removed_signal_ids": removed,
            "signals_removed": len(removed),
            "thread_id": thread_id,
        }
