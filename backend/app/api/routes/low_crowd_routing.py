"""
POST /api/routes/low-crowd

Same idea as /api/routes, but edges near a sensor currently showing
above-baseline crowd cost more to traverse -- so pgr_dijkstra trades
some distance for a calmer path when the numbers justify it.

Response contract is deliberately separate from RouteResponse (used by
plain /api/routes) rather than reusing it -- the plain endpoint has no
concept of crowd data, so it shouldn't carry these fields. See
app/models/low_crowd_routing.py for what's returned and why.

Design notes, addressing gaps flagged in review:

  - Segments with no sensor in range are marked crowd_status="unknown",
    never silently treated as confirmed-low-crowd. Only a sensor
    actually reporting at-or-below baseline counts as a known-calm
    reading.
  - sensor_coverage_ratio tells the caller what fraction of the route's
    real distance had any known crowd data at all -- a route that's
    90% covered is a much more trustworthy "low-crowd" result than one
    that's 10% covered by coincidence.
  - crowd_aggregation_method and proximity_radius_m are returned
    explicitly in the response (not just documented in code), so any
    consumer can see how ties/multiple-sensor cases were resolved
    without reading the SQL. Aggregation is MAX: if multiple sensors
    are in range of the same edge, the highest ratio wins -- the most
    cautious choice.

Crowd data source still follows the team's fallback rule (see
app/core/crowd_sql.py): fresh/delayed live data is used as-is; stale
or no_data falls back to the most recent historical row for that
sensor.
"""

import json

from fastapi import APIRouter, HTTPException

from app.core.crowd_sql import build_edge_nearby_ratio_cte
from app.core.db import get_pool
from app.core.geo import merge_line_geometries
from app.models.low_crowd_routing import (
    CrowdStatus,
    LowCrowdRouteResponse,
    LowCrowdRouteStep,
)
from app.models.routing import RouteRequest

router = APIRouter(prefix="/api/routes/low-crowd", tags=["routing"])

# How strongly current crowding discourages an edge. 1.0 means "an edge
# near a sensor at 2x baseline crowd costs roughly 2x as much to walk".
CROWD_PENALTY_WEIGHT = 1.0

# How far (metres) a sensor's influence reaches to nearby edges.
PROXIMITY_RADIUS_M = 150.0

_EDGE_NEARBY_RATIO_CTE = build_edge_nearby_ratio_cte(PROXIMITY_RADIUS_M)

# Passed as a bind parameter (not string-interpolated) into pgr_dijkstra's
# edges-SQL argument, so the embedded quotes never need manual escaping.
_LOW_CROWD_EDGES_SQL = f"""
    WITH {_EDGE_NEARBY_RATIO_CTE}
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

# The edges-SQL above runs as its own independent query inside
# pgr_dijkstra and can't share CTEs with the outer statement -- so this
# outer query rebuilds the same edge_nearby_ratio lookup to report
# base distance and known/unknown crowd status per step after the fact.
_DIJKSTRA_LOW_CROWD_SQL = f"""
    WITH {_EDGE_NEARBY_RATIO_CTE},
    path AS (
        SELECT *
        FROM pgr_dijkstra($1, $2::bigint, $3::bigint, directed => false)
    )
    SELECT
        p.seq,
        p.edge,
        p.node,
        p.cost AS weighted_cost,
        p.agg_cost AS weighted_agg_cost,
        e.cost AS base_distance_m,
        enr.max_ratio AS crowd_ratio,
        CASE
            WHEN p.edge = -1 THEN NULL
            WHEN e.source = p.node THEN ST_AsGeoJSON(e.geometry)
            WHEN e.target = p.node THEN ST_AsGeoJSON(ST_Reverse(e.geometry))
            ELSE ST_AsGeoJSON(e.geometry)
        END AS geometry_geojson
    FROM path p
    LEFT JOIN routing_edges_pgr e ON e.id = p.edge
    LEFT JOIN edge_nearby_ratio enr ON enr.id = p.edge
    ORDER BY p.seq;
"""


@router.post("", response_model=LowCrowdRouteResponse)
async def get_low_crowd_route(request: RouteRequest) -> LowCrowdRouteResponse:
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
    total_distance_m = 0.0
    known_distance_m = 0.0

    for r in rows:
        geometry = json.loads(r["geometry_geojson"]) if r["geometry_geojson"] else None
        if geometry is not None:
            edge_coord_lists.append(geometry["coordinates"])

        base_distance_m = r["base_distance_m"]
        crowd_ratio = r["crowd_ratio"]
        crowd_status = CrowdStatus.known if crowd_ratio is not None else CrowdStatus.unknown

        if base_distance_m is not None:
            total_distance_m += base_distance_m
            if crowd_status == CrowdStatus.known:
                known_distance_m += base_distance_m

        steps.append(
            LowCrowdRouteStep(
                seq=r["seq"],
                edge_id=r["edge"] if r["edge"] != -1 else None,
                node_id=r["node"],
                cost=r["weighted_cost"],
                agg_cost=r["weighted_agg_cost"],
                base_distance_m=base_distance_m,
                crowd_status=crowd_status,
                crowd_ratio=crowd_ratio,
                geometry=geometry,
            )
        )

    sensor_coverage_ratio = (
        known_distance_m / total_distance_m if total_distance_m > 0 else 0.0
    )

    return LowCrowdRouteResponse(
        start_node=request.start_node,
        end_node=request.end_node,
        total_cost=steps[-1].agg_cost,
        total_distance_m=total_distance_m,
        sensor_coverage_ratio=sensor_coverage_ratio,
        crowd_aggregation_method="max",
        proximity_radius_m=PROXIMITY_RADIUS_M,
        steps=steps,
        route_geometry=merge_line_geometries(edge_coord_lists),
    )
