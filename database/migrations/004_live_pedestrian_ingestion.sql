-- CityFlow migration 004: bounded live pedestrian ingestion and current crowd view.

CREATE TABLE live_ingestion_runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    window_start_utc TIMESTAMPTZ NOT NULL,
    window_end_utc TIMESTAMPTZ NOT NULL,
    max_source_timestamp_utc TIMESTAMPTZ,
    records_fetched BIGINT NOT NULL DEFAULT 0,
    records_loaded BIGINT NOT NULL DEFAULT 0,
    records_unchanged BIGINT NOT NULL DEFAULT 0,
    records_quarantined BIGINT NOT NULL DEFAULT 0,
    error_message TEXT,
    CONSTRAINT live_ingestion_runs_status_valid CHECK (
        status IN ('running', 'succeeded', 'succeeded_with_warnings', 'failed')
    ),
    CONSTRAINT live_ingestion_runs_window_valid
        CHECK (window_start_utc < window_end_utc),
    CONSTRAINT live_ingestion_runs_counts_non_negative CHECK (
        records_fetched >= 0
        AND records_loaded >= 0
        AND records_unchanged >= 0
        AND records_quarantined >= 0
    ),
    CONSTRAINT live_ingestion_runs_completed_after_started
        CHECK (completed_at IS NULL OR completed_at >= started_at),
    CONSTRAINT live_ingestion_runs_completion_matches_status CHECK (
        (status = 'running' AND completed_at IS NULL)
        OR (status <> 'running' AND completed_at IS NOT NULL)
    )
);

CREATE TABLE pedestrian_counts_minutely_live (
    sensor_id BIGINT NOT NULL,
    sensing_datetime_utc TIMESTAMPTZ NOT NULL,
    sensing_date_local DATE NOT NULL,
    sensing_time_local TIME WITHOUT TIME ZONE NOT NULL,
    iso_weekday SMALLINT NOT NULL,
    local_hour SMALLINT NOT NULL,
    direction_1_count BIGINT NOT NULL,
    direction_2_count BIGINT NOT NULL,
    pedestrian_count BIGINT NOT NULL,
    source_dataset_id TEXT NOT NULL,
    source_payload_fingerprint CHAR(64) NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    live_run_id UUID NOT NULL,
    CONSTRAINT pedestrian_counts_minutely_live_business_key
        UNIQUE (sensor_id, sensing_datetime_utc),
    CONSTRAINT pedestrian_counts_minutely_live_sensor_fk
        FOREIGN KEY (sensor_id) REFERENCES sensors(sensor_id),
    CONSTRAINT pedestrian_counts_minutely_live_run_fk
        FOREIGN KEY (live_run_id) REFERENCES live_ingestion_runs(run_id),
    CONSTRAINT pedestrian_counts_minutely_live_iso_weekday_range
        CHECK (iso_weekday BETWEEN 1 AND 7),
    CONSTRAINT pedestrian_counts_minutely_live_local_hour_range
        CHECK (local_hour BETWEEN 0 AND 23),
    CONSTRAINT pedestrian_counts_minutely_live_direction_1_non_negative
        CHECK (direction_1_count >= 0),
    CONSTRAINT pedestrian_counts_minutely_live_direction_2_non_negative
        CHECK (direction_2_count >= 0),
    CONSTRAINT pedestrian_counts_minutely_live_total_non_negative
        CHECK (pedestrian_count >= 0),
    CONSTRAINT pedestrian_counts_minutely_live_total_consistent
        CHECK (direction_1_count + direction_2_count = pedestrian_count),
    CONSTRAINT pedestrian_counts_minutely_live_dataset_not_blank
        CHECK (btrim(source_dataset_id) <> ''),
    CONSTRAINT pedestrian_counts_minutely_live_fingerprint_format
        CHECK (source_payload_fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE TABLE pedestrian_counts_minutely_quarantine (
    quarantine_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    issue_reason TEXT NOT NULL,
    location_id BIGINT,
    sensing_datetime_utc TIMESTAMPTZ,
    source_payload_fingerprint CHAR(64) NOT NULL,
    original_payload JSONB NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    live_run_id UUID NOT NULL,
    CONSTRAINT pedestrian_counts_minutely_quarantine_reason_valid CHECK (
        issue_reason IN (
            'conflicting_duplicate',
            'existing_record_conflict',
            'unknown_sensor'
        )
    ),
    CONSTRAINT pedestrian_counts_minutely_quarantine_fingerprint_format
        CHECK (source_payload_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT pedestrian_counts_minutely_quarantine_run_fk
        FOREIGN KEY (live_run_id) REFERENCES live_ingestion_runs(run_id)
);

CREATE INDEX idx_live_ingestion_runs_successful_watermark
    ON live_ingestion_runs (max_source_timestamp_utc DESC, completed_at DESC)
    WHERE status IN ('succeeded', 'succeeded_with_warnings');
CREATE INDEX idx_live_minutely_sensor_timestamp
    ON pedestrian_counts_minutely_live (sensor_id, sensing_datetime_utc DESC);
CREATE INDEX idx_live_minutely_timestamp_sensor
    ON pedestrian_counts_minutely_live (sensing_datetime_utc DESC, sensor_id);
CREATE INDEX idx_live_minutely_run
    ON pedestrian_counts_minutely_live (live_run_id);
CREATE INDEX idx_live_quarantine_run_reason
    ON pedestrian_counts_minutely_quarantine (live_run_id, issue_reason);

CREATE VIEW latest_sensor_crowd_levels AS
WITH window_bounds AS (
    SELECT
        date_bin(
            INTERVAL '15 minutes',
            CURRENT_TIMESTAMP,
            TIMESTAMPTZ '2001-01-01 00:00:00+00'
        ) - INTERVAL '15 minutes' AS window_start_utc,
        date_bin(
            INTERVAL '15 minutes',
            CURRENT_TIMESTAMP,
            TIMESTAMPTZ '2001-01-01 00:00:00+00'
        ) AS window_end_utc,
        CURRENT_TIMESTAMP AS calculated_at
), sensor_observations AS (
    SELECT
        sensor.sensor_id,
        sensor.sensor_name,
        bounds.window_start_utc,
        bounds.window_end_utc,
        bounds.calculated_at,
        current_window.reading_count,
        current_window.observed_15m_count,
        latest.latest_sensing_datetime_utc
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
        WHEN observation.reading_count = 0 THEN 'stale'
        ELSE 'fresh'
    END AS data_status,
    observation.latest_sensing_datetime_utc,
    observation.calculated_at
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
    'One row per canonical sensor for the latest completed 15-minute wall-clock '
    'window. hourly_equivalent_estimate multiplies the observed 15-minute sum by '
    'four; historical typical is exposed as frontend-friendly medium. Missing '
    'readings remain NULL. A sensor with older live history but no current-window '
    'readings is stale; a sensor with no live history is no_data.';
