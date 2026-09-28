from __future__ import annotations
from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class FundingSignal(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    block_id: int
    program_name: str
    program_type: Optional[str] = None      # 'federal' | 'state' | 'local'
    source_agency: Optional[str] = None
    award_amount_min: Optional[float] = None
    award_amount_max: Optional[float] = None
    deadline: Optional[date] = None
    eligibility_notes: Optional[str] = None
    relevance_score: float = Field(ge=0.0, le=1.0)
    application_url: Optional[str] = None
    raw_data: Optional[dict] = None
    thread_id: Optional[str] = None
    reviewed: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class FundingSignalCreate(BaseModel):
    """Input model — omits id and timestamps."""
    block_id: int
    program_name: str
    program_type: Optional[str] = None
    source_agency: Optional[str] = None
    award_amount_min: Optional[float] = None
    award_amount_max: Optional[float] = None
    deadline: Optional[date] = None
    eligibility_notes: Optional[str] = None
    relevance_score: float = Field(ge=0.0, le=1.0)
    application_url: Optional[str] = None
    raw_data: Optional[dict] = None
    thread_id: Optional[str] = None
