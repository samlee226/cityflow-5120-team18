-- CityFlow migration 003: query indexes and application-facing views.

CREATE INDEX idx_pipeline_runs_pipeline_started
    ON pipeline_runs (pipeline_name, started_at DESC);
CREATE INDEX idx_pipeline_runs_status_started
    ON pipeline_runs (status, started_at DESC);

CREATE INDEX idx_sensors_geometry_gist ON sensors USING GIST (geometry);
CREATE INDEX idx_landmarks_geometry_gist ON landmarks USING GIST (geometry);

CREATE INDEX idx_hourly_sensor_local_datetime
    ON pedestrian_counts_hourly (sensor_id, local_observation_datetime DESC);
CREATE INDEX idx_hourly_sensing_date
    ON pedestrian_counts_hourly (sensing_date DESC);
CREATE INDEX idx_hourly_latest_observations
    ON pedestrian_counts_hourly (local_observation_datetime DESC, sensor_id);
CREATE INDEX idx_hourly_pipeline_run
    ON pedestrian_counts_hourly (pipeline_run_id);

-- The crowd_baselines primary key already supplies the exact composite lookup
-- index on (sensor_id, iso_weekday, hour), so no duplicate index is created.
CREATE INDEX idx_crowd_baselines_pipeline_run
    ON crowd_baselines (pipeline_run_id);

CREATE INDEX idx_routing_nodes_geometry_gist
    ON routing_nodes USING GIST (geometry);
CREATE INDEX idx_routing_nodes_primary_component
    ON routing_nodes (component_id, id)
    WHERE is_primary_component;
CREATE INDEX idx_routing_nodes_source_network
    ON routing_nodes (source_network_id)
    WHERE source_network_id IS NOT NULL;

CREATE INDEX idx_routing_edges_geometry_gist
    ON routing_edges USING GIST (geometry);
CREATE INDEX idx_routing_edges_source ON routing_edges (source);
CREATE INDEX idx_routing_edges_target ON routing_edges (target);
CREATE INDEX idx_routing_edges_primary_component
    ON routing_edges (component_id, source, target)
    WHERE is_primary_component;
CREATE INDEX idx_routing_edges_source_object
    ON routing_edges (source_object_id)
    WHERE source_object_id IS NOT NULL;

CREATE INDEX idx_sensor_network_map_node ON sensor_network_map (node_id);
CREATE INDEX idx_sensor_network_map_primary
    ON sensor_network_map (is_primary_component, within_snap_threshold, sensor_id);
CREATE INDEX idx_landmark_network_map_node ON landmark_network_map (node_id);
CREATE INDEX idx_landmark_network_map_primary
    ON landmark_network_map (
        is_primary_component,
        within_snap_threshold,
        landmark_id
    );

CREATE VIEW routing_edges_pgr AS
WITH ranked_edges AS (
    SELECT
        edge.id,
        edge.source,
        edge.target,
        edge.cost,
        edge.reverse_cost,
        edge.geometry,
        ROW_NUMBER() OVER (
            PARTITION BY ST_AsEWKB(ST_Normalize(edge.geometry))
            ORDER BY edge.edge_uuid, edge.id
        ) AS duplicate_rank
    FROM routing_edges AS edge
    WHERE edge.is_primary_component
      AND edge.source <> edge.target
      AND edge.cost > 0
      AND edge.cost < 'Infinity'::DOUBLE PRECISION
      AND edge.reverse_cost > 0
      AND edge.reverse_cost < 'Infinity'::DOUBLE PRECISION
)
SELECT
    id,
    source,
    target,
    cost,
    reverse_cost,
    geometry
FROM ranked_edges
WHERE duplicate_rank = 1;

COMMENT ON VIEW routing_edges_pgr IS
    'Primary-component pgRouting edges. Exact geometries are normalized so '
    'reversed duplicates share a group; the lowest edge_uuid then id is kept. '
    'Distinct geometries between the same nodes remain as legitimate parallel edges.';

CREATE VIEW hourly_crowd_features AS
SELECT
    hourly.source_record_id,
    hourly.sensor_id,
    hourly.sensing_date,
    hourly.hour,
    hourly.local_observation_datetime,
    hourly.year,
    hourly.month,
    hourly.iso_weekday,
    hourly.weekday_name,
    hourly.is_weekend,
    hourly.direction_1_count,
    hourly.direction_2_count,
    hourly.pedestrian_count,
    hourly.pipeline_run_id,
    baseline.observation_count AS baseline_observation_count,
    baseline.mean_pedestrian_count AS baseline_mean,
    baseline.median_pedestrian_count AS baseline_median,
    baseline.population_std_pedestrian_count AS baseline_standard_deviation,
    baseline.percentile_25_pedestrian_count AS baseline_percentile_25,
    baseline.percentile_75_pedestrian_count AS baseline_percentile_75,
    baseline.percentile_90_pedestrian_count AS baseline_percentile_90,
    hourly.pedestrian_count::DOUBLE PRECISION
        - baseline.median_pedestrian_count AS difference_from_median,
    CASE
        WHEN baseline.sensor_id IS NULL
            OR baseline.median_pedestrian_count = 0 THEN NULL
        ELSE (
            hourly.pedestrian_count::DOUBLE PRECISION
            - baseline.median_pedestrian_count
        ) / baseline.median_pedestrian_count * 100.0
    END AS percentage_difference_from_median,
    CASE
        WHEN baseline.sensor_id IS NULL
            OR baseline.median_pedestrian_count = 0 THEN NULL
        ELSE hourly.pedestrian_count::DOUBLE PRECISION
            / baseline.median_pedestrian_count
    END AS crowd_ratio,
    CASE
        WHEN baseline.sensor_id IS NULL THEN NULL
        WHEN baseline.population_std_pedestrian_count > 0 THEN (
            hourly.pedestrian_count::DOUBLE PRECISION
            - baseline.mean_pedestrian_count
        ) / baseline.population_std_pedestrian_count
        ELSE 0.0
    END AS z_score,
    CASE
        WHEN baseline.sensor_id IS NULL THEN NULL
        WHEN hourly.pedestrian_count
            < baseline.percentile_25_pedestrian_count THEN 'low'
        WHEN hourly.pedestrian_count
            > baseline.percentile_75_pedestrian_count THEN 'high'
        ELSE 'typical'
    END AS crowd_level,
    baseline.sensor_id IS NULL AS baseline_missing
FROM pedestrian_counts_hourly AS hourly
LEFT JOIN crowd_baselines AS baseline
    ON baseline.sensor_id = hourly.sensor_id
   AND baseline.iso_weekday = hourly.iso_weekday
   AND baseline.hour = hourly.hour;

COMMENT ON VIEW hourly_crowd_features IS
    'Descriptive complete-history crowd comparison. Predictive models must use '
    'time-based training splits rather than this full-period baseline.';
