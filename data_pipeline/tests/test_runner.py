"""Focused tests for the historical end-to-end runner."""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

import cityflow_pipeline.runner as runner
from cityflow_pipeline.baseline import BASELINE_COLUMNS, HistoricalFeatureWorkflow
from cityflow_pipeline.clean import SensorCleaningResult
from cityflow_pipeline.extract import HISTORICAL_SOURCE_NAMES, HistoricalExtraction
from cityflow_pipeline.load import (
    DatabaseLoaderConfig,
    HistoricalLoadResult,
    TableLoadResult,
)
from cityflow_pipeline.runner import (
    EXCLUDED_HISTORICAL_SOURCES,
    PIPELINE_STAGE_ORDER,
    HistoricalPipelineConfig,
    HistoricalPipelineResult,
    PipelineExecutionError,
    PipelineStageResult,
    run_historical_pipeline,
)
from cityflow_pipeline.spatial import SpatialProcessingConfig
from cityflow_pipeline.transform import (
    HOURLY_FACT_COLUMNS,
    HistoricalTransformation,
)
from cityflow_pipeline.validate import (
    HistoricalValidationReport,
    ValidationIssue,
    ValidationReport,
)


@pytest.fixture
def raw_data_dir(tmp_path: Path) -> Path:
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    for name in HISTORICAL_SOURCE_NAMES:
        (raw / name).write_text("column\nvalue\n", encoding="utf-8")
    return raw


def file_hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def hourly_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [["source-1", 1, date(2024, 1, 1), 9, datetime(2024, 1, 1, 9), 2024, 1, 1, "Monday", False, 3, 4, 7]],
        columns=HOURLY_FACT_COLUMNS,
    )


def baseline_frame() -> pd.DataFrame:
    values: dict[str, object] = {
        "sensor_id": 1,
        "iso_weekday": 1,
        "hour": 9,
        "observation_count": 1,
        "minimum_sensing_date": date(2024, 1, 1),
        "maximum_sensing_date": date(2024, 1, 1),
        "minimum_pedestrian_count": 7,
        "maximum_pedestrian_count": 7,
    }
    return pd.DataFrame(
        [[values.get(column, 7.0) for column in BASELINE_COLUMNS]],
        columns=BASELINE_COLUMNS,
    )


def validation_report(
    *, warning: bool = False, failure: bool = False
) -> HistoricalValidationReport:
    issues: tuple[ValidationIssue, ...] = ()
    if warning:
        issues = (
            ValidationIssue("warning", "synthetic_warning", "warning retained", 1),
        )
    if failure:
        issues = (
            ValidationIssue("error", "synthetic_error", "blocking error", 1),
        )
    return HistoricalValidationReport(
        (ValidationReport("synthetic_hourly", 1, issues),)
    )


