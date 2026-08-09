"""
Response models for crowd conditions with live/historical fallback.

Per the team's rule: fresh or delayed live data is used as-is; stale or
no_data falls back to the most recent historical (hourly_crowd_features)
row for that sensor. crowd_ratio/crowd_level below are always the
*resolved* values after that fallback -- `source` says which one was
actually used, and `live_status` shows the underlying live freshness
even when historical data is what's displayed.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel


class LiveCrowdLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class DataStatus(str, Enum):
    """Matches latest_sensor_crowd_levels.data_status (migration 006)."""
    fresh = "fresh"
    delayed = "delayed"
    stale = "stale"
    no_data = "no_data"


class CrowdSource(str, Enum):
    live = "live"
    historical = "historical"
    none = "none"


class SensorCrowdCondition(BaseModel):
    sensor_id: int
    sensor_name: str
    latitude: float
    longitude: float

    # Resolved values, after live/historical fallback is applied.
    source: CrowdSource
    crowd_ratio: Optional[float]
    crowd_level: Optional[LiveCrowdLevel]
    observed_at: Optional[datetime]

    # Underlying live-feed status, kept for transparency even when
    # the resolved values above came from the historical fallback.
    live_status: DataStatus


class CrowdConditionsResponse(BaseModel):
    generated_at: datetime
    sensor_count: int
    conditions: List[SensorCrowdCondition]
