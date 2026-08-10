"""Structured, deterministic validation of cleaned CityFlow data."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final, Literal
from uuid import UUID

import numpy as np
import pandas as pd
from pandas.api.types import (
    is_datetime64_any_dtype,
    is_integer_dtype,
    is_numeric_dtype,
    is_string_dtype,
)

from cityflow_pipeline.clean import (
    COORDINATE_TOLERANCE,
    HOURLY_COLUMNS,
    LANDMARK_COLUMNS,
    SENSOR_COLUMNS,
    SENSOR_DIRECTION_COLUMNS,
)


Severity = Literal["error", "warning"]
JsonPrimitive = str | int | float | bool | None

VALIDATED_HISTORICAL_DATASETS: Final = (
    "canonical_sensors",
    "sensor_directions",
    "hourly_pedestrian_counts",
    "landmarks",
)
EXCLUDED_HISTORICAL_DATASETS: Final = (
    "pedestrian_counts_minutely",
    "pedestrian_network",
)


def _json_value(value: object) -> JsonPrimitive:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _json_mapping(values: Mapping[str, object]) -> dict[str, JsonPrimitive]:
    return {key: _json_value(value) for key, value in sorted(values.items())}


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One bounded, machine-readable data-quality finding."""

    severity: Severity
    code: str
    message: str
    affected_row_count: int
    examples: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        if self.severity not in ("error", "warning"):
            raise ValueError("severity must be 'error' or 'warning'")
        if not self.code.strip():
            raise ValueError("code must not be blank")
        if self.affected_row_count < 0:
            raise ValueError("affected_row_count must not be negative")
        object.__setattr__(self, "examples", tuple(self.examples[:5]))

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-serialisable representation."""

        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "affected_row_count": self.affected_row_count,
            "examples": [_json_mapping(example) for example in self.examples],
        }


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Validation outcome and metrics for one cleaned dataset."""

    dataset_name: str
    checked_row_count: int
    issues: tuple[ValidationIssue, ...] = ()
    metrics: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.dataset_name.strip():
            raise ValueError("dataset_name must not be blank")
        if self.checked_row_count < 0:
            raise ValueError("checked_row_count must not be negative")
        severity_order = {"error": 0, "warning": 1}
        object.__setattr__(
            self,
            "issues",
            tuple(
                sorted(
                    self.issues,
                    key=lambda issue: (
                        severity_order[issue.severity],
                        issue.code,
                        issue.message,
                    ),
                )
            ),
        )
        object.__setattr__(self, "metrics", dict(sorted(self.metrics.items())))

    @property
    def error_count(self) -> int:
        """Number of distinct error issues in the report."""

        return sum(issue.severity == "error" for issue in self.issues)

    @property
    def warning_count(self) -> int:
        """Number of distinct warning issues in the report."""

        return sum(issue.severity == "warning" for issue in self.issues)

    @property
    def passed(self) -> bool:
        """Whether no error-level issue was found; warnings are allowed."""

        return self.error_count == 0

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-serialisable representation."""

        return {
            "dataset_name": self.dataset_name,
            "checked_row_count": self.checked_row_count,
            "issues": [issue.to_dict() for issue in self.issues],
            "metrics": {
                key: _json_value(value) for key, value in self.metrics.items()
            },
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class HistoricalValidationReport:
    """Separate cleaned-dataset reports plus the historical overall result.

    The minutely live feed is excluded from V1. The pedestrian network remains
    extraction-only until network cleaning and topology contracts are defined.
    """

    reports: tuple[ValidationReport, ...]
    excluded_datasets: tuple[str, ...] = EXCLUDED_HISTORICAL_DATASETS

    @property
    def passed(self) -> bool:
        """Whether every included cleaned dataset passed validation."""

        return all(report.passed for report in self.reports)

    @property
    def error_count(self) -> int:
        return sum(report.error_count for report in self.reports)

    @property
    def warning_count(self) -> int:
        return sum(report.warning_count for report in self.reports)

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-serialisable representation."""

        return {
            "reports": [report.to_dict() for report in self.reports],
            "excluded_datasets": list(self.excluded_datasets),
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "passed": self.passed,
        }


def _ensure_dataframe(frame: object, name: str) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{name} must be a pandas DataFrame")
    return frame


def _missing_columns_issue(
    frame: pd.DataFrame,
    required: tuple[str, ...],
) -> ValidationIssue | None:
    missing = tuple(column for column in required if column not in frame.columns)
    if not missing:
        return None
    return ValidationIssue(
        severity="error",
        code="schema.missing_columns",
        message=f"Required cleaned columns are missing: {missing}",
        affected_row_count=len(frame),
        examples=({"missing_columns": ", ".join(missing)},),
    )


