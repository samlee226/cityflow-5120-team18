-- CityFlow migration 008: fixed routing-edge to pedestrian-sensor proximity.

CREATE TABLE edge_sensor_map (
    edge_id BIGINT NOT NULL,
    sensor_id BIGINT NOT NULL,
    distance_m DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (edge_id, sensor_id),
    CONSTRAINT edge_sensor_map_edge_fk
        FOREIGN KEY (edge_id) REFERENCES routing_edges(id) ON DELETE CASCADE,
    CONSTRAINT edge_sensor_map_sensor_fk
        FOREIGN KEY (sensor_id) REFERENCES sensors(sensor_id) ON DELETE CASCADE,
    CONSTRAINT edge_sensor_map_distance_non_negative CHECK (distance_m >= 0)
);

-- The primary key is the Backend lookup index for one or more edge IDs.
CREATE INDEX idx_edge_sensor_map_sensor
    ON edge_sensor_map (sensor_id, edge_id);

COMMENT ON TABLE edge_sensor_map IS
    'Fixed edge-to-sensor proximity derived from routing and sensor geometry. '
    'Rebuild explicitly when either geometry source or the radius changes; '
    'do not refresh during live ingestion.';

COMMENT ON COLUMN edge_sensor_map.distance_m IS
    'Minimum geography distance in metres from the sensor point to the full '
    'routing edge LineString.';
