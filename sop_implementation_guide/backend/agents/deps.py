"""
Shared agent dependencies — single instances across the whole app lifetime.
Import `get_orchestrator` as a FastAPI Depends() to avoid duplicate checkpointers.
"""
from functools import lru_cache
from backend.agents.orchestrator import OrchestratorAgent


@lru_cache(maxsize=1)
def get_orchestrator() -> OrchestratorAgent:
    return OrchestratorAgent()
