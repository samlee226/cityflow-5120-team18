"""
POST /api/routes/low-crowd

Same shape as /api/routes, but edges near sensors currently showing
above-baseline crowd cost more to traverse -- so pgr_dijkstra trades
some distance for a calmer path when the numbers justify it.

v2 design: proximity-based, not exact-node-based.

The first version only penalised an edge if a sensor was mapped to
that *exact* routing node via sensor_network_map. Since there are far
more path nodes than sensors, that meant almost no real route ever
passed through a mapped node -- crowd data had no practical effect on
most routing results, even when a sensor right next to the path was
badly crowded.

This version instead asks, per edge: "is any sensor within
PROXIMITY_RADIUS_M metres of this edge's actual geometry?" using
PostGIS's ST_DWithin on geography (accurate real-world metres, not
degrees). An edge takes the highest crowd_ratio among all sensors in
range. This spreads each sensor's influence across a realistic walking
radius instead of a single point, so most routes near a busy sensor
should now actually reflect it -- not just routes that happen to hit
one exact node.

Crowd data source still follows the team's fallback rule (see
app/core/crowd_sql.py): fresh/delayed live data is used as-is; stale
or no_data falls back to the most recent historical row for that
sensor.

Both PROXIMITY_RADIUS_M and CROWD_PENALTY_WEIGHT are open design
choices worth revisiting with the team once there's real usage to
tune against.
"""

import json

from fastapi import APIRouter, HTTPException

from app.core.crowd_sql import EFFECTIVE_SENSOR_CROWD_CTE
from app.core.db import get_pool
from app.core.geo import merge_line_geometries
from app.models.routing import RouteRequest, RouteResponse, RouteStep

router = APIRouter(prefix="/api/routes/low-crowd", tags=["routing"])

# How strongly current crowding discourages an edge. 1.0 means "an edge
# near a sensor at 2x baseline crowd costs roughly 2x as much to walk".
CROWD_PENALTY_WEIGHT = 1.0

# How far (metres) a sensor's influence reaches from its own location
# to nearby edges. Chosen as a rough "can probably see/hear this area
# while walking" distance -- not derived from any measured data.
PROXIMITY_RADIUS_M = 150

# Passed as a bind parameter (not string-interpolated) into pgr_dijkstra's
# edges-SQL argument, so the embedded quotes never need manual escaping.
_LOW_CROWD_EDGES_SQL = f"""
    WITH {EFFECTIVE_SENSOR_CROWD_CTE},
    edge_nearby_ratio AS (
        SELECT
            e.id,
            MAX(esc.effective_crowd_ratio) AS max_ratio
        FROM routing_edges_pgr e
        JOIN sensors s
            ON ST_DWithin(
                s.geometry::geography,
                e.geometry::geography,
                {PROXIMITY_RADIUS_M}
            )
        JOIN effective_sensor_crowd esc ON esc.sensor_id = s.sensor_id
        WHERE esc.effective_crowd_ratio IS NOT NULL
        GROUP BY e.id
    )
    SELECT
        e.id,
        e.source,
        e.target,
        e.cost * (
            1 + {CROWD_PENALTY_WEIGHT}
              * GREATEST(COALESCE(enr.max_ratio, 1.0) - 1, 0)
        ) AS cost,
        e.reverse_cost * (
            1 + {CROWD_PENALTY_WEIGHT}
              * GREATEST(COALESCE(enr.max_ratio, 1.0) - 1, 0)
        ) AS reverse_cost
    FROM routing_edges_pgr e
    LEFT JOIN edge_nearby_ratio enr ON enr.id = e.id
"""

_DIJKSTRA_LOW_CROWD_SQL = """
    WITH path AS (
        SELECT *
        FROM pgr_dijkstra($1, $2::bigint, $3::bigint, directed => false)
    )
    SELECT
        p.seq,
        p.edge,
        p.node,
        p.cost,
        p.agg_cost,
        CASE
            WHEN p.edge = -1 THEN NULL
            WHEN e.source = p.node THEN ST_AsGeoJSON(e.geometry)
            WHEN e.target = p.node THEN ST_AsGeoJSON(ST_Reverse(e.geometry))
            ELSE ST_AsGeoJSON(e.geometry)
        END AS geometry_geojson
    FROM path p
    LEFT JOIN routing_edges_pgr e ON e.id = p.edge
    ORDER BY p.seq;
"""


@router.post("", response_model=RouteResponse)
async def get_low_crowd_route(request: RouteRequest) -> RouteResponse:
    pool = get_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _DIJKSTRA_LOW_CROWD_SQL,
            _LOW_CROWD_EDGES_SQL,
            request.start_node,
            request.end_node,
        )

    if not rows:
        raise HTTPException(
            status_code=404,
            detail="No route found between the given nodes.",
        )

    steps = []
    edge_coord_lists = []
    for r in rows:
        geometry = json.loads(r["geometry_geojson"]) if r["geometry_geojson"] else None
        if geometry is not None:
            edge_coord_lists.append(geometry["coordinates"])

        steps.append(
            RouteStep(
                seq=r["seq"],
                edge_id=r["edge"] if r["edge"] != -1 else None,
                node_id=r["node"],
                cost=r["cost"],
                agg_cost=r["agg_cost"],
                geometry=geometry,
            )
        )

    return RouteResponse(
        start_node=request.start_node,
        end_node=request.end_node,
        total_cost=steps[-1].agg_cost,
        steps=steps,
        route_geometry=merge_line_geometries(edge_coord_lists),
    )
