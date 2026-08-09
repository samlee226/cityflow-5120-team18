"""
Shared CTE fragment implementing the crowd-data fallback rule agreed with
the data team:

  - data_status fresh or delayed -> use latest_sensor_crowd_levels (live)
  - data_status stale or no_data -> use hourly_crowd_features (historical)

Because the source feed itself lags, "fresh" alone was too strict and
left crowd_ratio NULL almost always. "delayed" (<=60 min old) is still
live data and is trusted the same as fresh; only stale/no_data falls
back to the most recently loaded historical row per sensor.

Compose into a query as:
    f"WITH {EFFECTIVE_SENSOR_CROWD_CTE} SELECT ... FROM effective_sensor_crowd ..."
Do not put a leading/trailing WITH or comma -- callers own the outer WITH.

Vocabulary note: hourly_crowd_features (historical) uses 'low'/'typical'/
'high'; latest_sensor_crowd_levels (live) uses 'low'/'medium'/'high'.
'typical' is normalised to 'medium' below so effective_crowd_level is
always one consistent vocabulary regardless of which source was used.
"""

EFFECTIVE_SENSOR_CROWD_CTE = """
historical_latest AS (
    SELECT DISTINCT ON (sensor_id)
        sensor_id,
        crowd_ratio AS hist_crowd_ratio,
        crowd_level AS hist_crowd_level,
        local_observation_datetime AS hist_observed_at
    FROM hourly_crowd_features
    ORDER BY sensor_id, local_observation_datetime DESC
),
effective_sensor_crowd AS (
    SELECT
        l.sensor_id,
        l.data_status AS live_status,
        CASE
            WHEN l.data_status IN ('fresh', 'delayed') THEN 'live'
            WHEN h.sensor_id IS NOT NULL THEN 'historical'
            ELSE 'none'
        END AS crowd_source,
        CASE
            WHEN l.data_status IN ('fresh', 'delayed') THEN l.crowd_ratio
            ELSE h.hist_crowd_ratio
        END AS effective_crowd_ratio,
        CASE
            WHEN l.data_status IN ('fresh', 'delayed') THEN l.crowd_level::text
            WHEN h.hist_crowd_level = 'typical' THEN 'medium'
            ELSE h.hist_crowd_level
        END AS effective_crowd_level,
        CASE
            WHEN l.data_status IN ('fresh', 'delayed') THEN l.latest_sensing_datetime_utc
            ELSE h.hist_observed_at
        END AS effective_observed_at
    FROM latest_sensor_crowd_levels l
    LEFT JOIN historical_latest h ON h.sensor_id = l.sensor_id
)
""".strip()


def build_edge_nearby_ratio_cte(radius_m: float) -> str:
    """
    Builds on EFFECTIVE_SENSOR_CROWD_CTE to add edge_nearby_ratio: one row
    per edge that has at least one sensor with a known (non-NULL) ratio
    within radius_m metres, using PostGIS ST_DWithin on geography.

    Aggregation rule when multiple sensors are in range: MAX(ratio) is
    used -- the most cautious choice, since a road bordered by both a
    calm and a very crowded sensor is treated as crowded, not averaged
    down. This is a deliberate, documented choice, not an arbitrary
    default -- surface it in any response that uses this CTE so
    consumers don't have to read the SQL to know it.

    An edge with NO row in edge_nearby_ratio (not just ratio <= 1.0) means
    "no sensor with known data was in range at all" -- callers should
    treat that as unknown, not as confirmed-low-crowd. Distinguish this
    from an edge that has a row but effective_crowd_ratio is exactly at
    or below baseline, which IS a known, confirmed-calm reading.

    radius_m is a Python-side constant, never user input, so it's safe
    to interpolate directly into the SQL text here.
    """
    return f"""
{EFFECTIVE_SENSOR_CROWD_CTE},
edge_nearby_ratio AS (
    SELECT
        e.id,
        MAX(esc.effective_crowd_ratio) AS max_ratio
    FROM routing_edges_pgr e
    JOIN sensors s
        ON ST_DWithin(
            s.geometry::geography,
            e.geometry::geography,
            {radius_m}
        )
    JOIN effective_sensor_crowd esc ON esc.sensor_id = s.sensor_id
    WHERE esc.effective_crowd_ratio IS NOT NULL
    GROUP BY e.id
)
""".strip()
