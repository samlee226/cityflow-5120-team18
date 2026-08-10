-- CityFlow migration 006: anchor live crowd windows to source availability.

CREATE OR REPLACE VIEW latest_sensor_crowd_levels AS
WITH source_anchor AS (
    SELECT max(live.sensing_datetime_utc) AS latest_source_timestamp_utc
    FROM pedestrian_counts_minutely_live AS live
), window_bounds AS (
    SELECT
        CASE WHEN anchor.latest_source_timestamp_utc IS NULL THEN NULL
            ELSE date_bin(
                INTERVAL '15 minutes',
                anchor.latest_source_timestamp_utc,
                TIMESTAMPTZ '2001-01-01 00:00:00+00'
            ) - INTERVAL '15 minutes'
        END AS window_start_utc,
        CASE WHEN anchor.latest_source_timestamp_utc IS NULL THEN NULL
            ELSE date_bin(
                INTERVAL '15 minutes',
                anchor.latest_source_timestamp_utc,
                TIMESTAMPTZ '2001-01-01 00:00:00+00'
            )
        END AS window_end_utc,
        anchor.latest_source_timestamp_utc,
        CURRENT_TIMESTAMP AS calculated_at
    FROM source_anchor AS anchor
), sensor_observations AS (
    SELECT
        sensor.sensor_id,
        sensor.sensor_name,
        bounds.window_start_utc,
        bounds.window_end_utc,
        bounds.calculated_at,
        current_window.reading_count,
        current_window.observed_15m_count,
        latest.latest_sensing_datetime_utc,
        bounds.latest_source_timestamp_utc
    FROM sensors AS sensor
    CROSS JOIN window_bounds AS bounds
    LEFT JOIN LATERAL (
        SELECT
            count(*)::BIGINT AS reading_count,
            sum(live.pedestrian_count)::BIGINT AS observed_15m_count
        FROM pedestrian_counts_minutely_live AS live
        WHERE live.sensor_id = sensor.sensor_id
          AND live.sensing_datetime_utc >= bounds.window_start_utc
          AND live.sensing_datetime_utc < bounds.window_end_utc
    ) AS current_window ON TRUE
    LEFT JOIN LATERAL (
        SELECT max(live.sensing_datetime_utc) AS latest_sensing_datetime_utc
        FROM pedestrian_counts_minutely_live AS live
        WHERE live.sensor_id = sensor.sensor_id
    ) AS latest ON TRUE
)
SELECT
    observation.sensor_id,
    observation.sensor_name,
    observation.window_start_utc,
    observation.window_end_utc,
    observation.window_start_utc AT TIME ZONE 'Australia/Melbourne'
        AS window_start_local,
    observation.window_end_utc AT TIME ZONE 'Australia/Melbourne'
        AS window_end_local,
    CASE WHEN observation.reading_count > 0
        THEN observation.observed_15m_count ELSE NULL END AS observed_15m_count,
    observation.reading_count,
    CASE WHEN observation.reading_count > 0
        THEN observation.observed_15m_count * 4 ELSE NULL END
        AS hourly_equivalent_estimate,
    baseline.median_pedestrian_count AS historical_baseline_median,
    baseline.percentile_90_pedestrian_count AS historical_baseline_p90,
    CASE
        WHEN observation.reading_count = 0
            OR baseline.median_pedestrian_count IS NULL
            OR baseline.median_pedestrian_count = 0 THEN NULL
        ELSE (observation.observed_15m_count * 4)::DOUBLE PRECISION
            / baseline.median_pedestrian_count
    END AS crowd_ratio,
    CASE
        WHEN observation.reading_count = 0 OR baseline.sensor_id IS NULL THEN NULL
        WHEN observation.observed_15m_count * 4
            < baseline.percentile_25_pedestrian_count THEN 'low'
        WHEN observation.observed_15m_count * 4
            > baseline.percentile_75_pedestrian_count THEN 'high'
        ELSE 'medium'
    END AS crowd_level,
    CASE WHEN observation.latest_sensing_datetime_utc IS NULL THEN NULL
        ELSE observation.calculated_at - observation.latest_sensing_datetime_utc
    END AS data_age,
    CASE
        WHEN observation.latest_sensing_datetime_utc IS NULL THEN 'no_data'
        WHEN observation.calculated_at - observation.latest_sensing_datetime_utc
            <= INTERVAL '15 minutes' THEN 'fresh'
        WHEN observation.calculated_at - observation.latest_sensing_datetime_utc
            <= INTERVAL '60 minutes' THEN 'delayed'
        ELSE 'stale'
    END AS data_status,
    observation.latest_sensing_datetime_utc,
    observation.calculated_at,
    observation.latest_source_timestamp_utc
FROM sensor_observations AS observation
LEFT JOIN crowd_baselines AS baseline
    ON baseline.sensor_id = observation.sensor_id
   AND baseline.iso_weekday = EXTRACT(
        ISODOW FROM observation.window_start_utc AT TIME ZONE 'Australia/Melbourne'
   )::SMALLINT
   AND baseline.hour = EXTRACT(
        HOUR FROM observation.window_start_utc AT TIME ZONE 'Australia/Melbourne'
   )::SMALLINT;

COMMENT ON VIEW latest_sensor_crowd_levels IS
    'One row per canonical sensor for the latest completed 15-minute window '
    'anchored to the latest available source timestamp. data_age compares each '
    'sensor latest reading with database time: up to 15 minutes is fresh, up to '
    '60 minutes is delayed, and older data is stale; no history is no_data.';