def _mask_examples(
    frame: pd.DataFrame,
    mask: pd.Series,
    columns: Iterable[str],
) -> tuple[Mapping[str, object], ...]:
    valid_columns = [column for column in columns if column in frame.columns]
    examples: list[Mapping[str, object]] = []
    normalised_mask = mask.reindex(frame.index, fill_value=False).fillna(False)
    for row_index, row in frame.loc[normalised_mask, valid_columns].head(5).iterrows():
        example: dict[str, object] = {"row_index": row_index}
        example.update(row.to_dict())
        examples.append(example)
    return tuple(examples)


def _masked_issue(
    frame: pd.DataFrame,
    mask: pd.Series,
    *,
    severity: Severity,
    code: str,
    message: str,
    columns: Iterable[str],
) -> ValidationIssue | None:
    normalised_mask = mask.reindex(frame.index, fill_value=False).fillna(False)
    affected = int(normalised_mask.sum())
    if affected == 0:
        return None
    return ValidationIssue(
        severity=severity,
        code=code,
        message=message,
        affected_row_count=affected,
        examples=_mask_examples(frame, normalised_mask, columns),
    )


def _append(issue_list: list[ValidationIssue], issue: ValidationIssue | None) -> None:
    if issue is not None:
        issue_list.append(issue)


def _blank_mask(series: pd.Series) -> pd.Series:
    text = series.astype("string").str.strip()
    return text.isna() | text.eq("")