class SuccessfulFakes:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        report: HistoricalValidationReport | None = None,
        loader_error: BaseException | None = None,
    ) -> None:
        self.events: list[str] = []
        self.hourly_iterator_count = 0
        self.loader_error = loader_error
        self.report = report or validation_report()
        self.connection_received: object | None = None
        self.loader_config_received: DatabaseLoaderConfig | None = None
        self.network_source = pd.DataFrame({"network": [1]})
        self.sensor_source = pd.DataFrame({"sensor": [1]})
        self.landmark_source = pd.DataFrame({"landmark": [1]})
        self.canonical = pd.DataFrame({"sensor_id": [1]})
        self.directions = pd.DataFrame(
            {
                "sensor_id": [1],
                "direction_config_id": [1],
                "direction_1_label": ["North"],
                "direction_2_label": ["South"],
            }
        )
        self.landmarks = pd.DataFrame({"landmark_id": ["landmark-1"]})
        self.transformed_hourly = hourly_frame()
        self.baseline = baseline_frame()
        self.spatial = SimpleNamespace(
            network=object(),
            sensor_mapping=object(),
            landmark_mapping=object(),
        )

        monkeypatch.setattr(runner, "extract_historical_sources", self.extract)
        monkeypatch.setattr(
            runner, "extract_pedestrian_counts_hourly", self.extract_hourly
        )
        monkeypatch.setattr(runner, "clean_pedestrian_sensors", self.clean_sensors)
        monkeypatch.setattr(runner, "clean_landmarks", self.clean_landmarks)
        monkeypatch.setattr(
            runner, "clean_pedestrian_counts_hourly", lambda frame: frame.copy()
        )
        monkeypatch.setattr(runner, "validate_historical_workflow", self.validate)
        monkeypatch.setattr(
            runner, "transform_historical_workflow", self.transform
        )
        monkeypatch.setattr(runner, "transform_hourly_chunk", self.transform_hourly)
        monkeypatch.setattr(
            runner, "engineer_historical_features", self.engineer_features
        )
        monkeypatch.setattr(
            runner,
            "add_crowd_features",
            lambda frame, baseline, config: frame.assign(crowd_level="normal"),
        )
        monkeypatch.setattr(runner, "process_spatial_workflow", self.process_spatial)
        monkeypatch.setattr(
            runner,
            "_spatial_summary",
            lambda value: {
                "routing_nodes": 2,
                "routing_edges": 1,
                "sensor_mappings": 1,
                "landmark_mappings": 1,
            },
        )
        monkeypatch.setattr(runner, "PostgresLoader", self.loader_class())

    def extract(self, raw_data_dir: Path, *, hourly_chunk_size: int) -> HistoricalExtraction:
        self.events.append("extraction")
        return HistoricalExtraction(
            self.sensor_source.copy(),
            iter(()),
            self.landmark_source.copy(),
            self.network_source.copy(),
        )

    def extract_hourly(self, raw_data_dir: Path, *, chunk_size: int):
        self.hourly_iterator_count += 1
        return iter([pd.DataFrame({"raw_hourly": [self.hourly_iterator_count]})])

    def clean_sensors(self, frame: pd.DataFrame) -> SensorCleaningResult:
        self.events.append("cleaning")
        return SensorCleaningResult(self.canonical.copy(), self.directions.copy())

    def clean_landmarks(self, frame: pd.DataFrame) -> pd.DataFrame:
        return self.landmarks.copy()

    def validate(self, sensors: pd.DataFrame, directions: pd.DataFrame, chunks: Any, landmarks: pd.DataFrame) -> HistoricalValidationReport:
        self.events.append("validation")
        assert len(list(chunks)) == 1
        return self.report

    def transform(self, sensors: pd.DataFrame, directions: pd.DataFrame, chunks: Any, landmarks: pd.DataFrame, report: HistoricalValidationReport) -> HistoricalTransformation:
        self.events.append("transformation")
        return HistoricalTransformation(
            sensors.copy(), directions.copy(), iter(()), landmarks.copy(), report
        )

    def transform_hourly(self, frame: pd.DataFrame, sensors: pd.DataFrame) -> pd.DataFrame:
        return self.transformed_hourly.copy()

    def engineer_features(self, factory: Any, config: object) -> HistoricalFeatureWorkflow:
        self.events.append("baseline_and_features")
        chunks = list(factory())
        assert len(chunks) == 1
        return HistoricalFeatureWorkflow(
            self.baseline.copy(), iter(()), len(chunks[0]), len(chunks), 128
        )

    def process_spatial(self, network: pd.DataFrame, sensors: pd.DataFrame, landmarks: pd.DataFrame, config: SpatialProcessingConfig):
        self.events.append("spatial_processing")
        assert network.equals(self.network_source)
        return self.spatial

    def loader_class(self):
        owner = self

        class FakeLoader:
            def __init__(self, *, connection: object, config: DatabaseLoaderConfig):
                owner.connection_received = connection
                owner.loader_config_received = config
                self.config = config

            def __enter__(self):
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def load_historical_dataset(self, **dataset: object) -> HistoricalLoadResult:
                owner.events.append("database_loading")
                if owner.loader_error is not None:
                    raise owner.loader_error
                chunks = list(dataset["hourly_chunk_factory"]())  # type: ignore[operator]
                assert len(chunks) == 1
                dry_run = self.config.dry_run
                return HistoricalLoadResult(
                    "temporary-run" if dry_run else "persisted-run",
                    "dry_run" if dry_run else "succeeded",
                    (
                        TableLoadResult("sensors", 1, 1, 1, 1),
                        TableLoadResult("pedestrian_counts_hourly", 1, 1, 1, 1),
                    ),
                    0.01,
                    ("loader warning",),
                    dry_run,
                )

        return FakeLoader


