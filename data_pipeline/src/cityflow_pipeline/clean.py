"""Deterministic, in-memory cleaning for extracted CityFlow data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import NAMESPACE_URL, uuid5

import numpy as np
import pandas as pd


COORDINATE_TOLERANCE: Final = 1e-7

SENSOR_SOURCE_COLUMNS: Final = (
    "Location_ID",
    "Sensor_Description",
    "Sensor_Name",
    "Installation_Date",
    "Note",
    "Location_Type",
    "Status",
    "Direction_1",
    "Direction_2",
    "Latitude",
    "Longitude",
    "Location",
)
HOURLY_SOURCE_COLUMNS: Final = (
    "ID",
    "Location_ID",
    "Sensing_Date",
    "HourDay",
    "Direction_1",
    "Direction_2",
    "Total_of_Directions",
    "Sensor_Name",
    "Location",
)
LANDMARK_SOURCE_COLUMNS: Final = (
    "Theme",
    "Sub Theme",
    "Feature Name",
    "Co-ordinates",
)

SENSOR_COLUMNS: Final = (
    "sensor_id",
    "sensor_description",
    "sensor_name",
    "installation_date",
    "note",
    "location_type",
    "status",
    "latitude",
    "longitude",
)
SENSOR_DIRECTION_COLUMNS: Final = (
    "sensor_id",
    "direction_config_id",
    "direction_1_label",
    "direction_2_label",
)
HOURLY_COLUMNS: Final = (
    "source_record_id",
    "sensor_id",
    "sensing_date",
    "hour",
    "direction_1_count",
    "direction_2_count",
    "pedestrian_count",
    "sensor_name",
    "latitude",
    "longitude",
)
LANDMARK_COLUMNS: Final = (
    "landmark_id",
    "name",
    "category",
    "subcategory",
    "latitude",
    "longitude",
)


class DataCleaningError(ValueError):
    """Raised when extracted data cannot satisfy a cleaning contract."""


@dataclass(frozen=True, slots=True)
class SensorCleaningResult:
    """Canonical sensors and their distinct direction configurations."""

    canonical_sensors: pd.DataFrame
    sensor_directions: pd.DataFrame


def _require_columns(
    frame: pd.DataFrame,
    required: tuple[str, ...],
    dataset: str,
) -> None:
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise DataCleaningError(
            f"{dataset} is missing required source columns: {missing}"
        )


def _required_text(series: pd.Series, field: str) -> pd.Series:
    cleaned = series.astype("string").str.strip()
    invalid = cleaned.isna() | cleaned.eq("")
    if invalid.any():
        rows = list(series.index[invalid][:5])
        raise DataCleaningError(f"{field} contains missing text at rows {rows}")
    return cleaned


def _nullable_text(series: pd.Series) -> pd.Series:
    cleaned = series.astype("string").str.strip()
    return cleaned.mask(cleaned.eq(""), pd.NA)


def _integers(series: pd.Series, field: str) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    invalid = values.isna() | ~np.isfinite(values) | values.mod(1).ne(0)
    if invalid.any():
        rows = list(series.index[invalid][:5])
        raise DataCleaningError(f"{field} contains invalid integers at rows {rows}")
    return values.astype("int64")


def _dates(series: pd.Series, field: str) -> pd.Series:
    text = series.astype("string").str.strip()
    values = pd.to_datetime(text, format="%Y-%m-%d", errors="coerce")
    invalid = text.isna() | text.eq("") | values.isna()
    if invalid.any():
        rows = list(series.index[invalid][:5])
        raise DataCleaningError(f"{field} contains invalid dates at rows {rows}")
    return values


def _optional_dates(series: pd.Series, field: str) -> pd.Series:
    """Parse optional ISO dates while distinguishing missing from malformed values."""

    text = series.astype("string").str.strip()
    missing = text.isna() | text.eq("")
    values = pd.to_datetime(
        text.mask(missing, pd.NA), format="%Y-%m-%d", errors="coerce"
    )
    invalid = ~missing & values.isna()
    if invalid.any():
        rows = list(series.index[invalid][:5])
        raise DataCleaningError(f"{field} contains invalid dates at rows {rows}")
    return values


def _coordinates(
    series: pd.Series,
    field: str,
) -> tuple[pd.Series, pd.Series]:
    text = series.astype("string").str.strip()
    parts = text.str.split(",", n=1, expand=True)
    if parts.shape[1] != 2:
        raise DataCleaningError(f"{field} must contain 'latitude, longitude' values")

    latitude = pd.to_numeric(parts[0].str.strip(), errors="coerce")
    longitude = pd.to_numeric(parts[1].str.strip(), errors="coerce")
    invalid = (
        text.isna()
        | latitude.isna()
        | longitude.isna()
        | ~np.isfinite(latitude)
        | ~np.isfinite(longitude)
        | ~latitude.between(-90, 90)
        | ~longitude.between(-180, 180)
    )
    if invalid.any():
        rows = list(series.index[invalid][:5])
        raise DataCleaningError(f"{field} contains invalid coordinates at rows {rows}")
    return latitude.astype("float64"), longitude.astype("float64")


def _numeric_coordinates(
    latitude_source: pd.Series,
    longitude_source: pd.Series,
) -> tuple[pd.Series, pd.Series]:
    latitude = pd.to_numeric(latitude_source, errors="coerce")
    longitude = pd.to_numeric(longitude_source, errors="coerce")
    invalid = (
        latitude.isna()
        | longitude.isna()
        | ~np.isfinite(latitude)
        | ~np.isfinite(longitude)
        | ~latitude.between(-90, 90)
        | ~longitude.between(-180, 180)
    )
    if invalid.any():
        rows = list(latitude_source.index[invalid][:5])
        raise DataCleaningError(
            f"Latitude/Longitude contain invalid coordinates at rows {rows}"
        )
    return latitude.astype("float64"), longitude.astype("float64")


def clean_pedestrian_sensors(frame: pd.DataFrame) -> SensorCleaningResult:
    """Validate sensors and split canonical metadata from direction configs."""

    _require_columns(frame, SENSOR_SOURCE_COLUMNS, "pedestrian sensors")
    source = frame.loc[:, SENSOR_SOURCE_COLUMNS].copy(deep=True)
    sensor_id = _integers(source["Location_ID"], "Location_ID")
    latitude, longitude = _numeric_coordinates(
        source["Latitude"], source["Longitude"]
    )
    location_latitude, location_longitude = _coordinates(
        source["Location"], "Location"
    )
    mismatch = (
        latitude.sub(location_latitude).abs().gt(COORDINATE_TOLERANCE)
        | longitude.sub(location_longitude).abs().gt(COORDINATE_TOLERANCE)
    )
    if mismatch.any():
        rows = list(source.index[mismatch][:5])
        raise DataCleaningError(
            f"Location does not match Latitude/Longitude at rows {rows}"
        )

    canonical_rows = pd.DataFrame(
        {
            "sensor_id": sensor_id,
            "sensor_description": _required_text(
                source["Sensor_Description"], "Sensor_Description"
            ),
            "sensor_name": _required_text(source["Sensor_Name"], "Sensor_Name"),
            "installation_date": _optional_dates(
                source["Installation_Date"], "Installation_Date"
            ),
            "note": _nullable_text(source["Note"]),
            "location_type": _required_text(
                source["Location_Type"], "Location_Type"
            ),
            "status": _required_text(source["Status"], "Status"),
            "latitude": latitude,
            "longitude": longitude,
        }
    )

    conflicts: list[str] = []
    metadata_columns = [column for column in SENSOR_COLUMNS if column != "sensor_id"]
    for duplicate_id, group in canonical_rows.groupby("sensor_id", sort=True):
        differing = [
            column
            for column in metadata_columns
            if group[column].nunique(dropna=False) > 1
        ]
        if differing:
            conflicts.append(f"sensor_id {duplicate_id}: {differing}")
    if conflicts:
        raise DataCleaningError(
            "Duplicate sensor rows contain conflicting metadata: "
            + "; ".join(conflicts[:5])
        )

    canonical = (
        canonical_rows.drop_duplicates(subset=["sensor_id"], keep="first")
        .sort_values("sensor_id", kind="mergesort")
        .reset_index(drop=True)
        .loc[:, SENSOR_COLUMNS]
    )

    directions = pd.DataFrame(
        {
            "sensor_id": sensor_id,
            "direction_1_label": _nullable_text(source["Direction_1"]),
            "direction_2_label": _nullable_text(source["Direction_2"]),
        }
    )
    directions = directions.loc[
        directions["direction_1_label"].notna()
        | directions["direction_2_label"].notna()
    ].drop_duplicates()
    directions = directions.assign(
        _direction_1_sort=directions["direction_1_label"].fillna(""),
        _direction_2_sort=directions["direction_2_label"].fillna(""),
    ).sort_values(
        ["sensor_id", "_direction_1_sort", "_direction_2_sort"],
        kind="mergesort",
    )
    directions.insert(
        1,
        "direction_config_id",
        directions.groupby("sensor_id", sort=False).cumcount().add(1).astype("int64"),
    )
    directions = directions.reset_index(drop=True).loc[:, SENSOR_DIRECTION_COLUMNS]

    return SensorCleaningResult(canonical, directions)


def clean_pedestrian_counts_hourly(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate and clean one extracted hourly-count chunk without aggregation."""

    _require_columns(frame, HOURLY_SOURCE_COLUMNS, "hourly pedestrian counts")
    source = frame.loc[:, HOURLY_SOURCE_COLUMNS].copy(deep=True)
    source_record_id = _required_text(source["ID"], "ID")
    sensor_id = _integers(source["Location_ID"], "Location_ID")
    hour = _integers(source["HourDay"], "HourDay")
    direction_1 = _integers(source["Direction_1"], "Direction_1")
    direction_2 = _integers(source["Direction_2"], "Direction_2")
    total = _integers(source["Total_of_Directions"], "Total_of_Directions")

    invalid_hour = ~hour.between(0, 23)
    if invalid_hour.any():
        rows = list(source.index[invalid_hour][:5])
        raise DataCleaningError(f"HourDay must be between 0 and 23 at rows {rows}")
    negative = direction_1.lt(0) | direction_2.lt(0) | total.lt(0)
    if negative.any():
        rows = list(source.index[negative][:5])
        raise DataCleaningError(f"Pedestrian counts cannot be negative at rows {rows}")
    inconsistent = direction_1.add(direction_2).ne(total)
    if inconsistent.any():
        rows = list(source.index[inconsistent][:5])
        raise DataCleaningError(
            f"Direction counts do not equal Total_of_Directions at rows {rows}"
        )

    latitude, longitude = _coordinates(source["Location"], "Location")
    cleaned = pd.DataFrame(
        {
            "source_record_id": source_record_id,
            "sensor_id": sensor_id,
            "sensing_date": _dates(source["Sensing_Date"], "Sensing_Date"),
            "hour": hour,
            "direction_1_count": direction_1,
            "direction_2_count": direction_2,
            "pedestrian_count": total,
            "sensor_name": _required_text(source["Sensor_Name"], "Sensor_Name"),
            "latitude": latitude,
            "longitude": longitude,
        }
    )
    return cleaned.reset_index(drop=True).loc[:, HOURLY_COLUMNS]


