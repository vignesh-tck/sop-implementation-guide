"""
ZoningAgent — scaffold only. Implements SpecialistAgent interface.
Full implementation post-midterm: query Prince George's County GIS parcel API,
classify zone type, flag barriers to parks/mixed-use, check variance requirements.
"""

from backend.agents.base import SpecialistAgent


class ZoningAgent(SpecialistAgent):
    def build_graph(self):
        raise NotImplementedError("ZoningAgent graph — post-midterm")

    def run(self, block_id: int, thread_id: str) -> dict:
        return {"status": "not_implemented", "agent": "ZoningAgent", "block_id": block_id}

    def get_pending_review(self, thread_id: str) -> dict:
        return {"status": "not_implemented", "agent": "ZoningAgent"}

    def apply_feedback(self, thread_id: str, feedback: str) -> dict:
        return {"status": "not_implemented", "agent": "ZoningAgent"}
