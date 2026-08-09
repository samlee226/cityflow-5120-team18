"""
POST /api/routes/low-crowd

Same shape as /api/routes, but edges near a sensor currently reporting
above-baseline crowd (via latest_sensor_crowd_levels, migration 004)
cost more to traverse -- so pgr_dijkstra will trade some distance for
a calmer path when the numbers justify it.

v1 design (deliberately simple, revisit with the team as a next step):
  - An edge is "near" a sensor if that sensor's mapped node
    (sensor_network_map) is the edge's source or target, AND the snap
    was within the pipeline's own distance threshold
    (within_snap_threshold) -- we don't trust a poorly-snapped sensor
    to represent that edge.
  - Only "fresh" live readings count. Stale or missing data does not
    penalise an edge -- no data is treated as neutral, not as "safe"
    or "busy".
  - Penalty formula: cost *= 1 + CROWD_PENALTY_WEIGHT * max(crowd_ratio - 1, 0)
    A sensor at exactly its baseline (ratio 1.0) adds no penalty. One
    at double its baseline roughly doubles that edge's effective cost
    when CROWD_PENALTY_WEIGHT = 1.0 (see constant below).
  - This is a real product decision, not just plumbing -- the weight,
    the "near" definition, and whether landmarks/other signals should
    also factor in are all open questions for the team, not just this
    endpoint.
"""

import json

from fastapi import APIRouter, HTTPException

from app.core.db import get_pool
from app.core.geo import merge_line_geometries
from app.models.routing import RouteRequest, RouteResponse, RouteStep

router = APIRouter(prefix="/api/routes/low-crowd", tags=["routing"])

# Tunable: how strongly current crowding discourages an edge. 1.0 means
# "an edge at 2x baseline crowd costs roughly 2x as much to walk".
CROWD_PENALTY_WEIGHT = 1.0

# Passed as a bind parameter (not string-interpolated) into pgr_dijkstra's
# edges-SQL argument, so we never have to hand-escape the 'fresh' literal
# inside a string that itself gets embedded in another string.
_LOW_CROWD_EDGES_SQL = f"""
    SELECT
        e.id,
        e.source,
        e.target,
        e.cost * (
            1 + {CROWD_PENALTY_WEIGHT} * GREATEST(
                GREATEST(
                    COALESCE(src_crowd.crowd_ratio, 1.0),
                    COALESCE(tgt_crowd.crowd_ratio, 1.0)
                ) - 1, 0
            )
        ) AS cost,
        e.reverse_cost * (
            1 + {CROWD_PENALTY_WEIGHT} * GREATEST(
                GREATEST(
                    COALESCE(src_crowd.crowd_ratio, 1.0),
                    COALESCE(tgt_crowd.crowd_ratio, 1.0)
                ) - 1, 0
            )
        ) AS reverse_cost
    FROM routing_edges_pgr e
    LEFT JOIN sensor_network_map src_map
        ON src_map.node_id = e.source AND src_map.within_snap_threshold
    LEFT JOIN latest_sensor_crowd_levels src_crowd
        ON src_crowd.sensor_id = src_map.sensor_id AND src_crowd.data_status = 'fresh'
    LEFT JOIN sensor_network_map tgt_map
        ON tgt_map.node_id = e.target AND tgt_map.within_snap_threshold
    LEFT JOIN latest_sensor_crowd_levels tgt_crowd
        ON tgt_crowd.sensor_id = tgt_map.sensor_id AND tgt_crowd.data_status = 'fresh'
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
