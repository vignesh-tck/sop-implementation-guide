from __future__ import annotations
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class PolicySignal(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    block_id: int
    policy_name: str
    policy_type: Optional[str] = None   # 'plan' | 'ordinance' | 'program'
    stance: Optional[Literal["supporting", "blocking", "neutral"]] = None
    excerpt: Optional[str] = None
    source_url: Optional[str] = None
    relevance_score: float = Field(ge=0.0, le=1.0)
    raw_data: Optional[dict] = None
    thread_id: Optional[str] = None
    reviewed: bool = False
    created_at: Optional[datetime] = None


class PolicySignalCreate(BaseModel):
    block_id: int
    policy_name: str
    policy_type: Optional[str] = None
    stance: Optional[Literal["supporting", "blocking", "neutral"]] = None
    excerpt: Optional[str] = None
    source_url: Optional[str] = None
    relevance_score: float = Field(ge=0.0, le=1.0)
    raw_data: Optional[dict] = None
    thread_id: Optional[str] = None
