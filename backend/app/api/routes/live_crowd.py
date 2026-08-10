"""
GET /api/live-crowd-conditions

Returns one row per sensor with the *resolved* crowd reading: live data
when fresh/delayed, otherwise the most recent historical row for that
sensor (hourly_crowd_features). This matches the team's agreed rule --
the source feed lags, so treating only "fresh" as usable left almost
everything NULL; "delayed" (<=60 min) is still live and trustworthy,
and only stale/no_data genuinely needs the historical fallback.

`source` on each row says which one was actually used; `live_status`
always shows the underlying live freshness for transparency.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query

from app.core.crowd_sql import EFFECTIVE_SENSOR_CROWD_CTE
from app.core.db import get_pool
from app.models.live_crowd import (
    CrowdConditionsResponse,
    CrowdSource,
    DataStatus,
    LiveCrowdLevel,
    SensorCrowdCondition,
)

router = APIRouter(prefix="/api/live-crowd-conditions", tags=["crowd-conditions"])

_QUERY_SQL = f"""
    WITH {EFFECTIVE_SENSOR_CROWD_CTE}
    SELECT
        s.sensor_id,
        s.sensor_name,
        s.latitude,
        s.longitude,
        e.crowd_source,
        e.effective_crowd_ratio,
        e.effective_crowd_level,
        e.effective_observed_at,
        e.live_status
    FROM sensors s
    LEFT JOIN effective_sensor_crowd e ON e.sensor_id = s.sensor_id
    ORDER BY s.sensor_name;
"""


@router.get("", response_model=CrowdConditionsResponse)
async def get_crowd_conditions(
    level: Optional[LiveCrowdLevel] = Query(
        default=None, description="Optional filter on the resolved crowd_level, e.g. ?level=high"
    ),
    source: Optional[CrowdSource] = Query(
        default=None, description="Optional filter on where the data came from, e.g. ?source=live"
    ),
) -> CrowdConditionsResponse:
    pool = get_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(_QUERY_SQL)

    conditions = [
        SensorCrowdCondition(
            sensor_id=r["sensor_id"],
            sensor_name=r["sensor_name"],
            latitude=r["latitude"],
            longitude=r["longitude"],
            source=r["crowd_source"] or CrowdSource.none,
            crowd_ratio=r["effective_crowd_ratio"],
            crowd_level=r["effective_crowd_level"],
            observed_at=r["effective_observed_at"],
            live_status=r["live_status"] or DataStatus.no_data,
        )
        for r in rows
    ]

    if level is not None:
        conditions = [c for c in conditions if c.crowd_level == level]
    if source is not None:
        conditions = [c for c in conditions if c.source == source]

    return CrowdConditionsResponse(
        generated_at=datetime.now(timezone.utc),
        sensor_count=len(conditions),
        conditions=conditions,
    )
