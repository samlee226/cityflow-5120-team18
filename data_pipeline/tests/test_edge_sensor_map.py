"""Tests for the transactional PostGIS edge-to-sensor mapping backfill."""

from __future__ import annotations

import inspect
import math
import os
from uuid import uuid4

import psycopg
import pytest

import cityflow_pipeline.live as live
import cityflow_pipeline.live_runner as live_runner
from cityflow_pipeline.edge_sensor_map import (
    REBUILD_INSERT_SQL,
    EdgeSensorMapError,
    main,
    rebuild_edge_sensor_map,
    validate_radius,
    verify_edge_sensor_map,
)


TEST_DATABASE_ENV = "CITYFLOW_EDGE_SENSOR_MAP_TEST_DATABASE_URL"


@pytest.mark.parametrize(
    "value",
    (0, -1, float("nan"), float("inf"), True, None, "invalid"),
)
def test_invalid_radius_is_rejected(value: object) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        validate_radius(value)


def test_rebuild_sql_uses_postgis_line_distance_in_metres() -> None:
    normalised = " ".join(REBUILD_INSERT_SQL.lower().split())

    assert "st_dwithin(" in normalised
    assert "st_distance(" in normalised
    assert "edge.geometry::geography" in normalised
    assert "sensor.geometry::geography" in normalised
    assert "order by edge.id, sensor.sensor_id" in normalised


