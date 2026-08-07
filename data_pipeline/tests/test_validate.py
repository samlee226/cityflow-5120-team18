"""Tests for structured validation of cleaned CityFlow data."""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, uuid5

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from cityflow_pipeline.validate import (
    EXCLUDED_HISTORICAL_DATASETS,
    VALIDATED_HISTORICAL_DATASETS,
    HistoricalValidationReport,
    HourlyValidationSession,
    ValidationIssue,
    ValidationReport,
    validate_canonical_sensors,
    validate_historical_workflow,
    validate_hourly_chunk,
    validate_landmarks,
    validate_sensor_directions,
)


def canonical_sensors() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "sensor_id": pd.Series([1, 2], dtype="int64"),
            "sensor_description": pd.Series(
                ["Bourke Street", "Flinders Street"], dtype="string"
            ),
            "sensor_name": pd.Series(["Sensor_A", "Sensor_B"], dtype="string"),
            "installation_date": pd.to_datetime(["2020-01-01", "2021-02-03"]),
            "note": pd.Series([pd.NA, "Reference"], dtype="string"),
            "location_type": pd.Series(["Outdoor", "Outdoor"], dtype="string"),
            "status": pd.Series(["A", "A"], dtype="string"),
            "latitude": pd.Series([-37.81, -37.82], dtype="float64"),
            "longitude": pd.Series([144.96, 144.97], dtype="float64"),
        }
    )


def sensor_directions() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "sensor_id": pd.Series([1, 1], dtype="int64"),
            "direction_config_id": pd.Series([1, 2], dtype="int64"),
            "direction_1_label": pd.Series(["North", "East"], dtype="string"),
            "direction_2_label": pd.Series(["South", "West"], dtype="string"),
        }
    )


def hourly_counts() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "source_record_id": pd.Series(["r1", "r2"], dtype="string"),
            "sensor_id": pd.Series([1, 2], dtype="int64"),
            "sensing_date": pd.to_datetime(["2024-07-15", "2024-07-15"]),
            "hour": pd.Series([10, 11], dtype="int64"),
            "direction_1_count": pd.Series([4, 6], dtype="int64"),
            "direction_2_count": pd.Series([5, 7], dtype="int64"),
            "pedestrian_count": pd.Series([9, 13], dtype="int64"),
            "sensor_name": pd.Series(["Sensor_A", "Sensor_B"], dtype="string"),
            "latitude": pd.Series([-37.81, -37.82], dtype="float64"),
            "longitude": pd.Series([144.96, 144.97], dtype="float64"),
        }
    )


def landmarks() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "landmark_id": pd.Series(
                [
                    str(uuid5(NAMESPACE_URL, "landmark-a")),
                    str(uuid5(NAMESPACE_URL, "landmark-b")),
                ],
                dtype="string",
            ),
            "name": pd.Series(["Library", "Library"], dtype="string"),
            "category": pd.Series(["Community", "Community"], dtype="string"),
            "subcategory": pd.Series(["Library", "Library"], dtype="string"),
            "latitude": pd.Series([-37.81, -37.82], dtype="float64"),
            "longitude": pd.Series([144.96, 144.97], dtype="float64"),
        }
    )


def issue_codes(report: ValidationReport) -> set[str]:
    return {issue.code for issue in report.issues}


def test_valid_canonical_sensors_pass() -> None:
    report = validate_canonical_sensors(canonical_sensors())

    assert report.passed
    assert report.error_count == 0
    assert report.metrics["unique_sensor_count"] == 2


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda frame: frame.assign(sensor_id=[1, 1]), "sensor.duplicate_id"),
        (lambda frame: frame.assign(sensor_id=[1, None]), "sensor.missing_id"),
        (lambda frame: frame.assign(latitude=[-37.81, 91.0]), "sensor.invalid_coordinate"),
        (
            lambda frame: frame.assign(sensor_description=["Bourke Street", "   "]),
            "sensor.blank_required_text",
        ),
    ],
)
def test_invalid_canonical_sensor_rules(mutator: object, code: str) -> None:
    frame = mutator(canonical_sensors())  # type: ignore[operator]

    report = validate_canonical_sensors(frame)

    assert not report.passed
    assert code in issue_codes(report)


def test_invalid_sensor_installation_date_is_reported() -> None:
    frame = canonical_sensors()
    frame["installation_date"] = frame["installation_date"].astype("object")
    frame.loc[1, "installation_date"] = "not-a-date"

    report = validate_canonical_sensors(frame)

    assert "sensor.invalid_installation_date" in issue_codes(report)
    assert "sensor.invalid_dtype" in issue_codes(report)


def test_valid_sensor_directions_pass_and_sensors_may_have_none() -> None:
    report = validate_sensor_directions(sensor_directions(), canonical_sensors())

    assert report.passed
    assert report.metrics["sensors_with_direction_configs"] == 1


