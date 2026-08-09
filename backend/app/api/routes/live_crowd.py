"""
GET /api/live-crowd-conditions

Returns one row per sensor from latest_sensor_crowd_levels -- the
latest completed 15-minute wall-clock window, computed live by the
view itself (not something this endpoint calculates).

data_status distinguishes:
  - "fresh": readings exist in the current 15-minute window
  - "stale": sensor has history, but nothing in the current window
  - "no_data": sensor has never reported

Optional filters let the frontend show only fresh/high-crowd sensors
without pulling the full set and filtering client-side.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query

from app.core.db import get_pool
from app.models.live_crowd import (
    DataStatus,
    LiveCrowdConditionsResponse,
    LiveCrowdLevel,
    SensorLiveCrowdCondition,
)

router = APIRouter(prefix="/api/live-crowd-conditions", tags=["live-crowd-conditions"])

_LIVE_CROWD_SQL = """
    SELECT
        sensor_id,
        sensor_name,
        window_start_local,
        window_end_local,
        observed_15m_count,
        reading_count,
        hourly_equivalent_estimate,
        historical_baseline_median,
        historical_baseline_p90,
        crowd_ratio,
        crowd_level,
        data_age,
        data_status,
        latest_sensing_datetime_utc,
        calculated_at
    FROM latest_sensor_crowd_levels
    ORDER BY sensor_name;
"""


@router.get("", response_model=LiveCrowdConditionsResponse)
async def get_live_crowd_conditions(
    level: Optional[LiveCrowdLevel] = Query(
        default=None, description="Optional filter, e.g. ?level=high"
    ),
    status: Optional[DataStatus] = Query(
        default=None, description="Optional filter, e.g. ?status=fresh"
    ),
) -> LiveCrowdConditionsResponse:
    pool = get_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(_LIVE_CROWD_SQL)

    conditions = [
        SensorLiveCrowdCondition(
            sensor_id=r["sensor_id"],
            sensor_name=r["sensor_name"],
            window_start_local=r["window_start_local"],
            window_end_local=r["window_end_local"],
            observed_15m_count=r["observed_15m_count"],
            reading_count=r["reading_count"],
            hourly_equivalent_estimate=r["hourly_equivalent_estimate"],
            historical_baseline_median=r["historical_baseline_median"],
            historical_baseline_p90=r["historical_baseline_p90"],
            crowd_ratio=r["crowd_ratio"],
            crowd_level=r["crowd_level"],
            data_age_seconds=(
                r["data_age"].total_seconds() if r["data_age"] is not None else None
            ),
            data_status=r["data_status"],
            latest_sensing_datetime_utc=r["latest_sensing_datetime_utc"],
            calculated_at=r["calculated_at"],
        )
        for r in rows
    ]

    if level is not None:
        conditions = [c for c in conditions if c.crowd_level == level]
    if status is not None:
        conditions = [c for c in conditions if c.data_status == status]

    return LiveCrowdConditionsResponse(
        generated_at=datetime.now(timezone.utc),
        sensor_count=len(conditions),
        conditions=conditions,
    )