def test_invalid_cli_radius_fails_before_connecting(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--radius-m", "0"]) == 1
    assert "positive finite" in capsys.readouterr().out


def test_live_pipeline_does_not_recompute_mapping() -> None:
    source = inspect.getsource(live) + inspect.getsource(live_runner)

    assert "edge_sensor_map" not in source
    assert "rebuild_edge_sensor_map" not in source


@pytest.fixture
def postgis_connection():
    database_url = os.environ.get(TEST_DATABASE_ENV, "").strip()
    if not database_url:
        pytest.skip(f"set {TEST_DATABASE_ENV} to run PostGIS integration tests")
    connection = psycopg.connect(database_url, autocommit=True)
    try:
        yield connection
    finally:
        connection.close()


def _seed_spatial_fixture(connection: psycopg.Connection[object]) -> None:
    """Create directed test edges and sensors in an isolated test database."""

    connection.execute(
        "TRUNCATE edge_sensor_map, routing_edges, routing_nodes, sensors CASCADE"
    )
    node_rows = [
        (index, str(uuid4()))
        for index in range(1, 5)
    ]
    for node_id, node_uuid in node_rows:
        connection.execute(
            """
            INSERT INTO routing_nodes (
                id, node_uuid, longitude, latitude, geometry, component_id,
                is_primary_component, node_origin
            ) OVERRIDING SYSTEM VALUE
            VALUES (
                %s, %s, 144.96, -37.81,
                ST_SetSRID(ST_MakePoint(144.96, -37.81), 4326),
                1, TRUE, 'derived_endpoint'
            )
            """,
            (node_id, node_uuid),
        )

    edge_values = (
        (101, str(uuid4()), 1, 2, False),
        (102, str(uuid4()), 3, 4, False),
        (103, str(uuid4()), 2, 1, True),
    )
    for edge_id, edge_uuid, source, target, reverse_geometry in edge_values:
        connection.execute(
            """
            WITH origin AS (
                SELECT ST_GeogFromText('POINT(144.96 -37.81)') AS point
            ), edge_center AS (
                SELECT CASE WHEN %s = 102
                    THEN ST_Project(point, 50.0, 0.0)
                    ELSE point
                END AS point
                FROM origin
            ), endpoints AS (
                SELECT
                    ST_Project(point, 100.0, radians(270.0))::geometry AS west,
                    ST_Project(point, 100.0, radians(90.0))::geometry AS east
                FROM edge_center
            )
            INSERT INTO routing_edges (
                id, edge_uuid, source, target, length_m, cost, reverse_cost,
                geometry, component_id, is_primary_component,
                duplicate_geometry
            ) OVERRIDING SYSTEM VALUE
            SELECT
                %s, %s, %s, %s, 200.0, 200.0, 200.0,
                CASE WHEN %s
                    THEN ST_MakeLine(east, west)
                    ELSE ST_MakeLine(west, east)
                END,
                1, TRUE, %s
            FROM endpoints
            """,
            (
                edge_id,
                edge_id,
                edge_uuid,
                source,
                target,
                reverse_geometry,
                reverse_geometry,
            ),
        )

    for sensor_id, north_distance_m in (
        (1, 0.0),
        (2, 100.0),
        (3, 149.99),
        (4, 201.0),
    ):
        connection.execute(
            """
            WITH projected AS (
                SELECT ST_Project(
                    ST_GeogFromText('POINT(144.96 -37.81)'), %s, 0.0
                )::geometry AS geometry
            )
            INSERT INTO sensors (
                sensor_id, sensor_name, sensor_description, location_type,
                status, latitude, longitude, geometry
            )
            SELECT
                %s, %s, 'test sensor', 'Outdoor', 'A',
                ST_Y(geometry), ST_X(geometry), geometry
            FROM projected
            """,
            (north_distance_m, sensor_id, f"sensor-{sensor_id}"),
        )


def _mapping_rows(connection: psycopg.Connection[object]) -> list[tuple[int, int, float]]:
    return [
        (int(edge_id), int(sensor_id), float(distance_m))
        for edge_id, sensor_id, distance_m in connection.execute(
            "SELECT edge_id, sensor_id, distance_m "
            "FROM edge_sensor_map ORDER BY edge_id, sensor_id"
        ).fetchall()
    ]


class _FailOnAnalyzeConnection:
    def __init__(self, connection: psycopg.Connection[object]) -> None:
        self.connection = connection

    def transaction(self):
        return self.connection.transaction()

    def execute(self, query: str, params: object = None):
        if query == "ANALYZE edge_sensor_map":
            raise psycopg.DatabaseError("forced test failure")
        return self.connection.execute(query, params)


def test_postgis_spatial_contract_idempotency_and_rollback(
    postgis_connection: psycopg.Connection[object],
) -> None:
    _seed_spatial_fixture(postgis_connection)

    boundary_distance = float(
        postgis_connection.execute(
            """
            SELECT ST_Distance(sensor.geometry::geography, edge.geometry::geography)
            FROM sensors AS sensor
            CROSS JOIN routing_edges AS edge
            WHERE sensor.sensor_id = 3 AND edge.id = 101
            """
        ).fetchone()[0]
    )
    assert boundary_distance == pytest.approx(150.0, abs=0.05)

    first = rebuild_edge_sensor_map(connection=postgis_connection, radius_m=150.0)
    first_rows = _mapping_rows(postgis_connection)

    assert first.rows_inserted == 9
    assert first.statistics.mapping_rows == 9
    assert first.statistics.distinct_edges == 3
    assert first.statistics.distinct_sensors == 3
    assert first.statistics.coverage_percentage == pytest.approx(100.0)
    assert first.statistics.minimum_distance_m == pytest.approx(0.0, abs=0.05)
    assert first.statistics.maximum_distance_m == pytest.approx(150.0, abs=0.05)
    assert first.statistics.is_valid
    assert (101, 3) in {(edge_id, sensor_id) for edge_id, sensor_id, _ in first_rows}
    assert not any(sensor_id == 4 for _, sensor_id, _ in first_rows)
    assert sum(edge_id == 101 for edge_id, _, _ in first_rows) == 3
    assert sum(sensor_id == 1 for _, sensor_id, _ in first_rows) == 3
    assert len({(edge_id, sensor_id) for edge_id, sensor_id, _ in first_rows}) == 9

    middle_distance = next(
        distance
        for edge_id, sensor_id, distance in first_rows
        if edge_id == 101 and sensor_id == 1
    )
    endpoint_distance = float(
        postgis_connection.execute(
            """
            SELECT LEAST(
                ST_Distance(sensor.geometry::geography, ST_StartPoint(edge.geometry)::geography),
                ST_Distance(sensor.geometry::geography, ST_EndPoint(edge.geometry)::geography)
            )
            FROM sensors AS sensor
            CROSS JOIN routing_edges AS edge
            WHERE sensor.sensor_id = 1 AND edge.id = 101
            """
        ).fetchone()[0]
    )
    assert middle_distance == pytest.approx(0.0, abs=0.05)
    assert endpoint_distance > 90.0

    second = rebuild_edge_sensor_map(connection=postgis_connection, radius_m=150.0)
    assert second.rows_inserted == first.rows_inserted
    assert _mapping_rows(postgis_connection) == first_rows
    assert verify_edge_sensor_map(
        connection=postgis_connection, radius_m=150.0
    ).is_valid

    with pytest.raises(EdgeSensorMapError, match="forced test failure"):
        rebuild_edge_sensor_map(
            connection=_FailOnAnalyzeConnection(postgis_connection),
            radius_m=10.0,
        )
    assert _mapping_rows(postgis_connection) == first_rows
    assert math.isfinite(first.elapsed_seconds)