def test_successful_orchestration_stage_order_and_fresh_hourly_iterators(
    raw_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = SuccessfulFakes(monkeypatch)
    result = run_historical_pipeline(HistoricalPipelineConfig(raw_data_dir))
    assert result.status == "succeeded"
    assert result.completed_stages == PIPELINE_STAGE_ORDER
    assert fakes.events == [
        "extraction",
        "cleaning",
        "validation",
        "transformation",
        "baseline_and_features",
        "spatial_processing",
        "database_loading",
    ]
    assert fakes.hourly_iterator_count == 3
    assert [item["pass"] for item in result.hourly_passes] == [
        "validation",
        "baseline",
        "feature_enrichment_1",
    ]
    assert result.pipeline_run_id == "persisted-run"
    assert result.rows_processed == 2


@pytest.mark.parametrize("chunk_size", [0, -1, True, 1.5])
def test_configuration_rejects_invalid_chunk_size(
    raw_data_dir: Path, chunk_size: object
) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        HistoricalPipelineConfig(raw_data_dir, hourly_chunk_size=chunk_size)  # type: ignore[arg-type]


def test_configuration_validates_nested_types(raw_data_dir: Path) -> None:
    with pytest.raises(ValueError, match="raw_data_dir"):
        HistoricalPipelineConfig("   ")
    with pytest.raises(TypeError, match="spatial_config"):
        HistoricalPipelineConfig(raw_data_dir, spatial_config=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="loader_config"):
        HistoricalPipelineConfig(raw_data_dir, loader_config=object())  # type: ignore[arg-type]


def test_missing_raw_directory_is_stage_specific(tmp_path: Path) -> None:
    with pytest.raises(PipelineExecutionError) as captured:
        run_historical_pipeline(HistoricalPipelineConfig(tmp_path / "missing"))
    assert captured.value.stage == "preflight"
    assert isinstance(captured.value.__cause__, FileNotFoundError)


def test_missing_historical_source_file_is_reported(raw_data_dir: Path) -> None:
    (raw_data_dir / HISTORICAL_SOURCE_NAMES[-1]).unlink()
    with pytest.raises(PipelineExecutionError) as captured:
        run_historical_pipeline(HistoricalPipelineConfig(raw_data_dir))
    assert captured.value.stage == "preflight"
    assert HISTORICAL_SOURCE_NAMES[-1] in str(captured.value)


def test_blocking_validation_stops_before_transformation_and_loading(
    raw_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = SuccessfulFakes(monkeypatch, report=validation_report(failure=True))
    with pytest.raises(PipelineExecutionError) as captured:
        run_historical_pipeline(HistoricalPipelineConfig(raw_data_dir))
    assert captured.value.stage == "validation"
    assert captured.value.result.validation_summary["error_count"] == 1
    assert "transformation" not in fakes.events
    assert "database_loading" not in fakes.events


def test_validation_and_loader_warnings_are_propagated(
    raw_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    SuccessfulFakes(monkeypatch, report=validation_report(warning=True))
    result = run_historical_pipeline(HistoricalPipelineConfig(raw_data_dir))
    assert result.validation_summary["warning_count"] == 1
    assert any("synthetic_warning" in item for item in result.warnings)
    assert "loader warning" in result.warnings


def test_stage_specific_error_preserves_original_cause(
    raw_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = SuccessfulFakes(monkeypatch)
    original = RuntimeError("synthetic transform failure")

    def fail(*args: object, **kwargs: object) -> None:
        raise original

    monkeypatch.setattr(runner, "transform_historical_workflow", fail)
    with pytest.raises(PipelineExecutionError) as captured:
        run_historical_pipeline(HistoricalPipelineConfig(raw_data_dir))
    assert captured.value.stage == "transformation"
    assert captured.value.__cause__ is original
    assert captured.value.result.failed_stage == "transformation"
    assert "database_loading" not in fakes.events


def test_loader_failure_is_wrapped_without_credential_leakage(
    raw_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "super-secret"
    error = RuntimeError(
        f"connection failed postgresql://cityflow:{secret}@db.example/cityflow password={secret}"
    )
    SuccessfulFakes(monkeypatch, loader_error=error)
    with pytest.raises(PipelineExecutionError) as captured:
        run_historical_pipeline(HistoricalPipelineConfig(raw_data_dir))
    assert captured.value.stage == "database_loading"
    assert captured.value.__cause__ is error
    encoded = json.dumps(captured.value.result.to_dict())
    assert secret not in str(captured.value)
    assert secret not in encoded
    assert "redacted" in encoded


def test_dry_run_and_caller_owned_connection_are_propagated(
    raw_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = SuccessfulFakes(monkeypatch)
    connection = object()
    result = run_historical_pipeline(
        HistoricalPipelineConfig(raw_data_dir, dry_run=True),
        connection=connection,  # type: ignore[arg-type]
    )
    assert result.status == "dry_run"
    assert result.dry_run
    assert result.pipeline_run_id is None
    assert fakes.connection_received is connection
    assert fakes.loader_config_received is not None
    assert fakes.loader_config_received.dry_run


def test_result_is_json_serialisable(
    raw_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    SuccessfulFakes(monkeypatch)
    result = run_historical_pipeline(HistoricalPipelineConfig(raw_data_dir))
    decoded = json.loads(json.dumps(result.to_dict(), allow_nan=False))
    assert decoded["status"] == "succeeded"
    assert decoded["completed_stages"] == list(PIPELINE_STAGE_ORDER)


def test_raw_files_are_immutable_and_no_output_directories_are_created(
    raw_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = SuccessfulFakes(monkeypatch)
    before = file_hashes(raw_data_dir)
    source_snapshots = (
        fakes.sensor_source.copy(deep=True),
        fakes.landmark_source.copy(deep=True),
        fakes.network_source.copy(deep=True),
    )
    interim = raw_data_dir.parent / "interim"
    processed = raw_data_dir.parent / "processed"
    run_historical_pipeline(HistoricalPipelineConfig(raw_data_dir))
    assert file_hashes(raw_data_dir) == before
    assert fakes.sensor_source.equals(source_snapshots[0])
    assert fakes.landmark_source.equals(source_snapshots[1])
    assert fakes.network_source.equals(source_snapshots[2])
    assert not interim.exists()
    assert not processed.exists()


def test_minutely_is_excluded_and_pedestrian_network_is_included(
    raw_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (raw_data_dir / "pedestrian_counts_minutely.csv").write_text(
        "live\nnot-read\n", encoding="utf-8"
    )
    fakes = SuccessfulFakes(monkeypatch)
    result = run_historical_pipeline(HistoricalPipelineConfig(raw_data_dir))
    assert result.excluded_sources == EXCLUDED_HISTORICAL_SOURCES
    assert "pedestrian_counts_minutely.csv" in result.excluded_sources
    assert "spatial_processing" in fakes.events


def test_runner_never_concatenates_hourly_chunks(
    raw_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    SuccessfulFakes(monkeypatch)
    monkeypatch.setattr(
        pd,
        "concat",
        lambda *args, **kwargs: pytest.fail("runner must not call pd.concat"),
    )
    result = run_historical_pipeline(HistoricalPipelineConfig(raw_data_dir))
    assert result.status == "succeeded"


def test_runner_source_has_no_hard_coded_paths_credentials_or_data_writes() -> None:
    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert "/Users/" not in source
    assert "/workspace/" not in source
    assert "to_csv(" not in source
    assert "to_parquet(" not in source
    assert "pd.concat(" not in source
    assert "boto3" not in source


def test_cli_argument_parsing() -> None:
    args = runner._build_parser().parse_args(
        [
            "--raw-data-dir",
            "data_pipeline/data/raw",
            "--hourly-chunk-size",
            "50000",
            "--dry-run",
            "--json",
        ]
    )
    assert args.raw_data_dir == Path("data_pipeline/data/raw")
    assert args.hourly_chunk_size == 50_000
    assert args.dry_run
    assert args.json_output


def test_cli_json_output(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    result = HistoricalPipelineResult(
        status="succeeded",
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:01+00:00",
        elapsed_seconds=1.0,
        dry_run=False,
        hourly_chunk_size=100_000,
        stages=(
            PipelineStageResult(
                "preflight",
                "succeeded",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:01+00:00",
                1.0,
            ),
        ),
    )
    monkeypatch.setattr(runner, "run_historical_pipeline", lambda config: result)
    assert runner.main(["--raw-data-dir", "raw", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"
