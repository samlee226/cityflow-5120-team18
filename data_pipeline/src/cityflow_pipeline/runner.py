"""Reproducible historical V1 orchestration for the CityFlow pipeline."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Final, TypeVar

import numpy as np
import pandas as pd
from psycopg import Connection

from cityflow_pipeline.baseline import (
    CrowdFeatureConfig,
    HistoricalFeatureWorkflow,
    add_crowd_features,
    engineer_historical_features,
)
from cityflow_pipeline.clean import (
    SensorCleaningResult,
    clean_landmarks,
    clean_pedestrian_counts_hourly,
    clean_pedestrian_sensors,
)
from cityflow_pipeline.extract import (
    DEFAULT_HOURLY_CHUNK_SIZE,
    HISTORICAL_SOURCE_NAMES,
    LIVE_SOURCE_NAMES,
    HistoricalExtraction,
    extract_historical_sources,
    extract_pedestrian_counts_hourly,
)
from cityflow_pipeline.load import (
    DatabaseLoaderConfig,
    HistoricalLoadResult,
    PostgresLoader,
)
from cityflow_pipeline.spatial import (
    SpatialProcessingConfig,
    SpatialWorkflowResult,
    process_spatial_workflow,
)
from cityflow_pipeline.transform import (
    HistoricalTransformation,
    transform_historical_workflow,
    transform_hourly_chunk,
)
from cityflow_pipeline.validate import (
    HistoricalValidationReport,
    validate_historical_workflow,
)


PIPELINE_STAGE_ORDER: Final = (
    "preflight",
    "extraction",
    "cleaning",
    "validation",
    "transformation",
    "baseline_and_features",
    "spatial_processing",
    "database_loading",
)
EXCLUDED_HISTORICAL_SOURCES: Final = LIVE_SOURCE_NAMES

_T = TypeVar("_T")


class PipelineExecutionError(RuntimeError):
    """Raised when one named pipeline stage fails.

    The structured partial result remains available through ``result``. The
    original exception is retained as the raised exception's ``__cause__``.
    """

    def __init__(
        self,
        stage: str,
        result: HistoricalPipelineResult,
        original_error: BaseException,
    ) -> None:
        self.stage = stage
        self.result = result
        safe = _safe_error_message(original_error)
        super().__init__(f"historical pipeline failed during {stage}: {safe}")


@dataclass(frozen=True, slots=True)
class HistoricalPipelineConfig:
    """Configuration for one historical V1 execution."""

    raw_data_dir: Path | str
    hourly_chunk_size: int = DEFAULT_HOURLY_CHUNK_SIZE
    spatial_config: SpatialProcessingConfig = field(
        default_factory=SpatialProcessingConfig
    )
    crowd_config: CrowdFeatureConfig = field(default_factory=CrowdFeatureConfig)
    loader_config: DatabaseLoaderConfig = field(
        default_factory=DatabaseLoaderConfig
    )
    dry_run: bool = False
    json_output: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.raw_data_dir, str) and not self.raw_data_dir.strip():
            raise ValueError("raw_data_dir must not be blank")
        raw_data_dir = Path(self.raw_data_dir).expanduser()
        if not str(raw_data_dir):
            raise ValueError("raw_data_dir must not be blank")
        if (
            isinstance(self.hourly_chunk_size, bool)
            or not isinstance(self.hourly_chunk_size, int)
            or self.hourly_chunk_size <= 0
        ):
            raise ValueError("hourly_chunk_size must be a positive integer")
        if not isinstance(self.spatial_config, SpatialProcessingConfig):
            raise TypeError("spatial_config must be a SpatialProcessingConfig")
        if not isinstance(self.crowd_config, CrowdFeatureConfig):
            raise TypeError("crowd_config must be a CrowdFeatureConfig")
        if not isinstance(self.loader_config, DatabaseLoaderConfig):
            raise TypeError("loader_config must be a DatabaseLoaderConfig")
        if not isinstance(self.dry_run, bool):
            raise TypeError("dry_run must be a boolean")
        if not isinstance(self.json_output, bool):
            raise TypeError("json_output must be a boolean")
        effective_dry_run = self.dry_run or self.loader_config.dry_run
        object.__setattr__(self, "raw_data_dir", raw_data_dir)
        object.__setattr__(self, "dry_run", effective_dry_run)
        if self.loader_config.dry_run != effective_dry_run:
            object.__setattr__(
                self,
                "loader_config",
                replace(self.loader_config, dry_run=effective_dry_run),
            )


@dataclass(frozen=True, slots=True)
class PipelineStageResult:
    """Timing and bounded details for one named pipeline stage."""

    name: str
    status: str
    started_at: str
    completed_at: str
    elapsed_seconds: float
    details: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable representation."""

        return {
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_seconds": self.elapsed_seconds,
            "details": _json_value(self.details),
        }