def test_orphan_sensor_direction_reference_fails() -> None:
    frame = sensor_directions()
    frame.loc[1, "sensor_id"] = 999

    report = validate_sensor_directions(frame, canonical_sensors())

    assert "sensor_direction.orphan_sensor" in issue_codes(report)
    assert not report.passed


def test_duplicate_direction_business_key_and_configuration_fail() -> None:
    frame = sensor_directions()
    frame.loc[1] = frame.loc[0]

    report = validate_sensor_directions(frame, canonical_sensors())

    assert "sensor_direction.duplicate_business_key" in issue_codes(report)
    assert "sensor_direction.duplicate_configuration" in issue_codes(report)


def test_blank_direction_labels_fail() -> None:
    frame = sensor_directions().iloc[[0]].copy()
    frame.loc[0, ["direction_1_label", "direction_2_label"]] = [pd.NA, "  "]

    report = validate_sensor_directions(frame, canonical_sensors())

    assert "sensor_direction.blank_labels" in issue_codes(report)


def test_valid_hourly_chunk_passes_with_expected_metrics() -> None:
    report = validate_hourly_chunk(hourly_counts(), canonical_sensors())

    assert report.passed
    assert report.checked_row_count == 2
    assert report.metrics["chunk_count"] == 1
    assert report.metrics["unique_sensor_count"] == 2
    assert report.metrics["minimum_count"] == 9
    assert report.metrics["maximum_count"] == 13


@pytest.mark.parametrize(
    ("column", "value", "code"),
    [
        ("sensing_date", "not-a-date", "hourly.invalid_sensing_date"),
        ("hour", 24, "hourly.invalid_hour"),
        ("pedestrian_count", -1, "hourly.negative_count"),
        ("pedestrian_count", 1.5, "hourly.invalid_count"),
    ],
)
def test_invalid_hourly_values(column: str, value: object, code: str) -> None:
    frame = hourly_counts().iloc[[0]].copy()
    frame[column] = frame[column].astype("object")
    frame.loc[frame.index[0], column] = value

    report = validate_hourly_chunk(frame, canonical_sensors())

    assert code in issue_codes(report)
    assert not report.passed


def test_orphan_hourly_sensor_reference_fails() -> None:
    frame = hourly_counts().iloc[[0]].copy()
    frame.loc[0, "sensor_id"] = 999

    report = validate_hourly_chunk(frame, canonical_sensors())

    assert "hourly.orphan_sensor" in issue_codes(report)
    assert report.metrics["orphan_sensor_reference_count"] == 1


def test_duplicate_hourly_business_key_within_chunk_fails() -> None:
    frame = pd.concat([hourly_counts().iloc[[0]]] * 2, ignore_index=True)
    frame.loc[1, "source_record_id"] = "different-source-id"

    report = validate_hourly_chunk(frame, canonical_sensors())

    assert "hourly.duplicate_business_key_within_chunk" in issue_codes(report)
    assert report.metrics["duplicate_key_count"] == 1


def test_duplicate_hourly_business_key_across_chunks_fails() -> None:
    first = hourly_counts().iloc[[0]].copy()
    second = first.copy()
    second.loc[0, "source_record_id"] = "r-later"
    session = HourlyValidationSession(canonical_sensors())

    assert session.validate_chunk(first).passed
    second_report = session.validate_chunk(second)
    final_report = session.finalize()

    assert "hourly.duplicate_business_key_across_chunks" in issue_codes(
        second_report
    )
    assert final_report.metrics["duplicate_key_count"] == 1
    assert final_report.metrics["chunk_count"] == 2
    assert not final_report.passed


def test_hourly_direction_sum_warning_does_not_fail() -> None:
    frame = hourly_counts().iloc[[0]].copy()
    frame.loc[0, "pedestrian_count"] = 10

    report = validate_hourly_chunk(frame, canonical_sensors())

    assert report.passed
    assert report.warning_count == 1
    assert "hourly.direction_sum_mismatch" in issue_codes(report)


def test_hourly_metadata_mismatch_is_warning() -> None:
    frame = hourly_counts().iloc[[0]].copy()
    frame.loc[0, "sensor_name"] = "Historical_Name"
    frame.loc[0, "latitude"] = -37.80

    report = validate_hourly_chunk(frame, canonical_sensors())

    assert report.passed
    assert {
        "hourly.sensor_name_mismatch",
        "hourly.sensor_coordinate_mismatch",
    }.issubset(issue_codes(report))


def test_valid_landmarks_pass() -> None:
    frame = landmarks()
    frame.loc[0, "landmark_id"] = frame.loc[0, "landmark_id"].upper()

    report = validate_landmarks(frame)

    assert report.passed
    assert report.metrics["unique_landmark_count"] == 2


def test_invalid_and_duplicate_landmark_ids_fail() -> None:
    frame = landmarks()
    frame.loc[0, "landmark_id"] = "not-a-uuid"
    frame.loc[1, "landmark_id"] = "not-a-uuid"

    report = validate_landmarks(frame)

    assert "landmark.invalid_uuid5" in issue_codes(report)
    assert "landmark.duplicate_id" in issue_codes(report)


