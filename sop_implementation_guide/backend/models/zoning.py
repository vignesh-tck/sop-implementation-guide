from __future__ import annotations
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict


class ZoningSignal(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    block_id: int
    parcel_id: Optional[str] = None
    current_zone: Optional[str] = None
    zone_description: Optional[str] = None
    allows_parks: Optional[bool] = None
    allows_mixed_use: Optional[bool] = None
    variance_required: Optional[bool] = None
    barrier_notes: Optional[str] = None
    raw_data: Optional[dict] = None
    thread_id: Optional[str] = None
    reviewed: bool = False
    created_at: Optional[datetime] = None


class ZoningSignalCreate(BaseModel):
    block_id: int
    parcel_id: Optional[str] = None
    current_zone: Optional[str] = None
    zone_description: Optional[str] = None
    allows_parks: Optional[bool] = None
    allows_mixed_use: Optional[bool] = None
    variance_required: Optional[bool] = None
    barrier_notes: Optional[str] = None
    raw_data: Optional[dict] = None
    thread_id: Optional[str] = None