def clean_landmarks(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate landmarks and assign stable identity from name and coordinates."""

    _require_columns(frame, LANDMARK_SOURCE_COLUMNS, "landmarks")
    source = frame.loc[:, LANDMARK_SOURCE_COLUMNS].copy(deep=True)
    name = _required_text(source["Feature Name"], "Feature Name")
    category = _required_text(source["Theme"], "Theme")
    subcategory = _required_text(source["Sub Theme"], "Sub Theme")
    latitude, longitude = _coordinates(source["Co-ordinates"], "Co-ordinates")

    identities = [
        str(
            uuid5(
                NAMESPACE_URL,
                f"cityflow-landmark|{item_name}|{item_latitude:.17g}|"
                f"{item_longitude:.17g}",
            )
        )
        for item_name, item_latitude, item_longitude in zip(
            name, latitude, longitude, strict=True
        )
    ]
    cleaned = pd.DataFrame(
        {
            "landmark_id": pd.Series(identities, index=source.index, dtype="string"),
            "name": name,
            "category": category,
            "subcategory": subcategory,
            "latitude": latitude,
            "longitude": longitude,
        }
    )
    if cleaned["landmark_id"].duplicated().any():
        raise DataCleaningError(
            "Landmark identity collision: name and coordinates must identify one row"
        )
    return cleaned.reset_index(drop=True).loc[:, LANDMARK_COLUMNS]


__all__ = [
    "COORDINATE_TOLERANCE",
    "HOURLY_COLUMNS",
    "LANDMARK_COLUMNS",
    "SENSOR_COLUMNS",
    "SENSOR_DIRECTION_COLUMNS",
    "DataCleaningError",
    "SensorCleaningResult",
    "clean_landmarks",
    "clean_pedestrian_counts_hourly",
    "clean_pedestrian_sensors",
]
