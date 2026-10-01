"""
OrchestratorAgent — fans out to all three specialists, aggregates signals,
computes feasibility score, triggers human approval, writes gold layer.

For midterm: FundingAgent runs fully; ZoningAgent and PolicyAgent return
placeholder scores so the gold layer can still be computed end-to-end.
"""

import uuid
from typing import Any

from backend.config import settings
from backend.agents.funding import FundingAgent
from backend.agents.zoning import ZoningAgent
from backend.agents.policy import PolicyAgent
from backend.db.session import get_supabase
from backend.repositories.funding import FundingRepository
from backend.repositories.gold import GoldRepository


class OrchestratorAgent:
    def __init__(self):
        self.funding_agent = FundingAgent()
        self.zoning_agent = ZoningAgent()
        self.policy_agent = PolicyAgent()

    def run_funding(self, block_id: int) -> dict:
        """Start FundingAgent for a block. Returns thread_id for tracking."""
        thread_id = f"funding-{block_id}-{uuid.uuid4().hex[:8]}"
        result = self.funding_agent.run(block_id, thread_id)
        return {"thread_id": thread_id, "state": result}

    def approve_funding(self, thread_id: str, feedback: Any = "approved") -> dict:
        """Resume FundingAgent after human review."""
        return self.funding_agent.apply_feedback(thread_id, feedback)

    def get_funding_review(self, thread_id: str) -> dict:
        """Get per-recommendation funding fits awaiting review."""
        return self.funding_agent.get_pending_review(thread_id)

    def resolve_block_id(self, thread_id: str, db=None) -> int | None:
        """
        Recover the block this thread belongs to.

        The thread's graph state is authoritative while it exists. We never take
        block_id from the caller: a mismatched value would write a profile for the
        wrong block without raising anything.

        The checkpointer is in-memory, so a server restart (uvicorn --reload fires on
        any edit) wipes the state of threads whose signals are already committed.
        Those rows carry their own thread_id, so fall back to the DB rather than
        making the user re-run an analysis that already succeeded.
        """
        state = self.funding_agent.get_pending_review(thread_id)
        block_id = state.get("block_id") if state else None
        if block_id is not None:
            return block_id

        return FundingRepository(db or get_supabase()).get_block_id_for_thread(thread_id)

    def compute_gold(self, thread_id: str) -> dict:
        """
        Compute and write the gold layer for a block after all signals are reviewed.
        Uses placeholder scores for zoning/policy (midterm — FundingAgent only).
        """
        db = get_supabase()
        block_id = self.resolve_block_id(thread_id, db)
        if block_id is None:
            raise ValueError(
                f"Thread {thread_id} has no live graph state and wrote no funding "
                "signals. Either it was never approved, or the server restarted "
                "before approval — re-run POST /blocks/{block_id}/analyze."
            )

        funding_repo = FundingRepository(db)
        gold_repo = GoldRepository(db)

        # Gather reviewed funding signals
        funding_signals = funding_repo.get_for_block(block_id, reviewed_only=True)
        funding_score = self._score_funding(funding_signals)

        # Placeholders until ZoningAgent + PolicyAgent are implemented
        zoning_score = 50.0
        policy_score = 50.0

        feasibility = gold_repo.compute_feasibility_score(
            funding_score, zoning_score, policy_score
        )

        top_actions = self._derive_actions(funding_signals)
        narrative = self._build_narrative(block_id, funding_signals, funding_score, feasibility)

        profile = {
            "block_id": block_id,
            "feasibility_score": feasibility,
            "funding_score": funding_score,
            "zoning_score": zoning_score,
            "policy_score": policy_score,
            "top_actions": top_actions,
            "narrative": narrative,
            "funding_signal_ids": [s["id"] for s in funding_signals if s.get("id")],
            "zoning_signal_ids": [],
            "policy_signal_ids": [],
            "thread_id": thread_id,
        }

        return gold_repo.upsert_profile(profile)

    @staticmethod
    def _best_per_program(signals: list[dict]) -> list[dict]:
        """
        Collapse to one row per programme, keeping its strongest fit.

        Signals are per (recommendation, programme), so one programme approved
        against four recommendations is four rows. Counting rows would let a single
        versatile programme cap the funding score on its own, so the block-level
        view dedupes first. The per-recommendation detail stays in the table.
        """
        best: dict[str, dict] = {}
        for s in signals:
            name = s.get("program_name") or ""
            current = best.get(name)
            if current is None or (s.get("relevance_score") or 0) > (
                current.get("relevance_score") or 0
            ):
                best[name] = s
        return list(best.values())

    def _score_funding(self, signals: list[dict]) -> float:
        """
        Score 0–100 from the funding signals on a block.

        Scored on reviewer-approved fit alone: every row here is one a human kept,
        so the score reflects how strong those matches are, not how many rows the
        model emitted. Point values live in config.py.
        """
        if not signals:
            return 0.0

        strong = marginal = 0
        for s in self._best_per_program(signals):
            fit = s.get("relevance_score") or 0.0
            if fit >= settings.relevance_high:
                strong += 1
            elif fit >= settings.relevance_floor:
                marginal += 1

        cap = settings.max_counted_per_tier
        total = (
            settings.points_strong_fit * min(strong, cap)
            + settings.points_marginal * min(marginal, cap)
        )
        return float(min(total, 100.0))

    def _derive_actions(self, signals: list[dict]) -> list[str]:
        """
        Turn the strongest fits into concrete action steps.

        Each action names the recommendation the money would pay for — "apply for
        X" is not actionable until you know which improvement it funds. A real
        deadline is included where we have one.
        """
        ranked = sorted(signals, key=lambda s: s.get("relevance_score") or 0, reverse=True)
        actions = []
        for s in ranked[:3]:
            parts = [f"Apply for {s.get('program_name', '')}"]
            if s.get("rec_label"):
                parts.append(f"to fund {s['rec_label']}")
            if s.get("deadline"):
                parts.append(f"— deadline {s['deadline']}")
            if s.get("application_url"):
                parts.append(f"— {s['application_url']}")
            actions.append(" ".join(parts))
        return actions

    def _build_narrative(
        self, block_id: int, signals: list[dict], funding_score: float, feasibility: float
    ) -> str:
        programs = self._best_per_program(signals)
        covered = {s.get("rec_label") for s in signals if s.get("rec_label")}
        dated = [s for s in signals if s.get("deadline")]
        # Rank before naming a "top" opportunity — the DB returns rows in arbitrary
        # order, which previously surfaced a stale signal as the headline.
        ranked = sorted(signals, key=lambda s: s.get("relevance_score") or 0, reverse=True)
        top = ranked[0] if ranked else None

        parts = [f"Block {block_id} has a feasibility score of {feasibility:.0f}/100."]
        if top:
            headline = f"{len(programs)} funding program(s) matched to "
            headline += f"{len(covered)} recommendation(s); strongest fit: "
            headline += f"{top.get('program_name', '')}"
            headline += f" for {top['rec_label']}." if top.get("rec_label") else "."
            parts.append(headline)
        else:
            parts.append("No funding programs were approved for this block.")
        if covered:
            parts.append("Recommendations with funding identified: " + ", ".join(sorted(covered)) + ".")
        if dated:
            soonest = min(s["deadline"] for s in dated)
            parts.append(f"Nearest application deadline is {soonest}.")
        parts.append(f"Funding sub-score: {funding_score:.0f}/100.")
        parts.append("Zoning and policy analysis pending.")
        return " ".join(parts)
