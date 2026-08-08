"""Tests for deterministic, in-memory CityFlow cleaning."""

from __future__ import annotations

from uuid import UUID

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from cityflow_pipeline.clean import (
    HOURLY_COLUMNS,
    LANDMARK_COLUMNS,
    SENSOR_COLUMNS,
    SENSOR_DIRECTION_COLUMNS,
    DataCleaningError,
    clean_landmarks,
    clean_pedestrian_counts_hourly,
    clean_pedestrian_sensors,
)


def sensor_frame() -> pd.DataFrame:
    """Return representative extracted sensor rows."""

    return pd.DataFrame(
        {
            "Location_ID": [2, 1, 1, 1],
            "Sensor_Description": [" Second ", " First ", " First ", " First "],
            "Sensor_Name": ["Sensor 2", " Sensor 1 ", " Sensor 1 ", " Sensor 1 "],
            "Installation_Date": [
                "2020-02-29",
                "2009-03-24",
                "2009-03-24",
                "2009-03-24",
            ],
            "Note": [None, " note ", " note ", " note "],
            "Location_Type": ["Outdoor", "Indoor", "Indoor", "Indoor"],
            "Status": ["A", "A", "A", "A"],
            "Direction_1": [None, " North ", "South", " North "],
            "Direction_2": [None, "South", "North", "South"],
            "Latitude": [-37.82, -37.81, -37.81, -37.81],
            "Longitude": [144.97, 144.96, 144.96, 144.96],
            "Location": [
                "-37.82, 144.97",
                "-37.81, 144.96",
                "-37.81, 144.96",
                "-37.81, 144.96",
            ],
        }
    )


def hourly_frame() -> pd.DataFrame:
    """Return representative extracted hourly rows."""

    return pd.DataFrame(
        {
            "ID": ["0001", "0002"],
            "Location_ID": [44, 44],
            "Sensing_Date": ["2024-07-15", "2024-07-15"],
            "HourDay": [0, 23],
            "Direction_1": [0, 1_000_000],
            "Direction_2": [0, 2_000_000],
            "Total_of_Directions": [0, 3_000_000],
            "Sensor_Name": [" UM3_T ", "UM3_T"],
            "Location": [
                "-37.79698741, 144.96441306",
                "-37.79698741, 144.96441306",
            ],
        }
    )


def landmark_frame() -> pd.DataFrame:
    """Return representative extracted landmark rows with repeated names."""

    return pd.DataFrame(
        {
            "Theme": [" Leisure/Recreation ", "Leisure/Recreation"],
            "Sub Theme": [" Sports ", "Sports"],
            "Feature Name": [" Shared Name ", "Shared Name"],
            "Co-ordinates": [
                "-37.7840864379557, 144.961967841559",
                "-37.785, 144.962",
            ],
        }
    )


def test_clean_sensors_returns_canonical_and_direction_tables() -> None:
    result = clean_pedestrian_sensors(sensor_frame())

    assert tuple(result.canonical_sensors.columns) == SENSOR_COLUMNS
    assert tuple(result.sensor_directions.columns) == SENSOR_DIRECTION_COLUMNS
    assert result.canonical_sensors["sensor_id"].tolist() == [1, 2]
    assert result.canonical_sensors.loc[0, "sensor_description"] == "First"
    assert result.canonical_sensors.loc[0, "note"] == "note"
    assert pd.isna(result.canonical_sensors.loc[1, "note"])
    assert result.sensor_directions["sensor_id"].tolist() == [1, 1]
    assert result.sensor_directions["direction_config_id"].tolist() == [1, 2]
    assert result.sensor_directions["direction_1_label"].tolist() == ["North", "South"]


def test_clean_sensors_is_deterministic_for_shuffled_input() -> None:
    original = clean_pedestrian_sensors(sensor_frame())
    shuffled = clean_pedestrian_sensors(
        sensor_frame().sample(frac=1, random_state=5120).reset_index(drop=True)
    )

    assert_frame_equal(original.canonical_sensors, shuffled.canonical_sensors)
    assert_frame_equal(original.sensor_directions, shuffled.sensor_directions)


def test_clean_sensors_omits_only_fully_missing_direction() -> None:
    frame = sensor_frame().iloc[[0]].copy()
    frame.loc[0, "Direction_1"] = "Inbound"

    directions = clean_pedestrian_sensors(frame).sensor_directions

    assert len(directions) == 1
    assert directions.loc[0, "direction_1_label"] == "Inbound"
    assert pd.isna(directions.loc[0, "direction_2_label"])


