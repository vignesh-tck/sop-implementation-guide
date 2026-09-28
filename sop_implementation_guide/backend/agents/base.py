from abc import ABC, abstractmethod
from typing import Any


class SpecialistAgent(ABC):
    """
    Contract all specialist agents must satisfy.

    Each agent wraps a LangGraph StateGraph with three externally-visible steps:
      1. run()              — start analysis for a block, pause before DB write
      2. get_pending_review() — return extracted signals awaiting human approval
      3. apply_feedback()   — resume graph with human feedback, write to DB
    """

    @abstractmethod
    def build_graph(self) -> Any:
        """Build and return the compiled LangGraph for this agent."""
        ...

    @abstractmethod
    def run(self, block_id: int, thread_id: str) -> dict:
        """
        Start (or resume) the agent for a given block.
        Returns the current graph state.
        The graph will interrupt before writing — call get_pending_review() next.
        """
        ...

    @abstractmethod
    def get_pending_review(self, thread_id: str) -> dict:
        """Return signals awaiting human review for this thread."""
        ...

    @abstractmethod
    def apply_feedback(self, thread_id: str, feedback: str) -> dict:
        """
        Resume the graph after human review.
        feedback: free-text notes or 'approved' to accept as-is.
        """
        ...
