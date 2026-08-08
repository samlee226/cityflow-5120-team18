"""
GET /api/crowd-conditions

Returns the most recent available hourly_crowd_features row per sensor,
joined to sensors for display name and coordinates.

"Current" = latest loaded observation per sensor, not necessarily
matching the current wall-clock hour (data load lag is expected). This
is a deliberate simplification -- revisit if the frontend later needs
strict "this exact hour, or nothing" semantics.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query

from app.core.db import get_pool
from app.models.crowd_conditions import (
    CrowdConditionsResponse,
    CrowdLevel,
    SensorCrowdCondition,
)

router = APIRouter(prefix="/api/crowd-conditions", tags=["crowd-conditions"])

# DISTINCT ON picks one row per sensor_id: the latest by observation time.
_LATEST_PER_SENSOR_SQL = """
    SELECT DISTINCT ON (f.sensor_id)
        f.sensor_id,
        s.sensor_name,
        s.latitude,
        s.longitude,
        f.sensing_date,
        f.hour,
        f.local_observation_datetime,
        f.pedestrian_count,
        f.baseline_median,
        f.difference_from_median,
        f.percentage_difference_from_median,
        f.crowd_ratio,
        f.z_score,
        f.crowd_level,
        f.baseline_missing
    FROM hourly_crowd_features AS f
    JOIN sensors AS s ON s.sensor_id = f.sensor_id
    ORDER BY f.sensor_id, f.local_observation_datetime DESC;
"""


@router.get("", response_model=CrowdConditionsResponse)
async def get_crowd_conditions(
    level: Optional[CrowdLevel] = Query(
        default=None, description="Optional filter, e.g. ?level=high"
    ),
) -> CrowdConditionsResponse:
    pool = get_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(_LATEST_PER_SENSOR_SQL)

    conditions = [
        SensorCrowdCondition(
            sensor_id=r["sensor_id"],
            sensor_name=r["sensor_name"],
            latitude=r["latitude"],
            longitude=r["longitude"],
            sensing_date=r["sensing_date"],
            hour=r["hour"],
            local_observation_datetime=r["local_observation_datetime"],
            pedestrian_count=r["pedestrian_count"],
            baseline_median=r["baseline_median"],
            difference_from_median=r["difference_from_median"],
            percentage_difference_from_median=r["percentage_difference_from_median"],
            crowd_ratio=r["crowd_ratio"],
            z_score=r["z_score"],
            crowd_level=r["crowd_level"],
            baseline_missing=r["baseline_missing"],
        )
        for r in rows
    ]

    if level is not None:
        conditions = [c for c in conditions if c.crowd_level == level]

    return CrowdConditionsResponse(
        generated_at=datetime.now(timezone.utc),
        sensor_count=len(conditions),
        conditions=conditions,
    )
