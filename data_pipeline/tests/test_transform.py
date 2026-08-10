"""Tests for pure database-ready CityFlow transformations."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from cityflow_pipeline.transform import (
    EXCLUDED_TRANSFORMATION_DATASETS,
    HOURLY_FACT_COLUMNS,
    HOURLY_LOCAL_TIME_SEMANTICS,
    LANDMARK_DIMENSION_COLUMNS,
    SENSOR_DIMENSION_COLUMNS,
    SENSOR_DIRECTION_TABLE_COLUMNS,
    TRANSFORMED_HISTORICAL_DATASETS,
    WGS84_SRID,
    DataTransformationError,
    HistoricalTransformation,
    transform_canonical_sensors,
    transform_historical_workflow,
    transform_hourly_chunk,
    transform_landmarks,
    transform_sensor_directions,
)
from cityflow_pipeline.validate import (
    HistoricalValidationReport,
    ValidationIssue,
    ValidationReport,
)


def canonical_sensors() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "sensor_id": pd.Series([2, 1], dtype="int64"),
            "sensor_description": pd.Series(
                ["Flinders Street", "Bourke Street"], dtype="string"
            ),
            "sensor_name": pd.Series(["Sensor_B", "Sensor_A"], dtype="string"),
            "installation_date": pd.to_datetime(["2021-02-03", "2020-01-01"]),
            "note": pd.Series(["Reference", pd.NA], dtype="string"),
            "location_type": pd.Series(["Outdoor", "Outdoor"], dtype="string"),
            "status": pd.Series(["A", "A"], dtype="string"),
            "latitude": pd.Series([-37.82, -37.81], dtype="float64"),
            "longitude": pd.Series([144.97, 144.96], dtype="float64"),
        }
    )


def sensor_directions() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "sensor_id": pd.Series([2, 1, 1], dtype="int64"),
            "direction_config_id": pd.Series([1, 2, 1], dtype="int64"),
            "direction_1_label": pd.Series(
                ["Inbound", "East", "North"], dtype="string"
            ),
            "direction_2_label": pd.Series(
                ["Outbound", "West", "South"], dtype="string"
            ),
        }
    )


def hourly_counts() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "source_record_id": pd.Series(["r2", "r1"], dtype="string"),
            "sensor_id": pd.Series([2, 1], dtype="int64"),
            "sensing_date": pd.to_datetime(["2024-07-21", "2024-07-15"]),
            "hour": pd.Series([23, 10], dtype="int64"),
            "direction_1_count": pd.Series([6, 4], dtype="int64"),
            "direction_2_count": pd.Series([7, 5], dtype="int64"),
            "pedestrian_count": pd.Series([13, 9], dtype="int64"),
            "sensor_name": pd.Series(["Sensor_B", "Sensor_A"], dtype="string"),
            "latitude": pd.Series([-37.82, -37.81], dtype="float64"),
            "longitude": pd.Series([144.97, 144.96], dtype="float64"),
        }
    )


def landmarks() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "landmark_id": pd.Series(
                [
                    str(uuid5(NAMESPACE_URL, "landmark-b")),
                    str(uuid5(NAMESPACE_URL, "landmark-a")),
                ],
                dtype="string",
            ),
            "name": pd.Series(["Station", "Library"], dtype="string"),
            "category": pd.Series(["Transport", "Community"], dtype="string"),
            "subcategory": pd.Series(["Rail", "Library"], dtype="string"),
            "latitude": pd.Series([-37.82, -37.81], dtype="float64"),
            "longitude": pd.Series([144.97, 144.96], dtype="float64"),
        }
    )


def passed_validation_report(*, warning: bool = False) -> HistoricalValidationReport:
    issues = (
        (ValidationIssue("warning", "test.warning", "Test warning", 1),)
        if warning
        else ()
    )
    return HistoricalValidationReport(
        (ValidationReport("historical_test", 2, issues),)
    )


def failed_validation_report() -> HistoricalValidationReport:
    return HistoricalValidationReport(
        (
            ValidationReport(
                "historical_test",
                2,
                (ValidationIssue("error", "test.error", "Test error", 1),),
            ),
        )
    )


def test_transform_canonical_sensors_returns_database_ready_dimension() -> None:
    source = canonical_sensors()

    transformed = transform_canonical_sensors(source)

    assert tuple(transformed.columns) == SENSOR_DIMENSION_COLUMNS
    assert len(transformed) == len(source)
    assert transformed["sensor_id"].tolist() == [1, 2]
    assert transformed.index.tolist() == [0, 1]
    assert transformed["sensor_id"].is_unique
    assert WGS84_SRID == 4326


def test_transform_canonical_sensors_preserves_missing_installation_date() -> None:
    source = canonical_sensors()
    source.loc[source["sensor_id"].eq(1), "installation_date"] = pd.NaT
    before = source.copy(deep=True)

    transformed = transform_canonical_sensors(source)

    assert pd.isna(
        transformed.loc[transformed["sensor_id"].eq(1), "installation_date"].iloc[0]
    )
    assert_frame_equal(source, before)


def test_sensor_wkt_uses_longitude_before_latitude() -> None:
    transformed = transform_canonical_sensors(canonical_sensors())

    assert transformed.loc[0, "geometry_wkt"] == "POINT(144.96 -37.81)"
    assert transformed.loc[1, "geometry_wkt"] == "POINT(144.97 -37.82)"


def test_transform_sensor_directions_preserves_business_keys_and_fk() -> None:
    source = sensor_directions()

    transformed = transform_sensor_directions(source, canonical_sensors())

    assert tuple(transformed.columns) == SENSOR_DIRECTION_TABLE_COLUMNS
    assert len(transformed) == len(source)
    assert list(
        transformed[["sensor_id", "direction_config_id"]].itertuples(
            index=False, name=None
        )
    ) == [(1, 1), (1, 2), (2, 1)]
    assert set(transformed["sensor_id"]) <= set(canonical_sensors()["sensor_id"])


def test_direction_transform_rejects_orphan_and_duplicate_keys() -> None:
    orphan = sensor_directions().iloc[[0]].copy()
    orphan.loc[orphan.index[0], "sensor_id"] = 999
    duplicate = pd.concat(
        [sensor_directions().iloc[[0]]] * 2, ignore_index=True
    )

    with pytest.raises(DataTransformationError, match="orphan"):
        transform_sensor_directions(orphan, canonical_sensors())
    with pytest.raises(DataTransformationError, match="unique business key"):
        transform_sensor_directions(duplicate, canonical_sensors())


def test_transform_hourly_chunk_returns_fact_schema_and_preserves_rows() -> None:
    source = hourly_counts()

    transformed = transform_hourly_chunk(source, canonical_sensors())

    assert tuple(transformed.columns) == HOURLY_FACT_COLUMNS
    assert len(transformed) == len(source)
    assert transformed["source_record_id"].tolist() == source[
        "source_record_id"
    ].tolist()
    assert transformed["sensor_id"].tolist() == source["sensor_id"].tolist()
    assert transformed.index.tolist() == [0, 1]
    assert {"sensor_name", "latitude", "longitude"}.isdisjoint(
        transformed.columns
    )


def test_hourly_calendar_fields_use_naive_melbourne_local_time() -> None:
    transformed = transform_hourly_chunk(hourly_counts(), canonical_sensors())

    assert transformed["local_observation_datetime"].tolist() == [
        pd.Timestamp("2024-07-21 23:00:00"),
        pd.Timestamp("2024-07-15 10:00:00"),
    ]
    assert transformed["local_observation_datetime"].dt.tz is None
    assert transformed["iso_weekday"].tolist() == [7, 1]
    assert transformed["weekday_name"].tolist() == ["Sunday", "Monday"]
    assert transformed["is_weekend"].tolist() == [True, False]
    assert transformed["year"].tolist() == [2024, 2024]
    assert transformed["month"].tolist() == [7, 7]
    assert "Australia/Melbourne" in HOURLY_LOCAL_TIME_SEMANTICS
    assert "timezone-naive" in HOURLY_LOCAL_TIME_SEMANTICS


def test_hourly_counts_source_ids_and_business_keys_are_unchanged() -> None:
    source = hourly_counts()
    transformed = transform_hourly_chunk(source, canonical_sensors())

    for column in (
        "source_record_id",
        "sensor_id",
        "sensing_date",
        "hour",
        "direction_1_count",
        "direction_2_count",
        "pedestrian_count",
    ):
        pd.testing.assert_series_equal(
            transformed[column], source[column].reset_index(drop=True), check_names=True
        )


def test_hourly_transform_rejects_orphan_sensor() -> None:
    source = hourly_counts().iloc[[0]].copy()
    source.loc[source.index[0], "sensor_id"] = 999

    with pytest.raises(DataTransformationError, match="orphan"):
        transform_hourly_chunk(source, canonical_sensors())


def test_transform_landmarks_preserves_ids_and_adds_correct_wkt() -> None:
    source = landmarks()

    transformed = transform_landmarks(source)

    assert tuple(transformed.columns) == LANDMARK_DIMENSION_COLUMNS
    assert len(transformed) == len(source)
    assert set(transformed["landmark_id"]) == set(source["landmark_id"])
    assert transformed["landmark_id"].is_unique
    expected_wkt = {
        source.loc[index, "landmark_id"]: (
            f"POINT({float(source.loc[index, 'longitude'])!r} "
            f"{float(source.loc[index, 'latitude'])!r})"
        )
        for index in source.index
    }
    assert dict(
        zip(
            transformed["landmark_id"],
            transformed["geometry_wkt"],
            strict=True,
        )
    ) == expected_wkt


def test_dimension_outputs_are_deterministically_sorted() -> None:
    sensors = canonical_sensors()
    directions = sensor_directions()
    places = landmarks()

    assert_frame_equal(
        transform_canonical_sensors(sensors),
        transform_canonical_sensors(
            sensors.sample(frac=1, random_state=5120).reset_index(drop=True)
        ),
    )
    assert_frame_equal(
        transform_sensor_directions(directions, sensors),
        transform_sensor_directions(
            directions.sample(frac=1, random_state=5120).reset_index(drop=True),
            sensors,
        ),
    )
    assert_frame_equal(
        transform_landmarks(places),
        transform_landmarks(
            places.sample(frac=1, random_state=5120).reset_index(drop=True)
        ),
    )


@pytest.mark.parametrize(
    ("transformer", "frame", "args"),
    [
        (transform_canonical_sensors, canonical_sensors(), ()),
        (
            transform_sensor_directions,
            sensor_directions(),
            (canonical_sensors(),),
        ),
        (transform_hourly_chunk, hourly_counts(), (canonical_sensors(),)),
        (transform_landmarks, landmarks(), ()),
    ],
)
def test_transformers_reject_missing_required_cleaned_columns(
    transformer: object,
    frame: pd.DataFrame,
    args: tuple[object, ...],
) -> None:
    incomplete = frame.drop(columns=frame.columns[-1])

    with pytest.raises(DataTransformationError, match="missing required"):
        transformer(incomplete, *args)  # type: ignore[operator]


def test_failed_validation_report_blocks_historical_transformation() -> None:
    with pytest.raises(DataTransformationError, match="passed validation"):
        transform_historical_workflow(
            canonical_sensors(),
            sensor_directions(),
            [hourly_counts()],
            landmarks(),
            failed_validation_report(),
        )


def test_warning_report_proceeds_and_remains_visible() -> None:
    validation_report = passed_validation_report(warning=True)

    transformed = transform_historical_workflow(
        canonical_sensors(),
        sensor_directions(),
        [hourly_counts()],
        landmarks(),
        validation_report,
    )

    assert transformed.validation_report is validation_report
    assert transformed.validation_report.warning_count == 1
    assert len(list(transformed.hourly_chunks)) == 1


def test_historical_workflow_keeps_hourly_transformation_lazy_and_chunked() -> None:
    consumed: list[int] = []

    def source_chunks() -> Iterator[pd.DataFrame]:
        for index, chunk in enumerate(
            [hourly_counts().iloc[[0]], hourly_counts().iloc[[1]]], start=1
        ):
            consumed.append(index)
            yield chunk

    transformed = transform_historical_workflow(
        canonical_sensors(),
        sensor_directions(),
        source_chunks(),
        landmarks(),
        passed_validation_report(),
    )

    assert isinstance(transformed, HistoricalTransformation)
    assert consumed == []
    chunks = list(transformed.hourly_chunks)
    assert consumed == [1, 2]
    assert [len(chunk) for chunk in chunks] == [1, 1]
    assert all(tuple(chunk.columns) == HOURLY_FACT_COLUMNS for chunk in chunks)


def test_historical_workflow_excludes_minutely_and_network() -> None:
    assert TRANSFORMED_HISTORICAL_DATASETS == (
        "canonical_sensors",
        "sensor_directions",
        "hourly_pedestrian_counts",
        "landmarks",
    )
    assert EXCLUDED_TRANSFORMATION_DATASETS == (
        "pedestrian_counts_minutely",
        "pedestrian_network",
    )


def test_transformers_do_not_modify_inputs() -> None:
    sensors = canonical_sensors()
    directions = sensor_directions()
    hourly = hourly_counts()
    places = landmarks()
    before = [frame.copy(deep=True) for frame in (sensors, directions, hourly, places)]

    transform_canonical_sensors(sensors)
    transform_sensor_directions(directions, sensors)
    transform_hourly_chunk(hourly, sensors)
    transform_landmarks(places)

    for actual, expected in zip((sensors, directions, hourly, places), before):
        assert_frame_equal(actual, expected)


def test_repeated_hourly_transformation_is_deterministic() -> None:
    first = transform_hourly_chunk(hourly_counts(), canonical_sensors())
    second = transform_hourly_chunk(hourly_counts(), canonical_sensors())

    assert_frame_equal(first, second)


def test_transformations_have_no_file_side_effects(tmp_path: Path) -> None:
    transform_canonical_sensors(canonical_sensors())
    transform_sensor_directions(sensor_directions(), canonical_sensors())
    transform_hourly_chunk(hourly_counts(), canonical_sensors())
    transform_landmarks(landmarks())

    assert list(tmp_path.iterdir()) == []
