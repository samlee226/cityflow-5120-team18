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
