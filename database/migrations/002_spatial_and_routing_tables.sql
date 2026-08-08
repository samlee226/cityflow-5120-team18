-- CityFlow migration 002: routing tables and source-to-network mappings.

CREATE TABLE routing_nodes (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    node_uuid UUID NOT NULL UNIQUE,
    source_object_id BIGINT,
    source_network_id BIGINT,
    longitude DOUBLE PRECISION NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    geometry geometry(Point, 4326) NOT NULL,
    component_id BIGINT NOT NULL,
    is_primary_component BOOLEAN NOT NULL,
    node_origin TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT routing_nodes_latitude_bounds CHECK (latitude BETWEEN -90 AND 90),
    CONSTRAINT routing_nodes_longitude_bounds CHECK (longitude BETWEEN -180 AND 180),
    CONSTRAINT routing_nodes_geometry_srid CHECK (ST_SRID(geometry) = 4326),
    CONSTRAINT routing_nodes_component_positive CHECK (component_id > 0),
    CONSTRAINT routing_nodes_origin_valid
        CHECK (node_origin IN ('source_point', 'derived_endpoint')),
    CONSTRAINT routing_nodes_source_traceability CHECK (
        (node_origin = 'source_point' AND source_network_id IS NOT NULL)
        OR (node_origin = 'derived_endpoint' AND source_network_id IS NULL)
    )
);

CREATE TRIGGER routing_nodes_set_updated_at
BEFORE UPDATE ON routing_nodes
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE routing_edges (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edge_uuid UUID NOT NULL UNIQUE,
    source_object_id BIGINT,
    source_network_id BIGINT,
    source BIGINT NOT NULL,
    target BIGINT NOT NULL,
    length_m DOUBLE PRECISION NOT NULL,
    cost DOUBLE PRECISION NOT NULL,
    reverse_cost DOUBLE PRECISION NOT NULL,
    geometry geometry(LineString, 4326) NOT NULL,
    component_id BIGINT NOT NULL,
    is_primary_component BOOLEAN NOT NULL,
    duplicate_geometry BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT routing_edges_source_fk
        FOREIGN KEY (source) REFERENCES routing_nodes(id),
    CONSTRAINT routing_edges_target_fk
        FOREIGN KEY (target) REFERENCES routing_nodes(id),
    CONSTRAINT routing_edges_distinct_endpoints CHECK (source <> target),
    CONSTRAINT routing_edges_length_positive CHECK (length_m > 0),
    CONSTRAINT routing_edges_cost_positive CHECK (cost > 0),
    CONSTRAINT routing_edges_reverse_cost_positive CHECK (reverse_cost > 0),
    CONSTRAINT routing_edges_geometry_srid CHECK (ST_SRID(geometry) = 4326),
    CONSTRAINT routing_edges_component_positive CHECK (component_id > 0)
);

COMMENT ON COLUMN routing_edges.cost IS
    'Initial bidirectional pedestrian cost equal to projected edge length in metres.';
COMMENT ON COLUMN routing_edges.reverse_cost IS
    'Initial reverse pedestrian cost equal to projected edge length in metres.';

CREATE TRIGGER routing_edges_set_updated_at
BEFORE UPDATE ON routing_edges
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE sensor_network_map (
    sensor_id BIGINT PRIMARY KEY,
    node_id BIGINT NOT NULL,
    snap_distance_m DOUBLE PRECISION NOT NULL,
    within_snap_threshold BOOLEAN NOT NULL,
    component_id BIGINT NOT NULL,
    is_primary_component BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT sensor_network_map_sensor_fk
        FOREIGN KEY (sensor_id) REFERENCES sensors(sensor_id) ON DELETE CASCADE,
    CONSTRAINT sensor_network_map_node_fk
        FOREIGN KEY (node_id) REFERENCES routing_nodes(id),
    CONSTRAINT sensor_network_map_distance_non_negative
        CHECK (snap_distance_m >= 0),
    CONSTRAINT sensor_network_map_component_positive CHECK (component_id > 0)
);

CREATE TRIGGER sensor_network_map_set_updated_at
BEFORE UPDATE ON sensor_network_map
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE landmark_network_map (
    landmark_id UUID PRIMARY KEY,
    node_id BIGINT NOT NULL,
    snap_distance_m DOUBLE PRECISION NOT NULL,
    within_snap_threshold BOOLEAN NOT NULL,
    component_id BIGINT NOT NULL,
    is_primary_component BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT landmark_network_map_landmark_fk
        FOREIGN KEY (landmark_id) REFERENCES landmarks(landmark_id) ON DELETE CASCADE,
    CONSTRAINT landmark_network_map_node_fk
        FOREIGN KEY (node_id) REFERENCES routing_nodes(id),
    CONSTRAINT landmark_network_map_distance_non_negative
        CHECK (snap_distance_m >= 0),
    CONSTRAINT landmark_network_map_component_positive CHECK (component_id > 0)
);

CREATE TRIGGER landmark_network_map_set_updated_at
BEFORE UPDATE ON landmark_network_map
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
