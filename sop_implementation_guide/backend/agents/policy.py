"""
PolicyAgent — scaffold only. Implements SpecialistAgent interface.
Full implementation post-midterm: fetch MD state plans + Bowie city plans,
run LLM extraction to identify supporting/blocking policy signals per block.
"""

from backend.agents.base import SpecialistAgent


class PolicyAgent(SpecialistAgent):
    def build_graph(self):
        raise NotImplementedError("PolicyAgent graph — post-midterm")

    def run(self, block_id: int, thread_id: str) -> dict:
        return {"status": "not_implemented", "agent": "PolicyAgent", "block_id": block_id}

    def get_pending_review(self, thread_id: str) -> dict:
        return {"status": "not_implemented", "agent": "PolicyAgent"}

    def apply_feedback(self, thread_id: str, feedback: str) -> dict:
        return {"status": "not_implemented", "agent": "PolicyAgent"}
