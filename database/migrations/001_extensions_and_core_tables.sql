-- CityFlow migration 001: required extensions and non-routing core tables.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgrouting;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis') THEN
        RAISE EXCEPTION 'CityFlow requires the PostGIS extension';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgrouting') THEN
        RAISE EXCEPTION 'CityFlow requires the pgRouting extension';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END
$$;

CREATE TABLE pipeline_runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    source_fingerprint TEXT,
    rows_processed BIGINT NOT NULL DEFAULT 0,
    error_message TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pipeline_runs_name_not_blank CHECK (btrim(pipeline_name) <> ''),
    CONSTRAINT pipeline_runs_status_valid
        CHECK (status IN ('started', 'succeeded', 'failed')),
    CONSTRAINT pipeline_runs_rows_non_negative CHECK (rows_processed >= 0),
    CONSTRAINT pipeline_runs_completed_after_started
        CHECK (completed_at IS NULL OR completed_at >= started_at),
    CONSTRAINT pipeline_runs_completion_matches_status CHECK (
        (status = 'started' AND completed_at IS NULL)
        OR (status IN ('succeeded', 'failed') AND completed_at IS NOT NULL)
    )
);

CREATE TABLE sensors (
    sensor_id BIGINT PRIMARY KEY,
    sensor_name TEXT NOT NULL,
    sensor_description TEXT NOT NULL,
    installation_date DATE NOT NULL,
    note TEXT,
    location_type TEXT NOT NULL,
    status TEXT NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    geometry geometry(Point, 4326) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT sensors_name_not_blank CHECK (btrim(sensor_name) <> ''),
    CONSTRAINT sensors_latitude_bounds CHECK (latitude BETWEEN -90 AND 90),
    CONSTRAINT sensors_longitude_bounds CHECK (longitude BETWEEN -180 AND 180),
    CONSTRAINT sensors_geometry_srid CHECK (ST_SRID(geometry) = 4326)
);

CREATE TRIGGER sensors_set_updated_at
BEFORE UPDATE ON sensors
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE sensor_directions (
    sensor_id BIGINT NOT NULL,
    direction_config_id BIGINT NOT NULL,
    direction_1_label TEXT,
    direction_2_label TEXT,
    PRIMARY KEY (sensor_id, direction_config_id),
    CONSTRAINT sensor_directions_sensor_fk
        FOREIGN KEY (sensor_id) REFERENCES sensors(sensor_id) ON DELETE CASCADE,
    CONSTRAINT sensor_directions_positive_config
        CHECK (direction_config_id > 0),
    CONSTRAINT sensor_directions_has_label
        CHECK (direction_1_label IS NOT NULL OR direction_2_label IS NOT NULL)
);

CREATE TABLE landmarks (
    landmark_id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    subcategory TEXT NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    geometry geometry(Point, 4326) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT landmarks_name_not_blank CHECK (btrim(name) <> ''),
    CONSTRAINT landmarks_latitude_bounds CHECK (latitude BETWEEN -90 AND 90),
    CONSTRAINT landmarks_longitude_bounds CHECK (longitude BETWEEN -180 AND 180),
    CONSTRAINT landmarks_geometry_srid CHECK (ST_SRID(geometry) = 4326)
);

