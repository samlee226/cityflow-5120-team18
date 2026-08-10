"""
Response models for the Current Crowd Conditions feature.

Fields mirror hourly_crowd_features directly where possible, so the API
stays a thin, trustworthy read layer over the view rather than
reinventing its own crowd math.
"""

from datetime import date, datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel


class CrowdLevel(str, Enum):
    """Matches hourly_crowd_features.crowd_level exactly (view-computed)."""
    low = "low"
    typical = "typical"
    high = "high"


class SensorCrowdCondition(BaseModel):
    sensor_id: int
    sensor_name: str
    latitude: float
    longitude: float
    sensing_date: date
    hour: int
    local_observation_datetime: datetime  # Melbourne local time, not UTC
    pedestrian_count: int
    baseline_median: Optional[float]
    difference_from_median: Optional[float]
    percentage_difference_from_median: Optional[float]
    crowd_ratio: Optional[float]
    z_score: Optional[float]
    crowd_level: Optional[CrowdLevel]
    baseline_missing: bool


class CrowdConditionsResponse(BaseModel):
    generated_at: datetime
    sensor_count: int
    conditions: List[SensorCrowdCondition]