@dataclass(frozen=True, slots=True)
class HistoricalPipelineResult:
    """Structured final or partial result for a historical pipeline run."""

    status: str
    started_at: str
    completed_at: str
    elapsed_seconds: float
    dry_run: bool
    hourly_chunk_size: int
    stages: tuple[PipelineStageResult, ...]
    validation_summary: Mapping[str, object] = field(default_factory=dict)
    rows_processed: int = 0
    table_results: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    spatial_summary: Mapping[str, object] = field(default_factory=dict)
    baseline_summary: Mapping[str, object] = field(default_factory=dict)
    hourly_passes: tuple[Mapping[str, object], ...] = ()
    pipeline_run_id: str | None = None
    warnings: tuple[str, ...] = ()
    excluded_sources: tuple[str, ...] = EXCLUDED_HISTORICAL_SOURCES
    failed_stage: str | None = None
    error_type: str | None = None
    error_message: str | None = None

    @property
    def completed_stages(self) -> tuple[str, ...]:
        """Return successful stage names in execution order."""

        return tuple(stage.name for stage in self.stages if stage.status == "succeeded")

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-serialisable representation."""

        return {
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_seconds": self.elapsed_seconds,
            "dry_run": self.dry_run,
            "hourly_chunk_size": self.hourly_chunk_size,
            "completed_stages": list(self.completed_stages),
            "failed_stage": self.failed_stage,
            "stages": [stage.to_dict() for stage in self.stages],
            "validation_summary": _json_value(self.validation_summary),
            "rows_processed": self.rows_processed,
            "table_results": _json_value(self.table_results),
            "spatial_summary": _json_value(self.spatial_summary),
            "baseline_summary": _json_value(self.baseline_summary),
            "hourly_passes": _json_value(self.hourly_passes),
            "pipeline_run_id": self.pipeline_run_id,
            "warnings": list(self.warnings),
            "excluded_sources": list(self.excluded_sources),
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


@dataclass(slots=True)
class _PipelineState:
    validation_summary: Mapping[str, object] = field(default_factory=dict)
    table_results: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    spatial_summary: Mapping[str, object] = field(default_factory=dict)
    baseline_summary: Mapping[str, object] = field(default_factory=dict)
    hourly_passes: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rows_processed: int = 0
    pipeline_run_id: str | None = None


class _StageFailure(Exception):
    def __init__(self, stage: str, error: BaseException) -> None:
        self.stage = stage
        self.error = error
        super().__init__(stage)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe_error_message(error: BaseException, maximum_length: int = 1_000) -> str:
    value = str(error)
    value = re.sub(
        r"(?i)postgres(?:ql)?://[^\s]+",
        "<redacted-database-url>",
        value,
    )
    value = re.sub(
        r"(?i)\b(password|token|secret)\s*[=:]\s*[^\s,;]+",
        r"\1=<redacted>",
        value,
    )
    value = " ".join(value.split()) or error.__class__.__name__
    return value[:maximum_length]


def _json_value(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, np.generic):
        return _json_value(value.item())
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return str(value)


def _validation_warnings(report: HistoricalValidationReport) -> tuple[str, ...]:
    return tuple(
        f"{dataset.dataset_name}:{issue.code}: {issue.message}"
        for dataset in report.reports
        for issue in dataset.issues
        if issue.severity == "warning"
    )


def _validation_summary(report: HistoricalValidationReport) -> dict[str, object]:
    return {
        "passed": report.passed,
        "error_count": report.error_count,
        "warning_count": report.warning_count,
        "checked_rows": sum(item.checked_row_count for item in report.reports),
        "reports": [item.to_dict() for item in report.reports],
        "excluded_datasets": list(report.excluded_datasets),
    }


def _spatial_summary(workflow: SpatialWorkflowResult) -> dict[str, object]:
    network = workflow.network.report
    sensors = workflow.sensor_mapping.report
    landmarks = workflow.landmark_mapping.report
    return {
        "routing_nodes": len(workflow.network.nodes),
        "routing_edges": len(workflow.network.edges),
        "connected_components": network.connected_component_count,
        "largest_component_node_percentage": network.largest_component_node_percentage,
        "network_issue_count": len(network.issues),
        "sensor_mappings": len(workflow.sensor_mapping.mappings),
        "sensor_mappings_outside_threshold": sensors.outside_threshold_count,
        "landmark_mappings": len(workflow.landmark_mapping.mappings),
        "landmark_mappings_outside_threshold": landmarks.outside_threshold_count,
        "geographic_crs": workflow.network.geographic_crs,
        "projected_crs": workflow.network.projected_crs,
    }


def _baseline_summary(workflow: HistoricalFeatureWorkflow) -> dict[str, object]:
    return {
        "baseline_rows": len(workflow.baseline),
        "accumulator_rows": workflow.accumulator_rows,
        "accumulator_chunks": workflow.accumulator_chunks,
        "accumulator_retained_bytes": workflow.accumulator_retained_bytes,
        "excluded_datasets": list(workflow.excluded_datasets),
    }


def _load_table_results(result: HistoricalLoadResult) -> dict[str, Mapping[str, object]]:
    return {
        table.table_name: table.to_dict()
        for table in result.table_results
    }


def _preflight(config: HistoricalPipelineConfig) -> dict[str, object]:
    raw_data_dir = config.raw_data_dir
    assert isinstance(raw_data_dir, Path)
    if not raw_data_dir.is_dir():
        raise FileNotFoundError(f"raw-data directory does not exist: {raw_data_dir}")
    missing = tuple(
        name for name in HISTORICAL_SOURCE_NAMES if not (raw_data_dir / name).is_file()
    )
    if missing:
        raise FileNotFoundError(
            "required historical source files are missing: " + ", ".join(missing)
        )
    return {
        "raw_data_dir": str(raw_data_dir),
        "required_sources": list(HISTORICAL_SOURCE_NAMES),
        "excluded_live_sources": list(EXCLUDED_HISTORICAL_SOURCES),
    }


def run_historical_pipeline(
    config: HistoricalPipelineConfig,
    *,
    connection: Connection[Any] | None = None,
) -> HistoricalPipelineResult:
    """Execute the complete historical V1 workflow without writing data files."""

    if not isinstance(config, HistoricalPipelineConfig):
        raise TypeError("config must be a HistoricalPipelineConfig")

    pipeline_started_at = _utc_now()
    pipeline_started = time.perf_counter()
    stages: list[PipelineStageResult] = []
    state = _PipelineState()

    def stage(
        name: str,
        action: Callable[[], _T],
        describe: Callable[[_T], Mapping[str, object]],
    ) -> _T:
        stage_started_at = _utc_now()
        stage_started = time.perf_counter()
        try:
            value = action()
            details = describe(value)
        except Exception as error:
            stages.append(
                PipelineStageResult(
                    name=name,
                    status="failed",
                    started_at=stage_started_at,
                    completed_at=_utc_now(),
                    elapsed_seconds=time.perf_counter() - stage_started,
                    details={
                        "error_type": error.__class__.__name__,
                        "error_message": _safe_error_message(error),
                    },
                )
            )
            raise _StageFailure(name, error) from error
        stages.append(
            PipelineStageResult(
                name=name,
                status="succeeded",
                started_at=stage_started_at,
                completed_at=_utc_now(),
                elapsed_seconds=time.perf_counter() - stage_started,
                details=details,
            )
        )
        return value

    try:
        stage("preflight", lambda: _preflight(config), lambda value: value)

        extraction = stage(
            "extraction",
            lambda: extract_historical_sources(
                config.raw_data_dir,
                hourly_chunk_size=config.hourly_chunk_size,
            ),
            lambda value: {
                "sensor_source_rows": len(value.pedestrian_sensors),
                "landmark_source_rows": len(value.landmarks),
                "network_source_rows": len(value.pedestrian_network),
                "hourly_mode": "lazy_chunk_iterator",
            },
        )
        assert isinstance(extraction, HistoricalExtraction)

        def clean_references() -> tuple[SensorCleaningResult, pd.DataFrame]:
            return (
                clean_pedestrian_sensors(extraction.pedestrian_sensors),
                clean_landmarks(extraction.landmarks),
            )

        cleaned_sensors, cleaned_landmarks = stage(
            "cleaning",
            clean_references,
            lambda value: {
                "canonical_sensors": len(value[0].canonical_sensors),
                "sensor_directions": len(value[0].sensor_directions),
                "landmarks": len(value[1]),
                "hourly_mode": "cleaned_lazily_per_pass",
            },
        )

        transformed_pass_number = 0

        def cleaned_hourly_chunks(pass_name: str) -> Iterable[pd.DataFrame]:
            metric: dict[str, object] | None = None
            for raw_chunk in extract_pedestrian_counts_hourly(
                config.raw_data_dir,
                chunk_size=config.hourly_chunk_size,
            ):
                if metric is None:
                    metric = {
                        "pass": pass_name,
                        "chunks": 0,
                        "rows": 0,
                        "first_chunk_rows": len(raw_chunk),
                        "final_chunk_rows": 0,
                        "maximum_chunk_rows": 0,
                    }
                    state.hourly_passes.append(metric)
                cleaned = clean_pedestrian_counts_hourly(raw_chunk)
                metric["chunks"] = int(metric["chunks"]) + 1
                metric["rows"] = int(metric["rows"]) + len(cleaned)
                metric["final_chunk_rows"] = len(cleaned)
                metric["maximum_chunk_rows"] = max(
                    int(metric["maximum_chunk_rows"]), len(cleaned)
                )
                yield cleaned

        def validate_inputs() -> HistoricalValidationReport:
            report = validate_historical_workflow(
                cleaned_sensors.canonical_sensors,
                cleaned_sensors.sensor_directions,
                cleaned_hourly_chunks("validation"),
                cleaned_landmarks,
            )
            state.validation_summary = _validation_summary(report)
            state.warnings.extend(_validation_warnings(report))
            if not report.passed:
                raise ValueError(
                    f"historical validation failed with {report.error_count} error(s)"
                )
            return report

        validation = stage(
            "validation",
            validate_inputs,
            lambda value: state.validation_summary,
        )

        transformation = stage(
            "transformation",
            lambda: transform_historical_workflow(
                cleaned_sensors.canonical_sensors,
                cleaned_sensors.sensor_directions,
                cleaned_hourly_chunks("transformation_contract"),
                cleaned_landmarks,
                validation,
            ),
            lambda value: {
                "canonical_sensors": len(value.canonical_sensors),
                "sensor_directions": len(value.sensor_directions),
                "landmarks": len(value.landmarks),
                "hourly_mode": "restartable_lazy_factory",
            },
        )
        assert isinstance(transformation, HistoricalTransformation)

        def transformed_hourly_chunks() -> Iterable[pd.DataFrame]:
            nonlocal transformed_pass_number
            transformed_pass_number += 1
            pass_name = (
                "baseline"
                if transformed_pass_number == 1
                else f"feature_enrichment_{transformed_pass_number - 1}"
            )
            for cleaned in cleaned_hourly_chunks(pass_name):
                yield transform_hourly_chunk(
                    cleaned, cleaned_sensors.canonical_sensors
                )

        feature_workflow = stage(
            "baseline_and_features",
            lambda: engineer_historical_features(
                transformed_hourly_chunks,
                config.crowd_config,
            ),
            lambda value: _baseline_summary(value),
        )
        state.baseline_summary = _baseline_summary(feature_workflow)

        spatial_workflow = stage(
            "spatial_processing",
            lambda: process_spatial_workflow(
                extraction.pedestrian_network,
                cleaned_sensors.canonical_sensors,
                cleaned_landmarks,
                config.spatial_config,
            ),
            lambda value: _spatial_summary(value),
        )
        state.spatial_summary = _spatial_summary(spatial_workflow)

        def enriched_hourly_chunks() -> Iterable[pd.DataFrame]:
            for transformed_chunk in transformed_hourly_chunks():
                yield add_crowd_features(
                    transformed_chunk,
                    feature_workflow.baseline,
                    config.crowd_config,
                )

        def load_outputs() -> HistoricalLoadResult:
            with PostgresLoader(
                connection=connection,
                config=config.loader_config,
            ) as loader:
                return loader.load_historical_dataset(
                    sensors=transformation.canonical_sensors,
                    sensor_directions=transformation.sensor_directions,
                    landmarks=transformation.landmarks,
                    hourly_chunk_factory=enriched_hourly_chunks,
                    crowd_baselines=feature_workflow.baseline,
                    spatial_network=spatial_workflow.network,
                    sensor_mapping=spatial_workflow.sensor_mapping,
                    landmark_mapping=spatial_workflow.landmark_mapping,
                    validation_report=validation,
                )

        load_result = stage(
            "database_loading",
            load_outputs,
            lambda value: {
                "status": value.status,
                "dry_run": value.dry_run,
                "pipeline_run_id": None if value.dry_run else value.pipeline_run_id,
                "table_results": [item.to_dict() for item in value.table_results],
            },
        )
        state.table_results = _load_table_results(load_result)
        state.rows_processed = sum(
            item.source_count for item in load_result.table_results
        )
        state.pipeline_run_id = (
            None if load_result.dry_run else load_result.pipeline_run_id
        )
        state.warnings.extend(load_result.warnings)

        completed_at = _utc_now()
        return HistoricalPipelineResult(
            status=load_result.status,
            started_at=pipeline_started_at,
            completed_at=completed_at,
            elapsed_seconds=time.perf_counter() - pipeline_started,
            dry_run=load_result.dry_run,
            hourly_chunk_size=config.hourly_chunk_size,
            stages=tuple(stages),
            validation_summary=state.validation_summary,
            rows_processed=state.rows_processed,
            table_results=state.table_results,
            spatial_summary=state.spatial_summary,
            baseline_summary=state.baseline_summary,
            hourly_passes=tuple(dict(item) for item in state.hourly_passes),
            pipeline_run_id=state.pipeline_run_id,
            warnings=tuple(state.warnings),
        )
    except _StageFailure as failure:
        completed_at = _utc_now()
        safe_message = _safe_error_message(failure.error)
        result = HistoricalPipelineResult(
            status="failed",
            started_at=pipeline_started_at,
            completed_at=completed_at,
            elapsed_seconds=time.perf_counter() - pipeline_started,
            dry_run=config.dry_run,
            hourly_chunk_size=config.hourly_chunk_size,
            stages=tuple(stages),
            validation_summary=state.validation_summary,
            rows_processed=state.rows_processed,
            table_results=state.table_results,
            spatial_summary=state.spatial_summary,
            baseline_summary=state.baseline_summary,
            hourly_passes=tuple(dict(item) for item in state.hourly_passes),
            pipeline_run_id=state.pipeline_run_id,
            warnings=tuple(state.warnings),
            failed_stage=failure.stage,
            error_type=failure.error.__class__.__name__,
            error_message=safe_message,
        )
        raise PipelineExecutionError(
            failure.stage, result, failure.error
        ) from failure.error


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the CityFlow historical V1 data pipeline."
    )
    parser.add_argument(
        "--raw-data-dir",
        required=True,
        type=Path,
        help="directory containing the four historical V1 raw CSV files",
    )
    parser.add_argument(
        "--hourly-chunk-size",
        type=int,
        default=DEFAULT_HOURLY_CHUNK_SIZE,
        help="maximum hourly rows processed per chunk (default: 100000)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="optional database URL; environment variables are safer for normal use",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="execute the complete database path and roll all persistent changes back",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="emit one JSON result suitable for automation",
    )
    return parser


def _human_output(result: HistoricalPipelineResult) -> str:
    lines = [
        f"Historical pipeline status: {result.status}",
        f"Elapsed seconds: {result.elapsed_seconds:.3f}",
        f"Dry run: {result.dry_run}",
        f"Pipeline run ID: {result.pipeline_run_id or 'not persisted'}",
        f"Rows processed: {result.rows_processed}",
    ]
    for stage in result.stages:
        lines.append(
            f"  {stage.name}: {stage.status} ({stage.elapsed_seconds:.3f}s)"
        )
    if result.failed_stage:
        lines.append(f"Failed stage: {result.failed_stage}")
        lines.append(f"Error: {result.error_type}: {result.error_message}")
    if result.warnings:
        lines.append(f"Warnings: {len(result.warnings)}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process-compatible exit code."""

    args = _build_parser().parse_args(argv)
    try:
        loader_config = DatabaseLoaderConfig(
            database_url=args.database_url,
            dry_run=args.dry_run,
        )
        config = HistoricalPipelineConfig(
            raw_data_dir=args.raw_data_dir,
            hourly_chunk_size=args.hourly_chunk_size,
            loader_config=loader_config,
            dry_run=args.dry_run,
            json_output=args.json_output,
        )
        result = run_historical_pipeline(config)
    except PipelineExecutionError as error:
        result = error.result
        output = (
            json.dumps(result.to_dict(), sort_keys=True)
            if args.json_output
            else _human_output(result)
        )
        print(output)
        return 1
    output = (
        json.dumps(result.to_dict(), sort_keys=True)
        if args.json_output
        else _human_output(result)
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXCLUDED_HISTORICAL_SOURCES",
    "PIPELINE_STAGE_ORDER",
    "HistoricalPipelineConfig",
    "HistoricalPipelineResult",
    "PipelineExecutionError",
    "PipelineStageResult",
    "main",
    "run_historical_pipeline",
]