def test_invalid_landmark_coordinate_and_duplicate_business_record_fail() -> None:
    frame = landmarks()
    frame.loc[1, ["name", "latitude", "longitude"]] = ["Library", -37.81, 144.96]
    coordinate_report = validate_landmarks(frame.assign(latitude=[-37.81, -91.0]))
    duplicate_report = validate_landmarks(frame)

    assert "landmark.invalid_coordinate" in issue_codes(coordinate_report)
    assert "landmark.duplicate_business_record" in issue_codes(duplicate_report)


def test_missing_landmark_category_is_warning_only() -> None:
    frame = landmarks()
    frame.loc[0, "subcategory"] = pd.NA

    report = validate_landmarks(frame)

    assert report.passed
    assert "landmark.missing_category" in issue_codes(report)


@pytest.mark.parametrize(
    ("validator", "frame", "args"),
    [
        (validate_canonical_sensors, canonical_sensors(), ()),
        (validate_sensor_directions, sensor_directions(), (canonical_sensors(),)),
        (validate_hourly_chunk, hourly_counts(), (canonical_sensors(),)),
        (validate_landmarks, landmarks(), ()),
    ],
)
def test_missing_required_cleaned_columns_are_reported(
    validator: object,
    frame: pd.DataFrame,
    args: tuple[object, ...],
) -> None:
    incomplete = frame.drop(columns=frame.columns[-1])

    report = validator(incomplete, *args)  # type: ignore[operator]

    assert "schema.missing_columns" in issue_codes(report)
    assert not report.passed


def test_issue_examples_are_limited_to_five() -> None:
    base = canonical_sensors().iloc[[0]].copy()
    frame = pd.concat([base] * 10, ignore_index=True)
    frame["sensor_id"] = pd.Series(range(10), dtype="int64")
    frame["sensor_name"] = pd.Series(["  "] * 10, dtype="string")

    report = validate_canonical_sensors(frame)
    issue = next(
        item for item in report.issues if item.code == "sensor.blank_required_text"
    )

    assert issue.affected_row_count == 10
    assert len(issue.examples) == 5


def test_report_to_dict_is_deterministic_and_json_serialisable() -> None:
    report = validate_hourly_chunk(hourly_counts(), canonical_sensors())

    first = report.to_dict()
    second = report.to_dict()

    assert first == second
    assert json.loads(json.dumps(first, sort_keys=True)) == first


def test_error_and_warning_pass_semantics() -> None:
    warning_report = ValidationReport(
        "warning-only",
        1,
        (ValidationIssue("warning", "sample.warning", "Warning", 1),),
    )
    error_report = ValidationReport(
        "error",
        1,
        (ValidationIssue("error", "sample.error", "Error", 1),),
    )

    assert warning_report.passed
    assert not error_report.passed


def test_validators_do_not_modify_input_dataframes() -> None:
    sensors = canonical_sensors()
    directions = sensor_directions()
    hourly = hourly_counts()
    places = landmarks()
    before = [frame.copy(deep=True) for frame in (sensors, directions, hourly, places)]

    validate_canonical_sensors(sensors)
    validate_sensor_directions(directions, sensors)
    validate_hourly_chunk(hourly, sensors)
    validate_landmarks(places)

    for actual, expected in zip((sensors, directions, hourly, places), before):
        assert_frame_equal(actual, expected)


def test_combined_historical_validation_excludes_network_and_minutely() -> None:
    report = validate_historical_workflow(
        canonical_sensors(),
        sensor_directions(),
        [hourly_counts().iloc[[0]], hourly_counts().iloc[[1]]],
        landmarks(),
    )

    assert isinstance(report, HistoricalValidationReport)
    assert report.passed
    assert tuple(item.dataset_name for item in report.reports) == (
        "canonical_sensors",
        "sensor_directions",
        "hourly_pedestrian_counts",
        "landmarks",
    )
    assert VALIDATED_HISTORICAL_DATASETS == tuple(
        item.dataset_name for item in report.reports
    )
    assert EXCLUDED_HISTORICAL_DATASETS == (
        "pedestrian_counts_minutely",
        "pedestrian_network",
    )
    assert report.to_dict()["excluded_datasets"] == [
        "pedestrian_counts_minutely",
        "pedestrian_network",
    ]


def test_hourly_session_rejects_invalid_configuration_and_post_finalize_use() -> None:
    with pytest.raises(ValueError, match="coordinate_tolerance"):
        HourlyValidationSession(canonical_sensors(), coordinate_tolerance=-1)

    session = HourlyValidationSession(canonical_sensors())
    session.validate_chunk(hourly_counts())
    assert session.finalize() is session.finalize()
    with pytest.raises(RuntimeError, match="after finalize"):
        session.validate_chunk(hourly_counts())
