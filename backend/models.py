"""Pydantic models for the target profile API."""

from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field

# Model for the target profile request after the user has filled out the form... This model is used to validate the request data
class TargetProfileRequest(BaseModel):
    sector: str
    deal_size_mm: float = Field(gt=0)
    geography: Optional[str] = None
    target_ownership_pre: Optional[str] = None
    target_revenue_band: Optional[str] = None
    target_ebitda_band: Optional[str] = None
    ebitda_margin_band: Optional[str] = None
    revenue_growth_band: Optional[str] = None
