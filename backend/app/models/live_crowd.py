"""
Response models for live crowd conditions, backed by migration 004's
latest_sensor_crowd_levels view (15-minute rolling window, real-time).

Distinct from crowd_conditions.py, which reads hourly_crowd_features
(historical, "most recently loaded row" rather than truly current).
Kept as a separate endpoint since they answer different questions --
worth a team conversation on whether the historical one still needs
its own route once live data is flowing everywhere.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel


class LiveCrowdLevel(str, Enum):
    """Matches latest_sensor_crowd_levels.crowd_level (note: 'medium', not
    'typical' -- this view uses different wording than hourly_crowd_features)."""
    low = "low"
    medium = "medium"
    high = "high"


class DataStatus(str, Enum):
    """Matches latest_sensor_crowd_levels.data_status."""
    fresh = "fresh"
    stale = "stale"
    no_data = "no_data"


class SensorLiveCrowdCondition(BaseModel):
    sensor_id: int
    sensor_name: str
    window_start_local: datetime
    window_end_local: datetime
    observed_15m_count: Optional[int]
    reading_count: int
    hourly_equivalent_estimate: Optional[int]
    historical_baseline_median: Optional[float]
    historical_baseline_p90: Optional[float]
    crowd_ratio: Optional[float]
    crowd_level: Optional[LiveCrowdLevel]
    data_age_seconds: Optional[float]
    data_status: DataStatus
    latest_sensing_datetime_utc: Optional[datetime]
    calculated_at: datetime


class LiveCrowdConditionsResponse(BaseModel):
    generated_at: datetime
    sensor_count: int
    conditions: List[SensorLiveCrowdCondition]
