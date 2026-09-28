from __future__ import annotations
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class BlockImplementationProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    block_id: int
    feasibility_score: float = Field(ge=0.0, le=100.0)
    funding_score: float = Field(ge=0.0, le=100.0)
    zoning_score: float = Field(ge=0.0, le=100.0)
    policy_score: float = Field(ge=0.0, le=100.0)
    top_actions: list[str] = []
    narrative: Optional[str] = None
    funding_signal_ids: list[int] = []
    zoning_signal_ids: list[int] = []
    policy_signal_ids: list[int] = []
    thread_id: Optional[str] = None
    computed_at: Optional[datetime] = None


class BlockImplementationProfileCreate(BaseModel):
    block_id: int
    feasibility_score: float = Field(ge=0.0, le=100.0)
    funding_score: float = Field(ge=0.0, le=100.0)
    zoning_score: float = Field(ge=0.0, le=100.0)
    policy_score: float = Field(ge=0.0, le=100.0)
    top_actions: list[str] = []
    narrative: Optional[str] = None
    funding_signal_ids: list[int] = []
    zoning_signal_ids: list[int] = []
    policy_signal_ids: list[int] = []
    thread_id: Optional[str] = None
