"""Unit tests for the PostgreSQL/PostGIS loading layer.

These tests use controlled connections and synthetic frames; no database,
Docker, network access, or real CityFlow data is required.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import date, datetime
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
import pytest

import cityflow_pipeline.load as load_module
from cityflow_pipeline.baseline import BASELINE_COLUMNS
from cityflow_pipeline.load import (
    DatabaseLoadError,
    DatabaseLoaderConfig,
    HistoricalLoadResult,
    PostgresLoader,
    TableLoadResult,
)
from cityflow_pipeline.spatial import (
    LANDMARK_MAPPING_COLUMNS,
    ROUTING_EDGE_COLUMNS,
    ROUTING_NODE_COLUMNS,
    SENSOR_MAPPING_COLUMNS,
)
from cityflow_pipeline.transform import (
    HOURLY_FACT_COLUMNS,
    LANDMARK_DIMENSION_COLUMNS,
    SENSOR_DIMENSION_COLUMNS,
    SENSOR_DIRECTION_TABLE_COLUMNS,
)
from cityflow_pipeline.validate import (
    HistoricalValidationReport,
    ValidationIssue,
    ValidationReport,
)


class ResultCursor:
    def __init__(self, rows: list[tuple[object, ...]] | None = None, rowcount: int = 0):
        self.rows = rows or []
        self.rowcount = rowcount

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None


class ControlledConnection:
    def __init__(self, responses: list[ResultCursor] | None = None):
        self.responses = list(responses or [])
        self.closed = False
        self.executed: list[tuple[object, object]] = []

    def transaction(self) -> Any:
        return nullcontext()

    def execute(self, statement: object, parameters: object = None) -> ResultCursor:
        self.executed.append((statement, parameters))
        return self.responses.pop(0) if self.responses else ResultCursor()

    def close(self) -> None:
        self.closed = True


class RecordingLoader(PostgresLoader):
    def __init__(self, *, responses: list[ResultCursor] | None = None):
        self.fake_connection = ControlledConnection(responses)
        super().__init__(connection=self.fake_connection)  # type: ignore[arg-type]
        self._validated = True
        self.staged_rows: list[tuple[object, ...]] = []
        self.upserts: list[dict[str, object]] = []
        self.final_counts: dict[str, int] = {}
        self.node_mapping: dict[str, int] = {}

    def _create_stage(self, name: str, definitions: object) -> None:
        return None

    def _copy_rows(self, stage_name: str, columns: object, rows: object) -> int:
        copied = list(rows)  # type: ignore[arg-type]
        self.staged_rows.extend(copied)
        return len(copied)

    def _run_upsert(self, **kwargs: object) -> int:
        self.upserts.append(dict(kwargs))
        return int(kwargs.get("test_affected", 1))

    def _final_count(self, table_name: str) -> int:
        return self.final_counts.get(table_name, len(self.staged_rows))

    def _node_id_mapping(self) -> dict[str, int]:
        return dict(self.node_mapping)


def sensor_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [[1, "Sensor", "Description", date(2020, 1, 1), None, "Outdoor", "A", -37.81, 144.96, "POINT (144.96 -37.81)"]],
        columns=SENSOR_DIMENSION_COLUMNS,
    )


def direction_frame() -> pd.DataFrame:
    return pd.DataFrame([[1, 10, "North", "South"]], columns=SENSOR_DIRECTION_TABLE_COLUMNS)


def landmark_frame(landmark_id: str | None = None) -> pd.DataFrame:
    return pd.DataFrame(
        [[landmark_id or str(uuid4()), "Library", "community", "library", -37.81, 144.96, "POINT (144.96 -37.81)"]],
        columns=LANDMARK_DIMENSION_COLUMNS,
    )


def hourly_frame(record_id: str = "source-1", hour: int = 9) -> pd.DataFrame:
    return pd.DataFrame(
        [[record_id, 1, date(2024, 1, 1), hour, datetime(2024, 1, 1, hour), 2024, 1, 1, "Monday", False, 3, 4, 7]],
        columns=HOURLY_FACT_COLUMNS,
    )


def baseline_frame() -> pd.DataFrame:
    values: dict[str, object] = {
        "sensor_id": 1,
        "iso_weekday": 1,
        "hour": 9,
        "observation_count": 10,
        "minimum_pedestrian_count": 1,
        "maximum_pedestrian_count": 20,
        "minimum_sensing_date": date(2024, 1, 1),
        "maximum_sensing_date": date(2024, 2, 1),
    }
    return pd.DataFrame([[values.get(column, 2.5) for column in BASELINE_COLUMNS]], columns=BASELINE_COLUMNS)


def node_frame(node_id: str | None = None) -> pd.DataFrame:
    return pd.DataFrame(
        [[node_id or str(uuid4()), 1, 11, 144.96, -37.81, "POINT (144.96 -37.81)", 1, True, "source"]],
        columns=ROUTING_NODE_COLUMNS,
    )


def edge_frame(source: str, target: str, edge_id: str | None = None) -> pd.DataFrame:
    return pd.DataFrame(
        [[edge_id or str(uuid4()), 1, 11, source, target, 10.0, 10.0, 10.0, "LINESTRING (144.96 -37.81, 144.961 -37.811)", 1, True, False]],
        columns=ROUTING_EDGE_COLUMNS,
    )


def sensor_mapping_frame(node_id: str) -> pd.DataFrame:
    return pd.DataFrame([[1, node_id, 4.0, True, 1, True]], columns=SENSOR_MAPPING_COLUMNS)


def landmark_mapping_frame(node_id: str, landmark_id: str | None = None) -> pd.DataFrame:
    return pd.DataFrame([[landmark_id or str(uuid4()), node_id, 4.0, True, 1, True]], columns=LANDMARK_MAPPING_COLUMNS)


def passing_report(*, warning: bool = False) -> HistoricalValidationReport:
    issues = (
        (ValidationIssue("warning", "synthetic_warning", "warning", 1),)
        if warning
        else ()
    )
    return HistoricalValidationReport((ValidationReport("synthetic", 1, issues),))


def failing_report() -> HistoricalValidationReport:
    issue = ValidationIssue("error", "synthetic_error", "error", 1)
    return HistoricalValidationReport((ValidationReport("synthetic", 1, (issue,)),))


def test_missing_database_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(load_module.os, "environ", {})
    loader = PostgresLoader()
    with pytest.raises(DatabaseLoadError, match="configuration is missing"):
        _ = loader.connection


def test_caller_owned_connection_is_preserved() -> None:
    connection = ControlledConnection()
    loader = PostgresLoader(connection=connection)  # type: ignore[arg-type]
    loader._validated = True
    loader.close()
    assert not connection.closed
    assert not loader.owns_connection


def test_loader_owned_connection_is_closed() -> None:
    connection = ControlledConnection()
    loader = PostgresLoader(database_url="postgresql://example.invalid/db", connect=lambda *args, **kwargs: connection)  # type: ignore[arg-type]
    loader._validate_database = lambda: None  # type: ignore[method-assign]
    assert loader.connection is connection
    assert loader.owns_connection
    loader.close()
    assert connection.closed


def test_required_migration_and_extension_checks() -> None:
    connection = ControlledConnection(
        [ResultCursor([(1,), (3,)]), ResultCursor([("postgis",), ("pgrouting",)])]
    )
    loader = PostgresLoader(connection=connection)  # type: ignore[arg-type]
    with pytest.raises(DatabaseLoadError, match="migrations.*2"):
        loader._validate_database()


def test_required_extension_checks() -> None:
    connection = ControlledConnection(
        [ResultCursor([(1,), (2,), (3,)]), ResultCursor([("postgis",)])]
    )
    loader = PostgresLoader(connection=connection)  # type: ignore[arg-type]
    with pytest.raises(DatabaseLoadError, match="pgrouting"):
        loader._validate_database()


@pytest.mark.parametrize(
    ("frame", "method"),
    [
        (sensor_frame().drop(columns=["sensor_name"]), "load_sensors"),
        (direction_frame().drop(columns=["direction_1_label"]), "load_sensor_directions"),
        (landmark_frame().drop(columns=["category"]), "load_landmarks"),
        (baseline_frame().drop(columns=[BASELINE_COLUMNS[-1]]), "load_crowd_baselines"),
    ],
)
def test_required_columns_are_checked(frame: pd.DataFrame, method: str) -> None:
    loader = RecordingLoader()
    arguments = (frame, str(uuid4())) if method == "load_crowd_baselines" else (frame,)
    with pytest.raises(DatabaseLoadError, match="missing required columns"):
        getattr(loader, method)(*arguments)


def test_sensor_upsert_uses_business_key_and_postgis_constructor() -> None:
    loader = RecordingLoader()
    result = loader.load_sensors(sensor_frame())
    assert result.source_count == result.staged_count == 1
    assert loader.upserts[0]["conflict_columns"] == ("sensor_id",)
    assert "ST_GeomFromText" in repr(loader.upserts[0]["select_expressions"])


def test_sensor_direction_upsert_uses_composite_key() -> None:
    loader = RecordingLoader()
    loader.load_sensor_directions(direction_frame())
    assert loader.upserts[0]["conflict_columns"] == ("sensor_id", "direction_config_id")


def test_landmark_upsert_uses_uuid_and_postgis_constructor() -> None:
    loader = RecordingLoader()
    loader.load_landmarks(landmark_frame())
    assert loader.upserts[0]["conflict_columns"] == ("landmark_id",)
    assert "4326" in repr(loader.upserts[0]["select_expressions"])


def test_invalid_uuid_and_missing_geometry_are_rejected() -> None:
    loader = RecordingLoader()
    with pytest.raises(DatabaseLoadError, match="invalid UUID"):
        loader.load_landmarks(landmark_frame("not-a-uuid"))
    frame = sensor_frame()
    frame.loc[0, "geometry_wkt"] = None
    with pytest.raises(DatabaseLoadError, match="geometry WKT"):
        loader.load_sensors(frame)


def test_hourly_chunks_are_streamed_without_concat(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        ResultCursor([(2,)]),
        ResultCursor(),
        ResultCursor(),
        ResultCursor(),
        ResultCursor(),
    ]
    loader = RecordingLoader(responses=responses)
    monkeypatch.setattr(pd, "concat", lambda *args, **kwargs: pytest.fail("concat must not be used"))
    result = loader.load_hourly_chunks(iter([hourly_frame("a", 9), hourly_frame("b", 10)]), str(uuid4()))
    assert result.source_count == result.staged_count == 2
    assert result.warnings == ("consumed 2 chunk(s)",)
    assert loader.upserts[0]["conflict_columns"] == ("sensor_id", "sensing_date", "hour")


def test_hourly_dataframe_and_empty_iterable_are_rejected() -> None:
    loader = RecordingLoader()
    with pytest.raises(TypeError, match="chunk iterable"):
        loader.load_hourly_chunks(hourly_frame(), str(uuid4()))
    with pytest.raises(DatabaseLoadError, match="no chunks"):
        loader.load_hourly_chunks(iter(()), str(uuid4()))


def test_crowd_baseline_upsert_uses_business_key() -> None:
    loader = RecordingLoader()
    loader.load_crowd_baselines(baseline_frame(), str(uuid4()))
    assert loader.upserts[0]["conflict_columns"] == ("sensor_id", "iso_weekday", "hour")


def test_routing_node_uuid_maps_to_database_bigint() -> None:
    node_uuid = str(uuid4())
    loader = RecordingLoader()
    loader.node_mapping = {node_uuid: 41}
    result, mapping = loader.load_routing_nodes(node_frame(node_uuid))
    assert result.source_count == 1
    assert mapping == {node_uuid: 41}
    assert loader.upserts[0]["conflict_columns"] == ("node_uuid",)


def test_integer_semantic_spatial_identifiers_are_normalised_for_copy() -> None:
    node_uuid = str(uuid4())
    frame = node_frame(node_uuid)
    frame.loc[0, "source_object_id"] = 3947.0
    frame.loc[0, "source_network_id"] = 11.0
    frame.loc[0, "component_id"] = 1.0
    loader = RecordingLoader()
    loader.node_mapping = {node_uuid: 41}
    loader.load_routing_nodes(frame)
    assert loader.staged_rows[0][1:3] == (3947, 11)
    assert loader.staged_rows[0][6] == 1


def test_fractional_spatial_identifier_is_rejected() -> None:
    node_uuid = str(uuid4())
    frame = node_frame(node_uuid)
    frame["source_object_id"] = frame["source_object_id"].astype(float)
    frame.loc[0, "source_object_id"] = 3947.5
    loader = RecordingLoader()
    loader.node_mapping = {node_uuid: 41}
    with pytest.raises(DatabaseLoadError, match="source_object_id must be an integer"):
        loader.load_routing_nodes(frame)


def test_routing_edges_resolve_uuid_endpoints() -> None:
    source, target = str(uuid4()), str(uuid4())
    loader = RecordingLoader()
    loader.load_routing_edges(edge_frame(source, target), {source: 101, target: 202})
    copied = loader.staged_rows[0]
    assert copied[3:5] == (101, 202)
    assert loader.upserts[0]["conflict_columns"] == ("edge_uuid",)


def test_missing_routing_node_uuid_fails_before_staging() -> None:
    source, target = str(uuid4()), str(uuid4())
    loader = RecordingLoader()
    with pytest.raises(DatabaseLoadError, match="unresolved node UUID"):
        loader.load_routing_edges(edge_frame(source, target), {source: 1})
    assert loader.staged_rows == []


def test_sensor_and_landmark_mappings_resolve_node_uuid() -> None:
    node_uuid = str(uuid4())
    loader = RecordingLoader()
    sensor_result, landmark_result = loader.load_network_mappings(
        sensor_mapping_frame(node_uuid), landmark_mapping_frame(node_uuid), {node_uuid: 77}
    )
    assert sensor_result.table_name == "sensor_network_map"
    assert landmark_result.table_name == "landmark_network_map"
    assert loader.staged_rows[0][1] == 77
    assert loader.staged_rows[1][1] == 77


class WorkflowLoader(PostgresLoader):
    def __init__(self, *, dry_run: bool = False, fail: bool = False):
        super().__init__(connection=ControlledConnection(), config=DatabaseLoaderConfig(dry_run=dry_run))  # type: ignore[arg-type]
        self._validated = True
        self.fail = fail
        self.finished = False
        self.failed = False
        self.deleted = False

    def _start_pipeline_run(self) -> str:
        return "00000000-0000-0000-0000-000000000001"

    def load_sensors(self, frame: pd.DataFrame) -> TableLoadResult:
        if self.fail:
            raise RuntimeError("synthetic failure")
        return TableLoadResult("sensors", 1, 1, 1, 1)

    def load_sensor_directions(self, frame: pd.DataFrame) -> TableLoadResult:
        return TableLoadResult("sensor_directions", 1, 1, 1, 1)

    def load_landmarks(self, frame: pd.DataFrame) -> TableLoadResult:
        return TableLoadResult("landmarks", 1, 1, 1, 1)

    def load_hourly_chunks(self, chunks: object, pipeline_run_id: str) -> TableLoadResult:
        list(chunks)  # type: ignore[arg-type]
        return TableLoadResult("pedestrian_counts_hourly", 1, 1, 1, 1)

    def load_crowd_baselines(self, frame: pd.DataFrame, pipeline_run_id: str) -> TableLoadResult:
        return TableLoadResult("crowd_baselines", 1, 1, 1, 1)

    def load_spatial_network(self, network: object) -> tuple[TableLoadResult, TableLoadResult, dict[str, int]]:
        return TableLoadResult("routing_nodes", 1, 1, 1, 1), TableLoadResult("routing_edges", 1, 1, 1, 1), {"n": 1}

    def load_network_mappings(self, *args: object) -> tuple[TableLoadResult, TableLoadResult]:
        return TableLoadResult("sensor_network_map", 1, 1, 1, 1), TableLoadResult("landmark_network_map", 1, 1, 1, 1)

    def _verify_relationships(self) -> None:
        return None

    def _finish_pipeline_run(self, run_id: str, results: object) -> None:
        self.finished = True

    def _fail_pipeline_run(self, run_id: str, error: BaseException) -> None:
        self.failed = True

    def _delete_dry_run(self, run_id: str) -> None:
        self.deleted = True


def workflow_arguments(report: HistoricalValidationReport) -> dict[str, object]:
    return {
        "sensors": sensor_frame(),
        "sensor_directions": direction_frame(),
        "landmarks": landmark_frame(),
        "hourly_chunk_factory": lambda: iter([hourly_frame()]),
        "crowd_baselines": baseline_frame(),
        "spatial_network": object(),
        "sensor_mapping": sensor_mapping_frame(str(uuid4())),
        "landmark_mapping": landmark_mapping_frame(str(uuid4())),
        "validation_report": report,
    }


def test_failed_validation_report_blocks_before_pipeline_run() -> None:
    loader = WorkflowLoader()
    with pytest.raises(DatabaseLoadError, match="validation report has errors"):
        loader.load_historical_dataset(**workflow_arguments(failing_report()))
    assert not loader.finished
    assert not loader.failed


def test_validation_warning_is_allowed_and_visible() -> None:
    loader = WorkflowLoader()
    result = loader.load_historical_dataset(**workflow_arguments(passing_report(warning=True)))
    assert result.status == "succeeded"
    assert result.warnings == ("historical validation contains 1 warning(s)",)
    assert loader.finished


def test_failure_is_wrapped_and_failed_run_is_recorded() -> None:
    loader = WorkflowLoader(fail=True)
    with pytest.raises(DatabaseLoadError, match="historical load failed") as captured:
        loader.load_historical_dataset(**workflow_arguments(passing_report()))
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert loader.failed


def test_dry_run_rolls_back_and_removes_pipeline_record() -> None:
    loader = WorkflowLoader(dry_run=True)
    result = loader.load_historical_dataset(**workflow_arguments(passing_report()))
    assert result.status == "dry_run"
    assert result.dry_run
    assert loader.deleted


def test_results_are_json_serialisable() -> None:
    table = TableLoadResult("sensors", 1, 1, 1, 1, ("warning",))
    result = HistoricalLoadResult("run-id", "succeeded", (table,), 0.1, ("warning",))
    encoded = json.dumps(result.to_dict(), sort_keys=True)
    assert '"table_name": "sensors"' in encoded
    assert result.total_source_rows == 1


def test_loader_source_has_no_data_file_writes_or_hard_coded_local_paths() -> None:
    source = Path(load_module.__file__).read_text(encoding="utf-8")
    assert "/Users/" not in source
    assert "/workspace/" not in source
    assert "to_csv(" not in source
    assert "to_parquet(" not in source
    assert "pd.concat(" not in source
    assert "COPY {}" in source
    assert "ST_GeomFromText" in source