CREATE TRIGGER landmarks_set_updated_at
BEFORE UPDATE ON landmarks
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE pedestrian_counts_hourly (
    source_record_id TEXT PRIMARY KEY,
    sensor_id BIGINT NOT NULL,
    sensing_date DATE NOT NULL,
    hour SMALLINT NOT NULL,
    local_observation_datetime TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    year INTEGER NOT NULL,
    month SMALLINT NOT NULL,
    iso_weekday SMALLINT NOT NULL,
    weekday_name TEXT NOT NULL,
    is_weekend BOOLEAN NOT NULL,
    direction_1_count BIGINT NOT NULL,
    direction_2_count BIGINT NOT NULL,
    pedestrian_count BIGINT NOT NULL,
    pipeline_run_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pedestrian_counts_hourly_sensor_fk
        FOREIGN KEY (sensor_id) REFERENCES sensors(sensor_id),
    CONSTRAINT pedestrian_counts_hourly_run_fk
        FOREIGN KEY (pipeline_run_id) REFERENCES pipeline_runs(run_id),
    CONSTRAINT pedestrian_counts_hourly_business_key
        UNIQUE (sensor_id, sensing_date, hour),
    CONSTRAINT pedestrian_counts_hourly_source_id_not_blank
        CHECK (btrim(source_record_id) <> ''),
    CONSTRAINT pedestrian_counts_hourly_hour_range CHECK (hour BETWEEN 0 AND 23),
    CONSTRAINT pedestrian_counts_hourly_month_range CHECK (month BETWEEN 1 AND 12),
    CONSTRAINT pedestrian_counts_hourly_iso_weekday_range
        CHECK (iso_weekday BETWEEN 1 AND 7),
    CONSTRAINT pedestrian_counts_hourly_weekday_name CHECK (
        weekday_name IN (
            'Monday', 'Tuesday', 'Wednesday', 'Thursday',
            'Friday', 'Saturday', 'Sunday'
        )
    ),
    CONSTRAINT pedestrian_counts_hourly_direction_1_non_negative
        CHECK (direction_1_count >= 0),
    CONSTRAINT pedestrian_counts_hourly_direction_2_non_negative
        CHECK (direction_2_count >= 0),
    CONSTRAINT pedestrian_counts_hourly_total_non_negative
        CHECK (pedestrian_count >= 0),
    CONSTRAINT pedestrian_counts_hourly_total_consistent
        CHECK (direction_1_count + direction_2_count = pedestrian_count),
    CONSTRAINT pedestrian_counts_hourly_datetime_consistent CHECK (
        local_observation_datetime
        = sensing_date::TIMESTAMP + make_interval(hours => hour)
    ),
    CONSTRAINT pedestrian_counts_hourly_weekend_consistent
        CHECK (is_weekend = (iso_weekday >= 6))
);

CREATE TRIGGER pedestrian_counts_hourly_set_updated_at
BEFORE UPDATE ON pedestrian_counts_hourly
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE crowd_baselines (
    sensor_id BIGINT NOT NULL,
    iso_weekday SMALLINT NOT NULL,
    hour SMALLINT NOT NULL,
    observation_count BIGINT NOT NULL,
    minimum_sensing_date DATE NOT NULL,
    maximum_sensing_date DATE NOT NULL,
    minimum_pedestrian_count BIGINT NOT NULL,
    maximum_pedestrian_count BIGINT NOT NULL,
    mean_pedestrian_count DOUBLE PRECISION NOT NULL,
    median_pedestrian_count DOUBLE PRECISION NOT NULL,
    population_std_pedestrian_count DOUBLE PRECISION NOT NULL,
    percentile_25_pedestrian_count DOUBLE PRECISION NOT NULL,
    percentile_75_pedestrian_count DOUBLE PRECISION NOT NULL,
    percentile_90_pedestrian_count DOUBLE PRECISION NOT NULL,
    pipeline_run_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sensor_id, iso_weekday, hour),
    CONSTRAINT crowd_baselines_sensor_fk
        FOREIGN KEY (sensor_id) REFERENCES sensors(sensor_id),
    CONSTRAINT crowd_baselines_run_fk
        FOREIGN KEY (pipeline_run_id) REFERENCES pipeline_runs(run_id),
    CONSTRAINT crowd_baselines_iso_weekday_range
        CHECK (iso_weekday BETWEEN 1 AND 7),
    CONSTRAINT crowd_baselines_hour_range CHECK (hour BETWEEN 0 AND 23),
    CONSTRAINT crowd_baselines_observation_count_positive
        CHECK (observation_count > 0),
    CONSTRAINT crowd_baselines_date_range
        CHECK (minimum_sensing_date <= maximum_sensing_date),
    CONSTRAINT crowd_baselines_count_range CHECK (
        minimum_pedestrian_count >= 0
        AND maximum_pedestrian_count >= minimum_pedestrian_count
    ),
    CONSTRAINT crowd_baselines_statistics_non_negative CHECK (
        mean_pedestrian_count >= 0
        AND median_pedestrian_count >= 0
        AND population_std_pedestrian_count >= 0
        AND percentile_25_pedestrian_count >= 0
        AND percentile_75_pedestrian_count >= percentile_25_pedestrian_count
        AND percentile_90_pedestrian_count >= percentile_75_pedestrian_count
    )
);

CREATE TRIGGER crowd_baselines_set_updated_at
BEFORE UPDATE ON crowd_baselines
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
