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
    # Provenance — answers "where did this come from?" per signal.
    source_name: Optional[str] = None
    source_url: Optional[str] = None
    source_vintage: Optional[str] = None
    fetched_at: Optional[datetime] = None
    # Set from a deterministic rule or dataset lookup, not from the LLM.
    # None means the program carries no hard eligibility test, or the test could
    # not be evaluated — never conflate that with False.
    eligibility_confirmed: Optional[bool] = None
    determination_basis: Optional[str] = None


class FundingProgram(BaseModel):
    """A row in the synced/curated funding catalog."""
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    program_key: str
    program_name: str
    program_type: Optional[str] = None
    source_agency: Optional[str] = None
    award_amount_min: Optional[float] = None
    award_amount_max: Optional[float] = None
    estimated_funding: Optional[float] = None
    deadline: Optional[date] = None
    eligibility_notes: Optional[str] = None
    application_url: Optional[str] = None
    cfda_numbers: Optional[list[str]] = None
    # When set, the block qualifies only if the matching determination is True.
    requires_tract_designation: Optional[str] = None
    # True when the row came from a live extraction session that a user reviewed.
    is_extracted: bool = False
    extraction_confidence: Optional[float] = None
    source_excerpt: Optional[str] = None
    geo_scope: Optional[str] = None
    reviewed_by_user: bool = False
    discovery_thread_id: Optional[str] = None
    source_name: str
    source_url: Optional[str] = None
    source_vintage: Optional[str] = None
    fetched_at: Optional[datetime] = None
    raw_data: Optional[dict] = None


class TractEligibility(BaseModel):
    """A per-census-tract funding designation."""
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    tract_geoid: str
    program_key: str
    # Tri-state on purpose: True designated, False confirmed not designated,
    # None lookup failed. A failed lookup must not read as ineligible.
    eligible: Optional[bool] = None
    basis: Optional[str] = None
    source_name: Optional[str] = None
    source_url: Optional[str] = None
    source_vintage: Optional[str] = None
    fetched_at: Optional[datetime] = None
    raw_data: Optional[dict] = None
