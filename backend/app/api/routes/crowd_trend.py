"""
GET /api/crowd-conditions/{sensor_id}/trend

Returns an hourly time series for one sensor from hourly_crowd_features,
covering the `days`-day window ending at that sensor's own most recent
available reading -- not at real wall-clock "now".

This matters because hourly_crowd_features is only as current as the
last historical CSV someone loaded (a manual, occasional refresh --
see the pipeline README). If that data stops in March and this
endpoint anchored to real "now" in August, "last 7 days" would always
return nothing, for reasons that have nothing to do with the sensor
or the query -- same failure mode migration 006 fixed for the live
view, applied here to the historical one.

Uses local_observation_datetime (Melbourne local time, not UTC) for
both filtering and the returned points, matching how the underlying
view stores it.
"""

from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.core.db import get_pool
from app.models.crowd_trend import SensorTrendResponse, TrendPoint

router = APIRouter(prefix="/api/crowd-conditions", tags=["crowd-conditions"])

_SENSOR_LOOKUP_SQL = "SELECT sensor_name FROM sensors WHERE sensor_id = $1;"

_LATEST_READING_SQL = """
    SELECT max(local_observation_datetime) AS latest
    FROM hourly_crowd_features
    WHERE sensor_id = $1;
"""

_TREND_SQL = """
    SELECT
        local_observation_datetime,
        pedestrian_count,
        baseline_median,
        crowd_ratio,
        crowd_level
    FROM hourly_crowd_features
    WHERE sensor_id = $1
      AND local_observation_datetime >= $2
      AND local_observation_datetime < $3
    ORDER BY local_observation_datetime;
"""


@router.get("/{sensor_id}/trend", response_model=SensorTrendResponse)
async def get_sensor_trend(
    sensor_id: int,
    days: int = Query(
        default=7, ge=1, le=90, description="How many days of data to include, ending at this sensor's latest available reading."
    ),
) -> SensorTrendResponse:
    pool = get_pool()

    async with pool.acquire() as conn:
        sensor_row = await conn.fetchrow(_SENSOR_LOOKUP_SQL, sensor_id)
        if sensor_row is None:
            raise HTTPException(status_code=404, detail=f"Sensor {sensor_id} not found.")

        latest_row = await conn.fetchrow(_LATEST_READING_SQL, sensor_id)
        end = latest_row["latest"]

        if end is None:
            # Sensor exists but has no hourly rows at all -- not an
            # error, just nothing to plot yet.
            return SensorTrendResponse(
                sensor_id=sensor_id,
                sensor_name=sensor_row["sensor_name"],
                start=None,
                end=None,
                point_count=0,
                points=[],
            )

        start = end - timedelta(days=days)
        rows = await conn.fetch(_TREND_SQL, sensor_id, start, end)

    points = [
        TrendPoint(
            local_observation_datetime=r["local_observation_datetime"],
            pedestrian_count=r["pedestrian_count"],
            baseline_median=r["baseline_median"],
            crowd_ratio=r["crowd_ratio"],
            crowd_level=r["crowd_level"],
        )
        for r in rows
    ]

    return SensorTrendResponse(
        sensor_id=sensor_id,
        sensor_name=sensor_row["sensor_name"],
        start=start,
        end=end,
        point_count=len(points),
        points=points,
    )