def test_clean_sensors_rejects_conflicting_duplicate_metadata() -> None:
    frame = sensor_frame()
    frame.loc[2, "Status"] = "Inactive"

    with pytest.raises(DataCleaningError, match="conflicting metadata.*status"):
        clean_pedestrian_sensors(frame)


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("Location_ID", "1.5", "invalid integers"),
        ("Installation_Date", "2023-02-29", "invalid dates"),
        ("Latitude", 91, "invalid coordinates"),
        ("Location", "not coordinates", "latitude, longitude"),
    ],
)
def test_clean_sensors_rejects_invalid_values(
    column: str,
    value: object,
    message: str,
) -> None:
    frame = sensor_frame().iloc[[0]].copy()
    frame[column] = frame[column].astype("object")
    frame.loc[frame.index[0], column] = value

    with pytest.raises(DataCleaningError, match=message):
        clean_pedestrian_sensors(frame)


def test_clean_sensors_rejects_location_coordinate_mismatch() -> None:
    frame = sensor_frame().iloc[[0]].copy()
    frame.loc[frame.index[0], "Location"] = "-37.80, 144.97"

    with pytest.raises(DataCleaningError, match="does not match"):
        clean_pedestrian_sensors(frame)


def test_clean_sensors_does_not_mutate_input() -> None:
    frame = sensor_frame()
    before = frame.copy(deep=True)

    clean_pedestrian_sensors(frame)

    assert_frame_equal(frame, before)


def test_clean_hourly_returns_typed_canonical_rows_without_aggregation() -> None:
    cleaned = clean_pedestrian_counts_hourly(hourly_frame())

    assert tuple(cleaned.columns) == HOURLY_COLUMNS
    assert len(cleaned) == 2
    assert cleaned["source_record_id"].tolist() == ["0001", "0002"]
    assert cleaned["hour"].tolist() == [0, 23]
    assert cleaned["pedestrian_count"].tolist() == [0, 3_000_000]
    assert cleaned["sensor_name"].tolist() == ["UM3_T", "UM3_T"]
    assert pd.api.types.is_integer_dtype(cleaned["sensor_id"])
    assert pd.api.types.is_datetime64_any_dtype(cleaned["sensing_date"])


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("Sensing_Date", "15/07/2024", "invalid dates"),
        ("HourDay", 24, "between 0 and 23"),
        ("HourDay", 1.5, "invalid integers"),
        ("Direction_1", -1, "cannot be negative"),
        ("Location", "-91, 144", "invalid coordinates"),
    ],
)
def test_clean_hourly_rejects_invalid_values(
    column: str,
    value: object,
    message: str,
) -> None:
    frame = hourly_frame().iloc[[0]].copy()
    frame[column] = frame[column].astype("object")
    frame.loc[frame.index[0], column] = value
    if column == "Direction_1":
        frame.loc[frame.index[0], "Total_of_Directions"] = value

    with pytest.raises(DataCleaningError, match=message):
        clean_pedestrian_counts_hourly(frame)


def test_clean_hourly_rejects_inconsistent_direction_total() -> None:
    frame = hourly_frame().iloc[[0]].copy()
    frame.loc[frame.index[0], "Total_of_Directions"] = 1

    with pytest.raises(DataCleaningError, match="do not equal"):
        clean_pedestrian_counts_hourly(frame)


def test_clean_hourly_does_not_mutate_input() -> None:
    frame = hourly_frame()
    before = frame.copy(deep=True)

    clean_pedestrian_counts_hourly(frame)

    assert_frame_equal(frame, before)


def test_clean_landmarks_returns_canonical_stable_uuid5_ids() -> None:
    frame = landmark_frame()
    first = clean_landmarks(frame)
    second = clean_landmarks(frame.copy(deep=True))

    assert tuple(first.columns) == LANDMARK_COLUMNS
    assert len(first) == 2
    assert first["name"].tolist() == ["Shared Name", "Shared Name"]
    assert first["landmark_id"].nunique() == 2
    assert [UUID(value).version for value in first["landmark_id"]] == [5, 5]
    assert_frame_equal(first, second)


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("Feature Name", "   ", "missing text"),
        ("Co-ordinates", "-37.8", "latitude, longitude"),
        ("Co-ordinates", "-37.8, 181", "invalid coordinates"),
    ],
)
def test_clean_landmarks_rejects_invalid_values(
    column: str,
    value: object,
    message: str,
) -> None:
    frame = landmark_frame().iloc[[0]].copy()
    frame.loc[frame.index[0], column] = value

    with pytest.raises(DataCleaningError, match=message):
        clean_landmarks(frame)


def test_clean_landmarks_does_not_mutate_input() -> None:
    frame = landmark_frame()
    before = frame.copy(deep=True)

    clean_landmarks(frame)

    assert_frame_equal(frame, before)


@pytest.mark.parametrize(
    ("cleaner", "frame"),
    [
        (clean_pedestrian_sensors, sensor_frame()),
        (clean_pedestrian_counts_hourly, hourly_frame()),
        (clean_landmarks, landmark_frame()),
    ],
)
def test_cleaners_reject_missing_source_columns(cleaner: object, frame: pd.DataFrame) -> None:
    incomplete = frame.drop(columns=frame.columns[-1])

    with pytest.raises(DataCleaningError, match="missing required source columns"):
        cleaner(incomplete)  # type: ignore[operator]