def _numeric_parts(series: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    numeric = pd.to_numeric(series, errors="coerce")
    finite = numeric.notna() & np.isfinite(numeric)
    integer_compatible = finite & numeric.mod(1).eq(0)
    return numeric, finite, integer_compatible


def _dtype_issue(
    frame: pd.DataFrame,
    dataset_code: str,
    invalid_columns: list[tuple[str, str]],
) -> ValidationIssue | None:
    if not invalid_columns:
        return None
    return ValidationIssue(
        severity="error",
        code=f"{dataset_code}.invalid_dtype",
        message="Important cleaned columns do not use their expected dtypes.",
        affected_row_count=len(frame),
        examples=tuple(
            {"column": column, "actual_dtype": actual_dtype}
            for column, actual_dtype in invalid_columns[:5]
        ),
    )


def _coordinate_issues(
    frame: pd.DataFrame,
    *,
    prefix: str,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if "latitude" not in frame.columns or "longitude" not in frame.columns:
        return issues
    latitude, lat_finite, _ = _numeric_parts(frame["latitude"])
    longitude, lon_finite, _ = _numeric_parts(frame["longitude"])
    invalid = (
        ~lat_finite
        | ~lon_finite
        | (lat_finite & ~latitude.between(-90, 90))
        | (lon_finite & ~longitude.between(-180, 180))
    )
    _append(
        issues,
        _masked_issue(
            frame,
            invalid,
            severity="error",
            code=f"{prefix}.invalid_coordinate",
            message="Coordinates must be finite numeric WGS84 latitude/longitude values.",
            columns=("latitude", "longitude"),
        ),
    )
    return issues


def validate_canonical_sensors(frame: pd.DataFrame) -> ValidationReport:
    """Validate the canonical sensor table produced by the cleaning layer."""

    frame = _ensure_dataframe(frame, "canonical sensors")
    issues: list[ValidationIssue] = []
    _append(issues, _missing_columns_issue(frame, SENSOR_COLUMNS))
    if frame.empty:
        issues.append(
            ValidationIssue(
                "error",
                "sensor.empty",
                "Canonical sensor data must not be empty.",
                0,
            )
        )

    invalid_dtypes: list[tuple[str, str]] = []
    if "sensor_id" in frame and not is_integer_dtype(frame["sensor_id"].dtype):
        invalid_dtypes.append(("sensor_id", str(frame["sensor_id"].dtype)))
    if "installation_date" in frame and not is_datetime64_any_dtype(
        frame["installation_date"].dtype
    ):
        invalid_dtypes.append(
            ("installation_date", str(frame["installation_date"].dtype))
        )
    for column in ("sensor_description", "sensor_name", "location_type", "status"):
        if column in frame and not is_string_dtype(frame[column].dtype):
            invalid_dtypes.append((column, str(frame[column].dtype)))
    for column in ("latitude", "longitude"):
        if column in frame and not is_numeric_dtype(frame[column].dtype):
            invalid_dtypes.append((column, str(frame[column].dtype)))
    _append(issues, _dtype_issue(frame, "sensor", invalid_dtypes))

    if "sensor_id" in frame:
        _append(
            issues,
            _masked_issue(
                frame,
                frame["sensor_id"].isna(),
                severity="error",
                code="sensor.missing_id",
                message="Every canonical sensor must have a sensor_id.",
                columns=("sensor_id",),
            ),
        )
        _append(
            issues,
            _masked_issue(
                frame,
                frame["sensor_id"].notna()
                & frame["sensor_id"].duplicated(keep=False),
                severity="error",
                code="sensor.duplicate_id",
                message="sensor_id must be unique in the canonical sensor table.",
                columns=("sensor_id",),
            ),
        )

    blank_required = pd.Series(False, index=frame.index)
    for column in ("sensor_description", "sensor_name", "location_type", "status"):
        if column in frame:
            blank_required |= _blank_mask(frame[column])
    _append(
        issues,
        _masked_issue(
            frame,
            blank_required,
            severity="error",
            code="sensor.blank_required_text",
            message="Required canonical sensor text fields must not be blank.",
            columns=(
                "sensor_id",
                "sensor_description",
                "sensor_name",
                "location_type",
                "status",
            ),
        ),
    )
    issues.extend(_coordinate_issues(frame, prefix="sensor"))

    if "installation_date" in frame:
        parsed_dates = pd.to_datetime(frame["installation_date"], errors="coerce")
        invalid_dates = frame["installation_date"].notna() & parsed_dates.isna()
        _append(
            issues,
            _masked_issue(
                frame,
                invalid_dates,
                severity="error",
                code="sensor.invalid_installation_date",
                message="Non-null installation dates must be valid dates.",
                columns=("sensor_id", "installation_date"),
            ),
        )

    unique_sensor_count = (
        int(frame["sensor_id"].dropna().nunique()) if "sensor_id" in frame else 0
    )
    return ValidationReport(
        "canonical_sensors",
        len(frame),
        tuple(issues),
        {"unique_sensor_count": unique_sensor_count},
    )


def validate_sensor_directions(
    frame: pd.DataFrame,
    canonical_sensors: pd.DataFrame,
) -> ValidationReport:
    """Validate sensor direction configs and canonical sensor references."""

    frame = _ensure_dataframe(frame, "sensor directions")
    canonical_sensors = _ensure_dataframe(canonical_sensors, "canonical sensors")
    issues: list[ValidationIssue] = []
    _append(issues, _missing_columns_issue(frame, SENSOR_DIRECTION_COLUMNS))

    invalid_dtypes: list[tuple[str, str]] = []
    for column in ("sensor_id", "direction_config_id"):
        if column in frame and not is_integer_dtype(frame[column].dtype):
            invalid_dtypes.append((column, str(frame[column].dtype)))
    for column in ("direction_1_label", "direction_2_label"):
        if column in frame and not is_string_dtype(frame[column].dtype):
            invalid_dtypes.append((column, str(frame[column].dtype)))
    _append(issues, _dtype_issue(frame, "sensor_direction", invalid_dtypes))

    canonical_ids: set[object] = set()
    if "sensor_id" in canonical_sensors:
        canonical_ids = set(canonical_sensors["sensor_id"].dropna().tolist())
    if "sensor_id" in frame:
        missing_sensor = frame["sensor_id"].isna()
        _append(
            issues,
            _masked_issue(
                frame,
                missing_sensor,
                severity="error",
                code="sensor_direction.missing_sensor_id",
                message="Every direction config must have a sensor_id.",
                columns=("sensor_id", "direction_config_id"),
            ),
        )
        orphan = frame["sensor_id"].notna() & ~frame["sensor_id"].isin(canonical_ids)
        _append(
            issues,
            _masked_issue(
                frame,
                orphan,
                severity="error",
                code="sensor_direction.orphan_sensor",
                message="Direction configs must reference a canonical sensor.",
                columns=("sensor_id", "direction_config_id"),
            ),
        )

    if "direction_config_id" in frame:
        _, _, integer_config = _numeric_parts(frame["direction_config_id"])
        invalid_config = ~integer_config | pd.to_numeric(
            frame["direction_config_id"], errors="coerce"
        ).le(0)
        _append(
            issues,
            _masked_issue(
                frame,
                invalid_config,
                severity="error",
                code="sensor_direction.invalid_config_id",
                message="direction_config_id must be a positive integer.",
                columns=("sensor_id", "direction_config_id"),
            ),
        )

    if {"sensor_id", "direction_config_id"}.issubset(frame.columns):
        duplicate_key = frame.duplicated(
            subset=["sensor_id", "direction_config_id"], keep=False
        )
        _append(
            issues,
            _masked_issue(
                frame,
                duplicate_key,
                severity="error",
                code="sensor_direction.duplicate_business_key",
                message="The sensor_id/direction_config_id business key must be unique.",
                columns=("sensor_id", "direction_config_id"),
            ),
        )

    if {"direction_1_label", "direction_2_label"}.issubset(frame.columns):
        blank_labels = _blank_mask(frame["direction_1_label"]) & _blank_mask(
            frame["direction_2_label"]
        )
        _append(
            issues,
            _masked_issue(
                frame,
                blank_labels,
                severity="error",
                code="sensor_direction.blank_labels",
                message="At least one direction label must be non-blank.",
                columns=(
                    "sensor_id",
                    "direction_config_id",
                    "direction_1_label",
                    "direction_2_label",
                ),
            ),
        )
    if {"sensor_id", "direction_1_label", "direction_2_label"}.issubset(
        frame.columns
    ):
        duplicate_config = frame.duplicated(
            subset=["sensor_id", "direction_1_label", "direction_2_label"],
            keep=False,
        )
        _append(
            issues,
            _masked_issue(
                frame,
                duplicate_config,
                severity="error",
                code="sensor_direction.duplicate_configuration",
                message="Exact direction label configurations must be unique per sensor.",
                columns=(
                    "sensor_id",
                    "direction_config_id",
                    "direction_1_label",
                    "direction_2_label",
                ),
            ),
        )

    configured_sensors = (
        int(frame["sensor_id"].dropna().nunique()) if "sensor_id" in frame else 0
    )
    return ValidationReport(
        "sensor_directions",
        len(frame),
        tuple(issues),
        {"sensors_with_direction_configs": configured_sensors},
    )


def _valid_uuid5(value: object) -> bool:
    try:
        parsed = UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 5


def validate_landmarks(frame: pd.DataFrame) -> ValidationReport:
    """Validate the cleaned landmark reference table."""

    frame = _ensure_dataframe(frame, "landmarks")
    issues: list[ValidationIssue] = []
    _append(issues, _missing_columns_issue(frame, LANDMARK_COLUMNS))
    if frame.empty:
        issues.append(
            ValidationIssue(
                "error",
                "landmark.empty",
                "Cleaned landmark data must not be empty.",
                0,
            )
        )

    invalid_dtypes: list[tuple[str, str]] = []
    for column in ("landmark_id", "name", "category", "subcategory"):
        if column in frame and not is_string_dtype(frame[column].dtype):
            invalid_dtypes.append((column, str(frame[column].dtype)))
    for column in ("latitude", "longitude"):
        if column in frame and not is_numeric_dtype(frame[column].dtype):
            invalid_dtypes.append((column, str(frame[column].dtype)))
    _append(issues, _dtype_issue(frame, "landmark", invalid_dtypes))

    if "landmark_id" in frame:
        missing_id = _blank_mask(frame["landmark_id"])
        _append(
            issues,
            _masked_issue(
                frame,
                missing_id,
                severity="error",
                code="landmark.missing_id",
                message="Every landmark must have a landmark_id.",
                columns=("landmark_id", "name"),
            ),
        )
        duplicate_id = ~missing_id & frame["landmark_id"].duplicated(keep=False)
        _append(
            issues,
            _masked_issue(
                frame,
                duplicate_id,
                severity="error",
                code="landmark.duplicate_id",
                message="landmark_id must be unique.",
                columns=("landmark_id", "name"),
            ),
        )
        invalid_uuid = ~missing_id & ~frame["landmark_id"].map(_valid_uuid5)
        _append(
            issues,
            _masked_issue(
                frame,
                invalid_uuid,
                severity="error",
                code="landmark.invalid_uuid5",
                message="landmark_id must be a canonical UUID version 5 string.",
                columns=("landmark_id", "name"),
            ),
        )

    if "name" in frame:
        _append(
            issues,
            _masked_issue(
                frame,
                _blank_mask(frame["name"]),
                severity="error",
                code="landmark.blank_name",
                message="Landmark names must not be blank.",
                columns=("landmark_id", "name"),
            ),
        )
    issues.extend(_coordinate_issues(frame, prefix="landmark"))

    if {"name", "latitude", "longitude"}.issubset(frame.columns):
        duplicate_record = frame.duplicated(
            subset=["name", "latitude", "longitude"], keep=False
        )
        _append(
            issues,
            _masked_issue(
                frame,
                duplicate_record,
                severity="error",
                code="landmark.duplicate_business_record",
                message="Landmark name/coordinate business records must be unique.",
                columns=("landmark_id", "name", "latitude", "longitude"),
            ),
        )

    missing_category = pd.Series(False, index=frame.index)
    for column in ("category", "subcategory"):
        if column in frame:
            missing_category |= _blank_mask(frame[column])
    _append(
        issues,
        _masked_issue(
            frame,
            missing_category,
            severity="warning",
            code="landmark.missing_category",
            message="Missing landmark category or subcategory reduces classification detail.",
            columns=("landmark_id", "name", "category", "subcategory"),
        ),
    )

    unique_landmarks = (
        int(frame["landmark_id"].dropna().nunique()) if "landmark_id" in frame else 0
    )
    return ValidationReport(
        "landmarks",
        len(frame),
        tuple(issues),
        {"unique_landmark_count": unique_landmarks},
    )


def _encode_hourly_key(sensor_id: int, sensing_date: pd.Timestamp, hour: int) -> int:
    """Encode an exact business key as one compact arbitrary-precision integer."""

    zigzag_sensor = sensor_id * 2 if sensor_id >= 0 else (-sensor_id * 2) - 1
    date_ordinal = sensing_date.date().toordinal()
    pair_sum = date_ordinal + zigzag_sensor
    paired = (pair_sum * (pair_sum + 1) // 2) + zigzag_sensor
    return (paired * 24) + hour


@dataclass(slots=True)
class _AggregatedIssue:
    severity: Severity
    code: str
    message: str
    affected_row_count: int = 0
    examples: list[Mapping[str, object]] = field(default_factory=list)

    def add(self, issue: ValidationIssue) -> None:
        self.affected_row_count += issue.affected_row_count
        remaining = 5 - len(self.examples)
        if remaining > 0:
            self.examples.extend(issue.examples[:remaining])

    def build(self) -> ValidationIssue:
        return ValidationIssue(
            self.severity,
            self.code,
            self.message,
            self.affected_row_count,
            tuple(self.examples),
        )


class HourlyValidationSession:
    """Validate cleaned hourly chunks while retaining only compact aggregates."""

    def __init__(
        self,
        canonical_sensors: pd.DataFrame,
        *,
        coordinate_tolerance: float = COORDINATE_TOLERANCE,
    ) -> None:
        canonical_sensors = _ensure_dataframe(canonical_sensors, "canonical sensors")
        if (
            isinstance(coordinate_tolerance, bool)
            or not isinstance(coordinate_tolerance, (int, float))
            or not np.isfinite(coordinate_tolerance)
            or coordinate_tolerance < 0
        ):
            raise ValueError("coordinate_tolerance must be a finite non-negative number")
        self.coordinate_tolerance = float(coordinate_tolerance)
        self._canonical: dict[object, tuple[object, object, object]] = {}
        required_lookup = {"sensor_id", "sensor_name", "latitude", "longitude"}
        if required_lookup.issubset(canonical_sensors.columns):
            for row in canonical_sensors.loc[:, list(required_lookup)].itertuples(
                index=False
            ):
                sensor_id = getattr(row, "sensor_id")
                if pd.notna(sensor_id) and sensor_id not in self._canonical:
                    self._canonical[sensor_id] = (
                        getattr(row, "sensor_name"),
                        getattr(row, "latitude"),
                        getattr(row, "longitude"),
                    )
        self._seen_keys: set[int] = set()
        self._unique_sensors: set[object] = set()
        self._aggregated_issues: dict[
            tuple[Severity, str, str], _AggregatedIssue
        ] = {}
        self._total_rows = 0
        self._chunk_count = 0
        self._duplicate_key_count = 0
        self._orphan_sensor_count = 0
        self._minimum_date: pd.Timestamp | None = None
        self._maximum_date: pd.Timestamp | None = None
        self._minimum_count: float | int | None = None
        self._maximum_count: float | int | None = None
        self._final_report: ValidationReport | None = None

    def _aggregate(self, issues: Iterable[ValidationIssue]) -> None:
        for issue in issues:
            key = (issue.severity, issue.code, issue.message)
            aggregate = self._aggregated_issues.setdefault(
                key,
                _AggregatedIssue(issue.severity, issue.code, issue.message),
            )
            aggregate.add(issue)

    def validate_chunk(self, frame: pd.DataFrame) -> ValidationReport:
        """Validate and accumulate one cleaned hourly DataFrame chunk."""

        if self._final_report is not None:
            raise RuntimeError("cannot validate another chunk after finalize()")
        frame = _ensure_dataframe(frame, "hourly chunk")
        issues: list[ValidationIssue] = []
        _append(issues, _missing_columns_issue(frame, HOURLY_COLUMNS))

        invalid_dtypes: list[tuple[str, str]] = []
        integer_columns = (
            "sensor_id",
            "hour",
            "direction_1_count",
            "direction_2_count",
            "pedestrian_count",
        )
        for column in integer_columns:
            if column in frame and not is_integer_dtype(frame[column].dtype):
                invalid_dtypes.append((column, str(frame[column].dtype)))
        if "sensing_date" in frame and not is_datetime64_any_dtype(
            frame["sensing_date"].dtype
        ):
            invalid_dtypes.append(("sensing_date", str(frame["sensing_date"].dtype)))
        for column in ("source_record_id", "sensor_name"):
            if column in frame and not is_string_dtype(frame[column].dtype):
                invalid_dtypes.append((column, str(frame[column].dtype)))
        for column in ("latitude", "longitude"):
            if column in frame and not is_numeric_dtype(frame[column].dtype):
                invalid_dtypes.append((column, str(frame[column].dtype)))
        _append(issues, _dtype_issue(frame, "hourly", invalid_dtypes))

        valid_sensor = pd.Series(False, index=frame.index)
        sensor_numeric = pd.Series(np.nan, index=frame.index)
        if "sensor_id" in frame:
            sensor_numeric, sensor_finite, sensor_integer = _numeric_parts(
                frame["sensor_id"]
            )
            valid_sensor = sensor_integer
            invalid_sensor = ~sensor_integer
            _append(
                issues,
                _masked_issue(
                    frame,
                    invalid_sensor,
                    severity="error",
                    code="hourly.invalid_sensor_id",
                    message="Hourly sensor_id values must be present integers.",
                    columns=("sensor_id", "sensing_date", "hour"),
                ),
            )

        parsed_dates = pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns]")
        valid_date = pd.Series(False, index=frame.index)
        if "sensing_date" in frame:
            parsed_dates = pd.to_datetime(frame["sensing_date"], errors="coerce")
            valid_date = parsed_dates.notna() & parsed_dates.eq(parsed_dates.dt.normalize())
            _append(
                issues,
                _masked_issue(
                    frame,
                    ~valid_date,
                    severity="error",
                    code="hourly.invalid_sensing_date",
                    message="sensing_date must contain valid date-only values.",
                    columns=("sensor_id", "sensing_date", "hour"),
                ),
            )

        hour_numeric = pd.Series(np.nan, index=frame.index)
        valid_hour = pd.Series(False, index=frame.index)
        if "hour" in frame:
            hour_numeric, _, hour_integer = _numeric_parts(frame["hour"])
            valid_hour = hour_integer & hour_numeric.between(0, 23)
            _append(
                issues,
                _masked_issue(
                    frame,
                    ~valid_hour,
                    severity="error",
                    code="hourly.invalid_hour",
                    message="hour must be an integer from 0 through 23.",
                    columns=("sensor_id", "sensing_date", "hour"),
                ),
            )

        count_values: dict[str, pd.Series] = {}
        valid_counts: dict[str, pd.Series] = {}
        for column in (
            "direction_1_count",
            "direction_2_count",
            "pedestrian_count",
        ):
            if column not in frame:
                continue
            numeric, finite, integer_compatible = _numeric_parts(frame[column])
            count_values[column] = numeric
            if column == "pedestrian_count":
                valid = integer_compatible
            else:
                valid = frame[column].isna() | integer_compatible
            valid_counts[column] = valid
            _append(
                issues,
                _masked_issue(
                    frame,
                    ~valid,
                    severity="error",
                    code="hourly.invalid_count",
                    message="Counts must be numeric and integer-compatible when present.",
                    columns=(
                        "sensor_id",
                        "sensing_date",
                        "hour",
                        column,
                    ),
                ),
            )
            negative = finite & numeric.lt(0)
            _append(
                issues,
                _masked_issue(
                    frame,
                    negative,
                    severity="error",
                    code="hourly.negative_count",
                    message="Pedestrian and direction counts must not be negative.",
                    columns=(
                        "sensor_id",
                        "sensing_date",
                        "hour",
                        column,
                    ),
                ),
            )

        if {
            "direction_1_count",
            "direction_2_count",
            "pedestrian_count",
        }.issubset(count_values):
            available = (
                frame["direction_1_count"].notna()
                & frame["direction_2_count"].notna()
                & frame["pedestrian_count"].notna()
            )
            comparable = (
                available
                & valid_counts["direction_1_count"]
                & valid_counts["direction_2_count"]
                & valid_counts["pedestrian_count"]
            )
            mismatch = comparable & count_values["direction_1_count"].add(
                count_values["direction_2_count"]
            ).ne(count_values["pedestrian_count"])
            _append(
                issues,
                _masked_issue(
                    frame,
                    mismatch,
                    severity="warning",
                    code="hourly.direction_sum_mismatch",
                    message="Available direction counts do not sum to pedestrian_count.",
                    columns=(
                        "sensor_id",
                        "sensing_date",
                        "hour",
                        "direction_1_count",
                        "direction_2_count",
                        "pedestrian_count",
                    ),
                ),
            )

        valid_key = valid_sensor & valid_date & valid_hour
        within_duplicate = pd.Series(False, index=frame.index)
        cross_duplicate = pd.Series(False, index=frame.index)
        duplicate_occurrence = pd.Series(False, index=frame.index)
        chunk_seen: set[int] = set()
        if valid_key.any():
            key_positions: dict[int, list[object]] = {}
            for row_index in frame.index[valid_key]:
                key = _encode_hourly_key(
                    int(sensor_numeric.at[row_index]),
                    pd.Timestamp(parsed_dates.at[row_index]),
                    int(hour_numeric.at[row_index]),
                )
                key_positions.setdefault(key, []).append(row_index)
                if key in self._seen_keys:
                    cross_duplicate.at[row_index] = True
                    duplicate_occurrence.at[row_index] = True
                elif key in chunk_seen:
                    duplicate_occurrence.at[row_index] = True
                chunk_seen.add(key)
            for positions in key_positions.values():
                if len(positions) > 1:
                    within_duplicate.loc[positions] = True
            self._seen_keys.update(chunk_seen)

        _append(
            issues,
            _masked_issue(
                frame,
                within_duplicate,
                severity="error",
                code="hourly.duplicate_business_key_within_chunk",
                message="sensor_id/sensing_date/hour must be unique within a chunk.",
                columns=("sensor_id", "sensing_date", "hour"),
            ),
        )
        _append(
            issues,
            _masked_issue(
                frame,
                cross_duplicate,
                severity="error",
                code="hourly.duplicate_business_key_across_chunks",
                message="sensor_id/sensing_date/hour was already seen in an earlier chunk.",
                columns=("sensor_id", "sensing_date", "hour"),
            ),
        )

        orphan = pd.Series(False, index=frame.index)
        if "sensor_id" in frame:
            orphan = valid_sensor & ~frame["sensor_id"].isin(self._canonical)
            _append(
                issues,
                _masked_issue(
                    frame,
                    orphan,
                    severity="error",
                    code="hourly.orphan_sensor",
                    message="Hourly sensor_id must reference a canonical sensor.",
                    columns=("sensor_id", "sensing_date", "hour"),
                ),
            )

        comparable_reference = valid_sensor & ~orphan
        if "sensor_name" in frame:
            expected_names = frame["sensor_id"].map(
                lambda sensor_id: self._canonical.get(sensor_id, (pd.NA, 0, 0))[0]
            )
            name_mismatch = (
                comparable_reference
                & frame["sensor_name"].notna()
                & expected_names.notna()
                & frame["sensor_name"].astype("string").ne(
                    expected_names.astype("string")
                )
            )
            _append(
                issues,
                _masked_issue(
                    frame,
                    name_mismatch,
                    severity="warning",
                    code="hourly.sensor_name_mismatch",
                    message="Hourly sensor_name differs from current canonical metadata.",
                    columns=("sensor_id", "sensor_name", "sensing_date", "hour"),
                ),
            )
        if {"latitude", "longitude"}.issubset(frame.columns):
            latitude = pd.to_numeric(frame["latitude"], errors="coerce")
            longitude = pd.to_numeric(frame["longitude"], errors="coerce")
            expected_latitude = frame["sensor_id"].map(
                lambda sensor_id: self._canonical.get(sensor_id, (pd.NA, np.nan, np.nan))[1]
            )
            expected_longitude = frame["sensor_id"].map(
                lambda sensor_id: self._canonical.get(sensor_id, (pd.NA, np.nan, np.nan))[2]
            )
            coordinate_mismatch = comparable_reference & (
                latitude.sub(expected_latitude).abs().gt(self.coordinate_tolerance)
                | longitude.sub(expected_longitude).abs().gt(
                    self.coordinate_tolerance
                )
            )
            _append(
                issues,
                _masked_issue(
                    frame,
                    coordinate_mismatch,
                    severity="warning",
                    code="hourly.sensor_coordinate_mismatch",
                    message="Hourly coordinates differ from current canonical metadata.",
                    columns=(
                        "sensor_id",
                        "sensing_date",
                        "hour",
                        "latitude",
                        "longitude",
                    ),
                ),
            )
        issues.extend(_coordinate_issues(frame, prefix="hourly"))

        self._total_rows += len(frame)
        self._chunk_count += 1
        self._duplicate_key_count += int(duplicate_occurrence.sum())
        self._orphan_sensor_count += int(orphan.sum())
        if "sensor_id" in frame:
            self._unique_sensors.update(frame.loc[valid_sensor, "sensor_id"].tolist())
        valid_date_values = parsed_dates.loc[valid_date]
        if not valid_date_values.empty:
            chunk_min = valid_date_values.min()
            chunk_max = valid_date_values.max()
            self._minimum_date = (
                chunk_min
                if self._minimum_date is None
                else min(self._minimum_date, chunk_min)
            )
            self._maximum_date = (
                chunk_max
                if self._maximum_date is None
                else max(self._maximum_date, chunk_max)
            )
        if "pedestrian_count" in count_values:
            valid_total = (
                valid_counts["pedestrian_count"]
                & count_values["pedestrian_count"].ge(0)
            )
            total_values = count_values["pedestrian_count"].loc[valid_total]
            if not total_values.empty:
                chunk_min_count = _json_value(total_values.min())
                chunk_max_count = _json_value(total_values.max())
                if isinstance(chunk_min_count, (int, float)) and isinstance(
                    chunk_max_count, (int, float)
                ):
                    self._minimum_count = (
                        chunk_min_count
                        if self._minimum_count is None
                        else min(self._minimum_count, chunk_min_count)
                    )
                    self._maximum_count = (
                        chunk_max_count
                        if self._maximum_count is None
                        else max(self._maximum_count, chunk_max_count)
                    )

        self._aggregate(issues)
        chunk_metrics = {
            "chunk_number": self._chunk_count,
            "duplicate_key_count": int(duplicate_occurrence.sum()),
            "orphan_sensor_reference_count": int(orphan.sum()),
            "unique_sensor_count": int(frame.loc[valid_sensor, "sensor_id"].nunique())
            if "sensor_id" in frame
            else 0,
        }
        return ValidationReport(
            "hourly_pedestrian_counts_chunk",
            len(frame),
            tuple(issues),
            chunk_metrics,
        )

    def finalize(self) -> ValidationReport:
        """Return the aggregate hourly report; repeated calls are idempotent."""

        if self._final_report is None:
            issues = tuple(
                aggregate.build() for aggregate in self._aggregated_issues.values()
            )
            metrics = {
                "chunk_count": self._chunk_count,
                "duplicate_key_count": self._duplicate_key_count,
                "maximum_count": self._maximum_count,
                "maximum_sensing_date": self._maximum_date,
                "minimum_count": self._minimum_count,
                "minimum_sensing_date": self._minimum_date,
                "orphan_sensor_reference_count": self._orphan_sensor_count,
                "unique_sensor_count": len(self._unique_sensors),
            }
            self._final_report = ValidationReport(
                "hourly_pedestrian_counts",
                self._total_rows,
                issues,
                metrics,
            )
        return self._final_report


def validate_hourly_chunk(
    frame: pd.DataFrame,
    canonical_sensors: pd.DataFrame,
    *,
    coordinate_tolerance: float = COORDINATE_TOLERANCE,
) -> ValidationReport:
    """Validate one cleaned hourly chunk without retaining it."""

    session = HourlyValidationSession(
        canonical_sensors,
        coordinate_tolerance=coordinate_tolerance,
    )
    session.validate_chunk(frame)
    return session.finalize()


def validate_historical_workflow(
    canonical_sensors: pd.DataFrame,
    sensor_directions: pd.DataFrame,
    hourly_chunks: Iterable[pd.DataFrame],
    landmarks: pd.DataFrame,
) -> HistoricalValidationReport:
    """Validate all currently cleaned V1 historical datasets.

    The minutely live source is excluded. The pedestrian network remains
    extraction-only until a future cleaning and topology-validation stage.
    """

    sensor_report = validate_canonical_sensors(canonical_sensors)
    direction_report = validate_sensor_directions(
        sensor_directions, canonical_sensors
    )
    hourly_session = HourlyValidationSession(canonical_sensors)
    for chunk in hourly_chunks:
        hourly_session.validate_chunk(chunk)
    hourly_report = hourly_session.finalize()
    landmark_report = validate_landmarks(landmarks)
    return HistoricalValidationReport(
        (sensor_report, direction_report, hourly_report, landmark_report)
    )


__all__ = [
    "EXCLUDED_HISTORICAL_DATASETS",
    "VALIDATED_HISTORICAL_DATASETS",
    "HistoricalValidationReport",
    "HourlyValidationSession",
    "ValidationIssue",
    "ValidationReport",
    "validate_canonical_sensors",
    "validate_historical_workflow",
    "validate_hourly_chunk",
    "validate_landmarks",
    "validate_sensor_directions",
]
