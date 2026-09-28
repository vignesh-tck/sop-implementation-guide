"""
OrchestratorAgent — fans out to all three specialists, aggregates signals,
computes feasibility score, triggers human approval, writes gold layer.

For midterm: FundingAgent runs fully; ZoningAgent and PolicyAgent return
placeholder scores so the gold layer can still be computed end-to-end.
"""

import uuid
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

    def approve_funding(self, thread_id: str, feedback: str = "approved") -> dict:
        """Resume FundingAgent after human review."""
        return self.funding_agent.apply_feedback(thread_id, feedback)

    def get_funding_review(self, thread_id: str) -> dict:
        """Get funding signals awaiting review."""
        return self.funding_agent.get_pending_review(thread_id)

    def compute_gold(self, block_id: int, thread_id: str) -> dict:
        """
        Compute and write the gold layer for a block after all signals are reviewed.
        Uses placeholder scores for zoning/policy (midterm — FundingAgent only).
        """
        db = get_supabase()
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

    def _score_funding(self, signals: list[dict]) -> float:
        """Score 0–100 based on number and quality of funding signals."""
        if not signals:
            return 0.0
        high_quality = [s for s in signals if (s.get("relevance_score") or 0) >= 0.7]
        medium = [s for s in signals if 0.3 <= (s.get("relevance_score") or 0) < 0.7]
        score = min(len(high_quality) * 25 + len(medium) * 10, 100)
        return float(score)

    def _derive_actions(self, signals: list[dict]) -> list[str]:
        """Turn top funding signals into concrete action steps."""
        actions = []
        for s in sorted(signals, key=lambda x: x.get("relevance_score", 0), reverse=True)[:3]:
            name = s.get("program_name", "")
            url = s.get("application_url", "")
            actions.append(f"Apply for {name}" + (f" — {url}" if url else ""))
        return actions

    def _build_narrative(
        self, block_id: int, signals: list[dict], funding_score: float, feasibility: float
    ) -> str:
        count = len(signals)
        top = signals[0].get("program_name", "") if signals else "none identified"
        return (
            f"Block {block_id} has a feasibility score of {feasibility:.0f}/100. "
            f"{count} funding program(s) identified; top opportunity: {top}. "
            f"Funding sub-score: {funding_score:.0f}/100. "
            f"Zoning and policy analysis pending."
        )
