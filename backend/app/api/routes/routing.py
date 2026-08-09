"""
POST /api/routes

Runs pgRouting's Dijkstra shortest-path over routing_edges_pgr and
returns the resulting path, including geometry so the frontend can
draw it on a map. Pure distance-based routing -- see low_crowd_routing.py
for the crowd-penalised variant.

Per the CityFlow database README: the network has no direction
restriction (walking is bidirectional), and cost/reverse_cost both
hold the positive metric edge length. So this query passes both
columns and uses directed => false, rather than a one-way cost column.

Geometry note: each edge's stored geometry has a fixed direction, but
a path can traverse it either way. We orient each edge's geometry to
match the direction actually travelled (comparing the edge's stored
source/target against the path's current node) before merging steps
into a single continuous line -- otherwise the merged route could
visually double back on itself at reversed edges.

All inputs are passed as bound parameters, never string-interpolated
into the query.
"""

import json

from fastapi import APIRouter, HTTPException

from app.core.db import get_pool
from app.core.geo import merge_line_geometries
from app.models.routing import RouteRequest, RouteResponse, RouteStep

router = APIRouter(prefix="/api/routes", tags=["routing"])

_DIJKSTRA_WITH_GEOMETRY_SQL = """
    WITH path AS (
        SELECT *
        FROM pgr_dijkstra(
            'SELECT id, source, target, cost, reverse_cost FROM routing_edges_pgr',
            $1::bigint,
            $2::bigint,
            directed => false
        )
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
async def get_route(request: RouteRequest) -> RouteResponse:
    pool = get_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _DIJKSTRA_WITH_GEOMETRY_SQL, request.start_node, request.end_node
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
