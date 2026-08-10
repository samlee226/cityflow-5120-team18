"""
GET /api/network/nearest-node

Snaps an arbitrary lat/lon (e.g. a point the user tapped on a map) to
the nearest routing_nodes.id, so the frontend can turn a map tap into
node IDs for /api/routes or /api/routes/low-crowd.

Default threshold (250m) reuses the pipeline's own landmark snap
threshold (database/migrations, sensor_network_map/landmark_network_map)
-- a user-tapped destination is conceptually closer to a landmark (an
arbitrary place someone wants to reach) than a sensor (fixed measurement
infrastructure), so we borrowed that number rather than inventing a
third one. Override via ?threshold_m= if 250m proves too loose or
strict once tested against real taps.

Uses PostGIS's KNN "<->" operator (index-accelerated nearest-neighbour
search) against routing_nodes.geometry, mirroring the pattern the
pipeline itself uses when snapping sensors/landmarks -- but computed
live here, since no live version of that snapping exists yet.
"""

from fastapi import APIRouter, HTTPException, Query

from app.core.db import get_pool
from app.models.network import NearestNodeResponse

router = APIRouter(prefix="/api/network", tags=["network"])

DEFAULT_SNAP_THRESHOLD_M = 250.0

_NEAREST_NODE_SQL = """
    SELECT
        id AS node_id,
        ST_Distance(
            geometry::geography,
            ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography
        ) AS distance_m
    FROM routing_nodes
    ORDER BY geometry <-> ST_SetSRID(ST_MakePoint($2, $1), 4326)
    LIMIT 1;
"""


@router.get("/nearest-node", response_model=NearestNodeResponse)
async def get_nearest_node(
    lat: float = Query(..., ge=-90, le=90, description="Latitude, WGS84"),
    lon: float = Query(..., ge=-180, le=180, description="Longitude, WGS84"),
    threshold_m: float = Query(
        default=DEFAULT_SNAP_THRESHOLD_M,
        gt=0,
        description="Distance in metres beyond which within_threshold is false.",
    ),
) -> NearestNodeResponse:
    pool = get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(_NEAREST_NODE_SQL, lat, lon)

    if row is None:
        raise HTTPException(status_code=404, detail="No routing nodes exist yet.")

    distance = row["distance_m"]
    return NearestNodeResponse(
        node_id=row["node_id"],
        distance_m=distance,
        within_threshold=distance <= threshold_m,
        threshold_m=threshold_m,
    )
