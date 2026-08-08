"""Tests for deterministic pedestrian-network spatial processing."""

from __future__ import annotations

import json
import math
from pathlib import Path

import networkx as nx
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from cityflow_pipeline.spatial import (
    EXCLUDED_SPATIAL_DATASETS,
    LANDMARK_MAPPING_COLUMNS,
    ROUTING_EDGE_COLUMNS,
    ROUTING_NODE_COLUMNS,
    SENSOR_MAPPING_COLUMNS,
    SpatialProcessingConfig,
    SpatialProcessingError,
    map_landmarks_to_network,
    map_sensors_to_network,
    process_pedestrian_network,
    process_spatial_workflow,
)


LON = 144.96
LAT = -37.81


def point_row(
    object_id: int,
    network_id: int,
    longitude: float,
    latitude: float,
) -> dict[str, object]:
    return {
        "Geo Point": f"{latitude}, {longitude}",
        "Geo Shape": json.dumps(
            {"coordinates": [longitude, latitude], "type": "Point"}
        ),
        "OBJECTID": object_id,
        "NeworkID": network_id,
    }


def line_row(
    object_id: int,
    coordinates: list[list[float]],
    network_id: int | None = None,
) -> dict[str, object]:
    midpoint = coordinates[len(coordinates) // 2]
    return {
        "Geo Point": f"{midpoint[1]}, {midpoint[0]}",
        "Geo Shape": json.dumps(
            {"coordinates": coordinates, "type": "LineString"}
        ),
        "OBJECTID": object_id,
        "NeworkID": network_id,
    }


def simple_network(*, isolated: bool = False) -> pd.DataFrame:
    rows = [
        point_row(101, 1001, LON, LAT),
        point_row(102, 1002, LON + 0.001, LAT),
        line_row(1, [[LON, LAT], [LON + 0.001, LAT]]),
    ]
    if isolated:
        rows.append(point_row(103, 1003, LON + 0.01, LAT + 0.01))
    return pd.DataFrame(rows)


def sensor_frame(
    rows: list[tuple[int, float, float]] | None = None,
) -> pd.DataFrame:
    rows = rows or [(1, LON, LAT)]
    return pd.DataFrame(rows, columns=["sensor_id", "longitude", "latitude"])


def landmark_frame(
    rows: list[tuple[str, float, float]] | None = None,
) -> pd.DataFrame:
    rows = rows or [("landmark-1", LON, LAT)]
    return pd.DataFrame(rows, columns=["landmark_id", "longitude", "latitude"])


def test_valid_point_and_linestring_create_routing_tables() -> None:
    result = process_pedestrian_network(simple_network())

    assert tuple(result.nodes.columns) == ROUTING_NODE_COLUMNS
    assert tuple(result.edges.columns) == ROUTING_EDGE_COLUMNS
    assert result.report.geometry_type_counts == {"LineString": 1, "Point": 2}
    assert result.report.valid_geometry_count == 3
    assert result.report.invalid_geometry_count == 0
    assert result.report.node_count == 2
    assert result.report.edge_count == 1
    assert set(result.nodes["source_network_id"]) == {1001, 1002}
    assert result.edges.loc[0, "source_object_id"] == 1


def test_geojson_coordinate_order_is_longitude_then_latitude() -> None:
    result = process_pedestrian_network(
        pd.DataFrame([point_row(1, 100, 144.5, -37.5)])
    )

    node = result.nodes.iloc[0]
    assert node["longitude"] == 144.5
    assert node["latitude"] == -37.5
    assert node["geometry_wkt"] == "POINT (144.5 -37.5)"


@pytest.mark.parametrize(
    ("raw_shape", "expected_code"),
    [
        ("{not-json", "malformed_geometry"),
        (json.dumps({"type": "Polygon", "coordinates": []}), "unsupported_geometry"),
        (json.dumps({"type": "Point", "coordinates": [181, -37]}), "invalid_coordinates"),
        (json.dumps({"type": "Point", "coordinates": [144, -91]}), "invalid_coordinates"),
        ("", "missing_geometry"),
    ],
)
def test_invalid_geometries_are_reported_not_silently_discarded(
    raw_shape: str, expected_code: str
) -> None:
    frame = pd.DataFrame(
        [{"Geo Point": "", "Geo Shape": raw_shape, "OBJECTID": 1, "NeworkID": None}]
    )

    result = process_pedestrian_network(frame)

    assert result.report.invalid_geometry_count == 1
    assert result.report.issues[0].code == expected_code
    assert result.nodes.empty
    assert result.edges.empty


def test_stable_node_and_edge_ids_and_output_ordering() -> None:
    source = simple_network(isolated=True)

    first = process_pedestrian_network(source)
    second = process_pedestrian_network(
        source.sample(frac=1, random_state=5120).reset_index(drop=True)
    )

    assert_frame_equal(first.nodes, second.nodes)
    assert_frame_equal(first.edges, second.edges)
    assert first.nodes["node_id"].is_monotonic_increasing
    assert first.edges["edge_id"].is_monotonic_increasing


def test_existing_endpoint_matching_and_derived_node_creation() -> None:
    frame = pd.DataFrame(
        [
            point_row(101, 1001, LON, LAT),
            line_row(1, [[LON, LAT], [LON + 0.001, LAT]]),
        ]
    )

    result = process_pedestrian_network(frame)

    assert result.report.matched_endpoint_count == 1
    assert result.report.unmatched_endpoint_count == 1
    assert result.report.derived_node_count == 1
    assert set(result.nodes["node_origin"]) == {"source_point", "derived_endpoint"}


def test_endpoint_matching_tolerance_is_configurable() -> None:
    offset = 0.000001
    frame = pd.DataFrame(
        [
            point_row(101, 1001, LON, LAT),
            line_row(1, [[LON + offset, LAT], [LON + 0.001, LAT]]),
        ]
    )

    narrow = process_pedestrian_network(
        frame, SpatialProcessingConfig(endpoint_tolerance_m=0.01)
    )
    wide = process_pedestrian_network(
        frame, SpatialProcessingConfig(endpoint_tolerance_m=0.2)
    )

    assert narrow.report.derived_node_count == 2
    assert wide.report.derived_node_count == 1
    assert wide.report.matched_endpoint_count == 1


def test_metric_edge_length_is_positive_and_bidirectional_costs_match() -> None:
    result = process_pedestrian_network(simple_network())
    edge = result.edges.iloc[0]

    assert 80 < edge["length_m"] < 100
    assert edge["cost"] == edge["length_m"]
    assert edge["reverse_cost"] == edge["length_m"]
    assert result.bidirectional
    assert result.projected_crs == "EPSG:32755"


def test_zero_length_edge_is_reported_and_excluded() -> None:
    frame = pd.DataFrame([line_row(1, [[LON, LAT], [LON, LAT]])])

    result = process_pedestrian_network(frame)

    assert result.report.zero_length_edge_count == 1
    assert result.report.edge_count == 0
    assert result.nodes.empty


def test_positive_length_self_loop_after_snapping_is_reported() -> None:
    frame = pd.DataFrame(
        [
            point_row(101, 1001, LON, LAT),
            line_row(1, [[LON - 0.000001, LAT], [LON + 0.000001, LAT]]),
        ]
    )

    result = process_pedestrian_network(
        frame,
        SpatialProcessingConfig(
            endpoint_tolerance_m=0.2, zero_length_tolerance_m=0.001
        ),
    )

    assert result.report.self_loop_count == 1
    assert result.report.edge_count == 0
    assert any(issue.code == "self_loop" for issue in result.report.issues)


def test_exact_reversed_duplicate_geometries_are_flagged_and_preserved() -> None:
    coordinates = [[LON, LAT], [LON + 0.001, LAT]]
    frame = pd.DataFrame(
        [
            point_row(101, 1001, LON, LAT),
            point_row(102, 1002, LON + 0.001, LAT),
            line_row(1, coordinates),
            line_row(2, list(reversed(coordinates))),
        ]
    )

    result = process_pedestrian_network(frame)

    assert len(result.edges) == 2
    assert result.edges["edge_id"].is_unique
    assert result.edges["duplicate_geometry"].tolist() == [True, True]
    assert result.report.duplicate_geometry_count == 2


def test_parallel_edges_with_distinct_geometry_are_preserved() -> None:
    frame = pd.DataFrame(
        [
            point_row(101, 1001, LON, LAT),
            point_row(102, 1002, LON + 0.001, LAT),
            line_row(1, [[LON, LAT], [LON + 0.001, LAT]]),
            line_row(
                2,
                [[LON, LAT], [LON + 0.0005, LAT + 0.0002], [LON + 0.001, LAT]],
            ),
        ]
    )

    result = process_pedestrian_network(frame)

    assert len(result.edges) == 2
    assert not result.edges["duplicate_geometry"].any()
    assert result.edges[["source_node_id", "target_node_id"]].nunique().max() == 1


def test_components_primary_component_and_isolated_nodes() -> None:
    result = process_pedestrian_network(simple_network(isolated=True))

    assert result.report.connected_component_count == 2
    assert result.report.largest_component_node_count == 2
    assert result.report.largest_component_edge_count == 1
    assert result.report.isolated_node_count == 1
    assert result.report.largest_component_node_percentage == pytest.approx(200 / 3)
    assert result.nodes["is_primary_component"].sum() == 2
    assert result.edges["is_primary_component"].all()


def test_routing_edge_foreign_keys_and_connectivity_are_valid() -> None:
    result = process_pedestrian_network(simple_network())
    node_ids = set(result.nodes["node_id"])

    assert set(result.edges["source_node_id"]) <= node_ids
    assert set(result.edges["target_node_id"]) <= node_ids
    graph = nx.Graph()
    graph.add_edges_from(
        result.edges[["source_node_id", "target_node_id"]].itertuples(
            index=False, name=None
        )
    )
    assert nx.has_path(
        graph,
        result.edges.loc[0, "source_node_id"],
        result.edges.loc[0, "target_node_id"],
    )


def test_sensor_nearest_node_mapping_and_threshold_flag() -> None:
    network = process_pedestrian_network(simple_network())
    sensors = sensor_frame(
        [(1, LON, LAT), (2, LON + 0.01, LAT + 0.01)]
    )

    mapping = map_sensors_to_network(
        sensors,
        network.nodes,
        SpatialProcessingConfig(sensor_snap_threshold_m=100.0),
    )

    assert tuple(mapping.mappings.columns) == SENSOR_MAPPING_COLUMNS
    assert len(mapping.mappings) == 2
    assert mapping.mappings["within_snap_threshold"].tolist() == [True, False]
    assert mapping.report.outside_threshold_count == 1
    assert mapping.report.minimum_distance_m == pytest.approx(0.0)
    assert mapping.report.maximum_distance_m is not None


def test_landmark_uses_separate_threshold_and_preserves_rows() -> None:
    network = process_pedestrian_network(simple_network())
    landmarks = landmark_frame(
        [("a", LON, LAT), ("b", LON + 0.003, LAT)]
    )

    mapping = map_landmarks_to_network(
        landmarks,
        network.nodes,
        SpatialProcessingConfig(landmark_snap_threshold_m=50.0),
    )

    assert tuple(mapping.mappings.columns) == LANDMARK_MAPPING_COLUMNS
    assert len(mapping.mappings) == 2
    assert mapping.mappings["within_snap_threshold"].tolist() == [True, False]
    assert mapping.report.outside_threshold_count == 1


def test_invalid_mapping_coordinate_is_preserved_and_flagged_unmapped() -> None:
    network = process_pedestrian_network(simple_network())
    sensors = sensor_frame([(1, 999.0, LAT)])

    mapping = map_sensors_to_network(sensors, network.nodes)

    assert len(mapping.mappings) == 1
    assert pd.isna(mapping.mappings.loc[0, "node_id"])
    assert not bool(mapping.mappings.loc[0, "within_snap_threshold"])
    assert mapping.report.unmapped_count == 1


def test_mapping_against_empty_network_preserves_every_source() -> None:
    network = process_pedestrian_network(
        pd.DataFrame(columns=["Geo Point", "Geo Shape", "OBJECTID", "NeworkID"])
    )

    mapping = map_landmarks_to_network(
        landmark_frame([("a", LON, LAT), ("b", LON + 1, LAT)]),
        network.nodes,
    )

    assert len(mapping.mappings) == 2
    assert mapping.mappings["node_id"].isna().all()
    assert mapping.report.unmapped_count == 2
    assert mapping.report.minimum_distance_m is None


def test_complete_workflow_and_exclusions() -> None:
    workflow = process_spatial_workflow(
        simple_network(), sensor_frame(), landmark_frame()
    )

    assert len(workflow.network.edges) == 1
    assert len(workflow.sensor_mapping.mappings) == 1
    assert len(workflow.landmark_mapping.mappings) == 1
    assert workflow.excluded_datasets == EXCLUDED_SPATIAL_DATASETS
    assert "pedestrian_counts_minutely" in workflow.excluded_datasets
    assert "pedestrian_counts_hourly" in workflow.excluded_datasets


def test_input_dataframes_are_not_modified() -> None:
    network = simple_network()
    sensors = sensor_frame()
    landmarks = landmark_frame()
    before_network = network.copy(deep=True)
    before_sensors = sensors.copy(deep=True)
    before_landmarks = landmarks.copy(deep=True)

    process_spatial_workflow(network, sensors, landmarks)

    assert_frame_equal(network, before_network)
    assert_frame_equal(sensors, before_sensors)
    assert_frame_equal(landmarks, before_landmarks)


def test_missing_required_columns_are_rejected() -> None:
    with pytest.raises(SpatialProcessingError, match="missing required columns"):
        process_pedestrian_network(pd.DataFrame({"Geo Shape": []}))

    with pytest.raises(SpatialProcessingError, match="missing required columns"):
        map_sensors_to_network(pd.DataFrame({"sensor_id": [1]}), pd.DataFrame())


def test_configuration_rejects_invalid_tolerances_and_crs() -> None:
    with pytest.raises(ValueError, match="positive finite"):
        SpatialProcessingConfig(endpoint_tolerance_m=0)
    with pytest.raises(ValueError, match="must be projected"):
        SpatialProcessingConfig(projected_crs="EPSG:4326")


def test_processing_is_deterministic() -> None:
    source = simple_network(isolated=True)

    first = process_spatial_workflow(source, sensor_frame(), landmark_frame())
    second = process_spatial_workflow(source, sensor_frame(), landmark_frame())

    assert_frame_equal(first.network.nodes, second.network.nodes)
    assert_frame_equal(first.network.edges, second.network.edges)
    assert_frame_equal(first.sensor_mapping.mappings, second.sensor_mapping.mappings)
    assert first.network.report == second.network.report


def test_no_file_or_database_side_effects(tmp_path: Path) -> None:
    marker = tmp_path / "immutable.csv"
    marker.write_text("unchanged\n", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    process_spatial_workflow(simple_network(), sensor_frame(), landmark_frame())

    after = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert before == after


def test_output_contains_no_non_finite_edge_costs() -> None:
    result = process_pedestrian_network(simple_network())

    for column in ("length_m", "cost", "reverse_cost"):
        assert result.edges[column].map(math.isfinite).all()
        assert result.edges[column].gt(0).all()
