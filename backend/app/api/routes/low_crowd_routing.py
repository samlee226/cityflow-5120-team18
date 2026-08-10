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
    real distance had any known crowd data at all.
  - crowd_aggregation_method and proximity_radius_m are returned
    explicitly, not just documented in code. Aggregation is MAX: the
    highest ratio among sensors in range wins -- the most cautious
    choice.

Crowd data source still follows the team's fallback rule (see
app/core/crowd_sql.py): fresh/delayed live data is used as-is; stale
or no_data falls back to the most recent historical row for that
sensor.

PERFORMANCE:

  - The spatial cost (which sensors are near which edges) is gone --
    app/core/crowd_sql.py queries the precomputed edge_sensor_map table
    (migration 008) instead of a live ST_DWithin join. Requires the map
    to exist:
        python database/migrate.py
        python -m cityflow_pipeline.edge_sensor_map --radius-m 150

  - What's left is resolving each sensor's CURRENT effective crowd
    ratio (the fresh/delayed/historical fallback in
    EFFECTIVE_SENSOR_CROWD_CTE) -- this is real, non-trivial work
    (joins against hourly_crowd_features and latest_sensor_crowd_levels)
    that was being computed TWICE per request: once inside
    pgr_dijkstra's edges query, once again in the path-detail query.
    That's fixed here by resolving it ONCE into a session-scoped TEMP
    TABLE, then having both queries read from that instead of
    recomputing it. The temp table is created inside an explicit
    transaction with ON COMMIT DROP, so it's cleaned up automatically
    and never leaks into the next request that reuses this pooled
    connection.

  - The in-memory response cache (below) is a separate, additional
    layer on top of both of the above -- still useful for genuinely
    repeated identical requests, but no longer load-bearing for a
    single request's performance the way it was before.

  - A freshly created temp table has no query-planner statistics until
    ANALYZEd -- Postgres defaults to guessing it has almost no rows,
    which can push the planner toward a slow join strategy against the
    ~70k-row routing_edges_pgr. An index plus an explicit ANALYZE run
    right after creating the temp table (before either query touches
    it) fixes this; CREATE TABLE AS does not analyse automatically.
"""

import json
import time
from typing import Dict, Tuple

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

# How far (metres) a sensor's influence reaches to nearby edges. Must be
# <= the radius edge_sensor_map was built with, or rows beyond that
# build-time cutoff simply won't exist to filter down to.
PROXIMITY_RADIUS_M = 150.0

# How long a cached route response is considered still valid. Chosen to
# roughly match how often crowd data itself changes (live_runner's own
# refresh cadence), not an arbitrary number.
CACHE_TTL_SECONDS = 600  # 10 minutes

# (start_node, end_node) -> (response, cached_at_epoch_seconds)
_route_cache: Dict[Tuple[int, int], Tuple[LowCrowdRouteResponse, float]] = {}

_EDGE_NEARBY_RATIO_CTE = build_edge_nearby_ratio_cte(PROXIMITY_RADIUS_M)

# Resolves each sensor's current effective crowd ratio and each edge's
# nearby-sensor max ratio ONCE, into a temp table scoped to this
# connection/transaction. ON COMMIT DROP means it's cleaned up
# automatically when the transaction ends, regardless of success or
# failure, before the connection returns to the pool.
_MATERIALIZE_EDGE_RATIO_SQL = f"""
    CREATE TEMP TABLE tmp_edge_nearby_ratio ON COMMIT DROP AS
    WITH {_EDGE_NEARBY_RATIO_CTE}
    SELECT id, max_ratio FROM edge_nearby_ratio;
"""

# A freshly created temp table has NO statistics until analysed --
# Postgres defaults to a near-zero row-count guess, which can make the
# planner choose a bad join strategy (e.g. nested loop instead of hash
# join) for the ~18k-row join against routing_edges_pgr's ~70k rows.
# Both the index and ANALYZE are needed; CREATE TABLE AS does not run
# ANALYZE automatically.
_INDEX_EDGE_RATIO_SQL = "CREATE UNIQUE INDEX ON tmp_edge_nearby_ratio (id);"
_ANALYZE_EDGE_RATIO_SQL = "ANALYZE tmp_edge_nearby_ratio;"

# Passed as a bind parameter (not string-interpolated) into pgr_dijkstra's
# edges-SQL argument, so the embedded quotes never need manual escaping.
# Reads from the temp table -- no recomputation of crowd data here.
_LOW_CROWD_EDGES_SQL = f"""
    SELECT
        e.id,
        e.source,
        e.target,
        e.cost * (
            1 + {CROWD_PENALTY_WEIGHT}
              * GREATEST(COALESCE(t.max_ratio, 1.0) - 1, 0)
        ) AS cost,
        e.reverse_cost * (
            1 + {CROWD_PENALTY_WEIGHT}
              * GREATEST(COALESCE(t.max_ratio, 1.0) - 1, 0)
        ) AS reverse_cost
    FROM routing_edges_pgr e
    LEFT JOIN tmp_edge_nearby_ratio t ON t.id = e.id
