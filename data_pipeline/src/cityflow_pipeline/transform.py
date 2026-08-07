"""Pure transformations from validated CityFlow data to database-ready tables."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Final

import pandas as pd

from cityflow_pipeline.clean import (
    HOURLY_COLUMNS,
    LANDMARK_COLUMNS,
    SENSOR_COLUMNS,
    SENSOR_DIRECTION_COLUMNS,
)
from cityflow_pipeline.validate import HistoricalValidationReport


WGS84_SRID: Final = 4326
HOURLY_LOCAL_TIME_SEMANTICS: Final = (
    "timezone-naive Australia/Melbourne local clock time from source date and hour"
)

SENSOR_DIMENSION_COLUMNS: Final = (
    "sensor_id",
    "sensor_name",
    "sensor_description",
    "installation_date",
    "note",
    "location_type",
    "status",
    "latitude",
    "longitude",
    "geometry_wkt",
)
SENSOR_DIRECTION_TABLE_COLUMNS: Final = (
    "sensor_id",
    "direction_config_id",
    "direction_1_label",
    "direction_2_label",
)
HOURLY_FACT_COLUMNS: Final = (
    "source_record_id",
    "sensor_id",
    "sensing_date",
    "hour",
    "local_observation_datetime",
    "year",
    "month",
    "iso_weekday",
    "weekday_name",
    "is_weekend",
    "direction_1_count",
    "direction_2_count",
    "pedestrian_count",
)
LANDMARK_DIMENSION_COLUMNS: Final = (
    "landmark_id",
    "name",
    "category",
    "subcategory",
    "latitude",
    "longitude",
    "geometry_wkt",
)

TRANSFORMED_HISTORICAL_DATASETS: Final = (
    "canonical_sensors",
    "sensor_directions",
    "hourly_pedestrian_counts",
    "landmarks",
)
EXCLUDED_TRANSFORMATION_DATASETS: Final = (
    "pedestrian_counts_minutely",
    "pedestrian_network",
)


class DataTransformationError(ValueError):
    """Raised when validated data cannot satisfy a transformation contract."""


@dataclass(frozen=True, slots=True)
class HistoricalTransformation:
    """Eager dimensions and a lazy iterator of transformed hourly chunks.

    The original validation report remains available so callers retain any
    warning details that were allowed to proceed.
    """

    canonical_sensors: pd.DataFrame
    sensor_directions: pd.DataFrame
    hourly_chunks: Iterator[pd.DataFrame]
    landmarks: pd.DataFrame
    validation_report: HistoricalValidationReport
    excluded_datasets: tuple[str, ...] = EXCLUDED_TRANSFORMATION_DATASETS


def _ensure_dataframe(frame: object, name: str) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{name} must be a pandas DataFrame")
    return frame


def _require_columns(
    frame: pd.DataFrame,
    required: tuple[str, ...],
    dataset: str,
) -> None:
    missing = tuple(column for column in required if column not in frame.columns)
    if missing:
        raise DataTransformationError(
            f"{dataset} is missing required cleaned columns: {missing}"
        )


def _point_wkt(longitude: object, latitude: object) -> str:
    """Return deterministic WGS84 point WKT in longitude/latitude order."""

    return f"POINT({float(longitude)!r} {float(latitude)!r})"


def _geometry_wkt(frame: pd.DataFrame) -> pd.Series:
    values = [
        _point_wkt(longitude, latitude)
        for longitude, latitude in zip(
            frame["longitude"], frame["latitude"], strict=True
        )
    ]
    return pd.Series(values, index=frame.index, dtype="string")


def _require_unique_key(
    frame: pd.DataFrame,
    columns: list[str],
    dataset: str,
) -> None:
    if frame.duplicated(subset=columns, keep=False).any():
        raise DataTransformationError(
            f"{dataset} requires a unique business key: {tuple(columns)}"
        )


def transform_canonical_sensors(frame: pd.DataFrame) -> pd.DataFrame:
    """Create a deterministic WGS84 canonical sensor dimension."""

    frame = _ensure_dataframe(frame, "canonical sensors")
    _require_columns(frame, SENSOR_COLUMNS, "canonical sensors")
    _require_unique_key(frame, ["sensor_id"], "canonical sensors")

    transformed = frame.loc[:, SENSOR_COLUMNS].copy(deep=True)
    transformed["geometry_wkt"] = _geometry_wkt(transformed)
    transformed = transformed.loc[:, SENSOR_DIMENSION_COLUMNS]
    return transformed.sort_values("sensor_id", kind="mergesort").reset_index(
        drop=True
    )


def transform_sensor_directions(
    frame: pd.DataFrame,
    canonical_sensors: pd.DataFrame,
) -> pd.DataFrame:
    """Create an ordered sensor-direction table with valid sensor references."""

    frame = _ensure_dataframe(frame, "sensor directions")
    canonical_sensors = _ensure_dataframe(canonical_sensors, "canonical sensors")
    _require_columns(frame, SENSOR_DIRECTION_COLUMNS, "sensor directions")
    _require_columns(canonical_sensors, SENSOR_COLUMNS, "canonical sensors")
    _require_unique_key(
        frame,
        ["sensor_id", "direction_config_id"],
        "sensor directions",
    )
    canonical_ids = set(canonical_sensors["sensor_id"].tolist())
    orphan_ids = sorted(set(frame["sensor_id"].tolist()) - canonical_ids)
    if orphan_ids:
        raise DataTransformationError(
            f"sensor directions contain orphan sensor references: {orphan_ids[:5]}"
        )

    transformed = frame.loc[:, SENSOR_DIRECTION_COLUMNS].copy(deep=True)
    return transformed.sort_values(
        ["sensor_id", "direction_config_id"], kind="mergesort"
    ).reset_index(drop=True)


def transform_hourly_chunk(
    frame: pd.DataFrame,
    canonical_sensors: pd.DataFrame,
) -> pd.DataFrame:
    """Transform one hourly chunk using naive Melbourne local clock time.

    The source provides local date and hour without a trustworthy UTC offset.
    ``local_observation_datetime`` therefore remains timezone-naive and must
    not be interpreted as UTC. Repeated sensor name/coordinates are omitted
    because the referenced canonical sensor dimension contains them.
    """

    frame = _ensure_dataframe(frame, "hourly pedestrian counts")
    canonical_sensors = _ensure_dataframe(canonical_sensors, "canonical sensors")
    _require_columns(frame, HOURLY_COLUMNS, "hourly pedestrian counts")
    _require_columns(canonical_sensors, SENSOR_COLUMNS, "canonical sensors")
    canonical_ids = set(canonical_sensors["sensor_id"].tolist())
    orphan_ids = sorted(set(frame["sensor_id"].tolist()) - canonical_ids)
    if orphan_ids:
        raise DataTransformationError(
            f"hourly pedestrian counts contain orphan sensor references: "
            f"{orphan_ids[:5]}"
        )

    sensing_date = pd.to_datetime(frame["sensing_date"], errors="raise")
    local_datetime = sensing_date + pd.to_timedelta(frame["hour"], unit="h")
    iso_weekday = local_datetime.dt.isocalendar().day.astype("int64")
    weekday_names = iso_weekday.map(
        {
            1: "Monday",
            2: "Tuesday",
            3: "Wednesday",
            4: "Thursday",
            5: "Friday",
            6: "Saturday",
            7: "Sunday",
        }
    ).astype("string")

    transformed = pd.DataFrame(
        {
            "source_record_id": frame["source_record_id"].copy(),
            "sensor_id": frame["sensor_id"].copy(),
            "sensing_date": sensing_date,
            "hour": frame["hour"].copy(),
            "local_observation_datetime": local_datetime,
            "year": local_datetime.dt.year.astype("int64"),
            "month": local_datetime.dt.month.astype("int64"),
            "iso_weekday": iso_weekday,
            "weekday_name": weekday_names,
            "is_weekend": iso_weekday.ge(6),
            "direction_1_count": frame["direction_1_count"].copy(),
            "direction_2_count": frame["direction_2_count"].copy(),
            "pedestrian_count": frame["pedestrian_count"].copy(),
        }
    )
    return transformed.reset_index(drop=True).loc[:, HOURLY_FACT_COLUMNS]


def transform_landmarks(frame: pd.DataFrame) -> pd.DataFrame:
    """Create a deterministic WGS84 landmark dimension."""

    frame = _ensure_dataframe(frame, "landmarks")
    _require_columns(frame, LANDMARK_COLUMNS, "landmarks")
    _require_unique_key(frame, ["landmark_id"], "landmarks")

    transformed = frame.loc[:, LANDMARK_COLUMNS].copy(deep=True)
    transformed["geometry_wkt"] = _geometry_wkt(transformed)
    transformed = transformed.loc[:, LANDMARK_DIMENSION_COLUMNS]
    return transformed.sort_values("landmark_id", kind="mergesort").reset_index(
        drop=True
    )


def _transform_hourly_chunks(
    chunks: Iterable[pd.DataFrame],
    canonical_sensors: pd.DataFrame,
) -> Iterator[pd.DataFrame]:
    for chunk in chunks:
        yield transform_hourly_chunk(chunk, canonical_sensors)


def transform_historical_workflow(
    canonical_sensors: pd.DataFrame,
    sensor_directions: pd.DataFrame,
    hourly_chunks: Iterable[pd.DataFrame],
    landmarks: pd.DataFrame,
    validation_report: HistoricalValidationReport,
) -> HistoricalTransformation:
    """Transform validated V1 history while keeping hourly processing lazy.

    The live minutely source and pedestrian network are intentionally outside
    this workflow. Validation warnings may proceed and remain visible through
    ``HistoricalTransformation.validation_report``; errors block all output.
    """

    if not isinstance(validation_report, HistoricalValidationReport):
        raise TypeError("validation_report must be a HistoricalValidationReport")
    if not validation_report.passed:
        raise DataTransformationError(
            "historical transformation requires a passed validation report"
        )

    transformed_sensors = transform_canonical_sensors(canonical_sensors)
    transformed_directions = transform_sensor_directions(
        sensor_directions, canonical_sensors
    )
    transformed_landmarks = transform_landmarks(landmarks)
    return HistoricalTransformation(
        canonical_sensors=transformed_sensors,
        sensor_directions=transformed_directions,
        hourly_chunks=_transform_hourly_chunks(hourly_chunks, canonical_sensors),
        landmarks=transformed_landmarks,
        validation_report=validation_report,
    )


__all__ = [
    "EXCLUDED_TRANSFORMATION_DATASETS",
    "HOURLY_FACT_COLUMNS",
    "HOURLY_LOCAL_TIME_SEMANTICS",
    "LANDMARK_DIMENSION_COLUMNS",
    "SENSOR_DIMENSION_COLUMNS",
    "SENSOR_DIRECTION_TABLE_COLUMNS",
    "TRANSFORMED_HISTORICAL_DATASETS",
    "WGS84_SRID",
    "DataTransformationError",
    "HistoricalTransformation",
    "transform_canonical_sensors",
    "transform_historical_workflow",
    "transform_hourly_chunk",
    "transform_landmarks",
    "transform_sensor_directions",
]
