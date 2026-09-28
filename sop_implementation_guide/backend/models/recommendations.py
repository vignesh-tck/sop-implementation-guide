from __future__ import annotations
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict


class Block(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int                         # sgmntid
    street_name: str
    intersection_a: Optional[str] = None
    intersection_b: Optional[str] = None
    neighborhood: str = "Bowie Town Center"

    # SoP scores
    sop_index: Optional[float] = None
    sop_index_norm: Optional[float] = None
    form_score: Optional[float] = None
    density_score: Optional[float] = None
    proximity_score: Optional[float] = None
    connectivity_score: Optional[float] = None
    parks_score: Optional[float] = None
    pedestrian_score: Optional[float] = None
    safety_score: Optional[float] = None
    traffic_score: Optional[float] = None
    aesthetics_score: Optional[float] = None

    # Census / ACS
    tract_geoid: Optional[str] = None
    tract_name: Optional[str] = None
    total_pop: Optional[int] = None
    median_hh_income: Optional[float] = None
    median_home_value: Optional[float] = None
    commuters_total: Optional[int] = None
    commuters_transit: Optional[int] = None
    renter_no_vehicle: Optional[int] = None

    # Feature snapshots (JSONB)
    features_current: Optional[dict] = None
    features_recommended: Optional[dict] = None
    geometry_geojson: Optional[dict] = None

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class BlockRecommendation(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    block_id: int
    rec_rank: int
    rec_label: str              # "Public Garden"
    dimension: Optional[str] = None    # "Parks & Public Spaces"
    predicted_score_increase: Optional[float] = None
    direction: Optional[str] = None    # "Increase" | "Decrease"
    created_at: Optional[datetime] = None


class BlockWithRecs(Block):
    recommendations: list[BlockRecommendation] = []