"""

# Just runs pgr_dijkstra and returns the raw path -- no crowd/geometry
# join here, kept minimal.
_DIJKSTRA_ONLY_SQL = """
    SELECT seq, node, edge, cost AS weighted_cost, agg_cost AS weighted_agg_cost
    FROM pgr_dijkstra($1, $2::bigint, $3::bigint, directed => false)
    ORDER BY seq;
"""

# Second pass: crowd status, base distance and geometry, scoped to only
# the edge IDs actually in the path. Also reads from the same temp
# table -- crowd data resolved once, used twice.
_PATH_DETAIL_SQL = """
    SELECT
        e.id,
        e.source,
        e.target,
        e.cost AS base_distance_m,
        t.max_ratio AS crowd_ratio,
        ST_AsGeoJSON(e.geometry) AS geometry_geojson
    FROM routing_edges_pgr e
    LEFT JOIN tmp_edge_nearby_ratio t ON t.id = e.id
    WHERE e.id = ANY($1::bigint[]);
"""


def _get_cached(key: Tuple[int, int]):
    entry = _route_cache.get(key)
    if entry is None:
        return None
    response, cached_at = entry
    if time.monotonic() - cached_at > CACHE_TTL_SECONDS:
        del _route_cache[key]
        return None
    return response


def _store_cache(key: Tuple[int, int], response: LowCrowdRouteResponse) -> None:
    now = time.monotonic()
    expired = [k for k, (_, cached_at) in _route_cache.items() if now - cached_at > CACHE_TTL_SECONDS]
    for k in expired:
        del _route_cache[k]

    _route_cache[key] = (response, now)


@router.post("", response_model=LowCrowdRouteResponse)
async def get_low_crowd_route(request: RouteRequest) -> LowCrowdRouteResponse:
    cache_key = (request.start_node, request.end_node)
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    pool = get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(_MATERIALIZE_EDGE_RATIO_SQL)
            await conn.execute(_INDEX_EDGE_RATIO_SQL)
            await conn.execute(_ANALYZE_EDGE_RATIO_SQL)

            path_rows = await conn.fetch(
                _DIJKSTRA_ONLY_SQL,
                _LOW_CROWD_EDGES_SQL,
                request.start_node,
                request.end_node,
            )

            if not path_rows:
                raise HTTPException(
                    status_code=404,
                    detail="No route found between the given nodes.",
                )

            edge_ids = [r["edge"] for r in path_rows if r["edge"] != -1]
            detail_rows = await conn.fetch(_PATH_DETAIL_SQL, edge_ids) if edge_ids else []

    # edge_id -> (source, target, base_distance_m, crowd_ratio, geometry)
    detail_by_edge = {r["id"]: r for r in detail_rows}

    steps = []
    edge_coord_lists = []
    total_distance_m = 0.0
    known_distance_m = 0.0

    for r in path_rows:
        edge_id = r["edge"] if r["edge"] != -1 else None
        detail = detail_by_edge.get(edge_id) if edge_id is not None else None

        geometry = None
        base_distance_m = None
        crowd_ratio = None

        if detail is not None:
            base_distance_m = detail["base_distance_m"]
            crowd_ratio = detail["crowd_ratio"]

            if detail["geometry_geojson"]:
                raw_geometry = json.loads(detail["geometry_geojson"])
                if detail["source"] == r["node"]:
                    geometry = raw_geometry
                elif detail["target"] == r["node"]:
                    geometry = {
                        "type": "LineString",
                        "coordinates": list(reversed(raw_geometry["coordinates"])),
                    }
                else:
                    geometry = raw_geometry
                edge_coord_lists.append(geometry["coordinates"])

        crowd_status = CrowdStatus.known if crowd_ratio is not None else CrowdStatus.unknown

        if base_distance_m is not None:
            total_distance_m += base_distance_m
            if crowd_status == CrowdStatus.known:
                known_distance_m += base_distance_m

        steps.append(
            LowCrowdRouteStep(
                seq=r["seq"],
                edge_id=edge_id,
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

    response = LowCrowdRouteResponse(
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

    _store_cache(cache_key, response)
    return response
