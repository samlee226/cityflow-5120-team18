from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel


class TrendCrowdLevel(str, Enum):
    """Matches hourly_crowd_features.crowd_level exactly (source vocabulary)."""
    low = "low"
    typical = "typical"
    high = "high"


class TrendPoint(BaseModel):
    local_observation_datetime: datetime  # Melbourne local time, not UTC
    pedestrian_count: int
    baseline_median: Optional[float]
    crowd_ratio: Optional[float]
    crowd_level: Optional[TrendCrowdLevel]


class SensorTrendResponse(BaseModel):
    sensor_id: int
    sensor_name: str
    start: Optional[datetime]
    end: Optional[datetime]
    point_count: int
    points: List[TrendPoint]
