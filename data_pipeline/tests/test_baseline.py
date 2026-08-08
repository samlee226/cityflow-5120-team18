"""Tests for chunk-based historical crowd baselines and feature engineering."""

from __future__ import annotations

from collections.abc import Iterable
import math
from pathlib import Path

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from cityflow_pipeline.baseline import (
    BASELINE_COLUMNS,
    BASELINE_KEY_COLUMNS,
    ENRICHED_FEATURE_COLUMNS,
    EXCLUDED_FEATURE_DATASETS,
    QUANTILE_INTERPOLATION,
    CrowdFeatureConfig,
    FeatureEngineeringError,
    HistoricalBaselineAccumulator,
    add_crowd_features,
    build_historical_baseline,
    engineer_historical_features,
)
from cityflow_pipeline.transform import HOURLY_FACT_COLUMNS


HourlyRow = tuple[str, int, str, int, int]


def make_hourly(rows: Iterable[HourlyRow]) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    for source_id, sensor_id, date_text, hour, pedestrian_count in rows:
        sensing_date = pd.Timestamp(date_text)
        local_datetime = sensing_date + pd.Timedelta(hour, unit="h")
        records.append(
            {
                "source_record_id": source_id,
                "sensor_id": sensor_id,
                "sensing_date": sensing_date,
                "hour": hour,
                "local_observation_datetime": local_datetime,
                "year": local_datetime.year,
                "month": local_datetime.month,
                "iso_weekday": local_datetime.isoweekday(),
                "weekday_name": local_datetime.day_name(),
                "is_weekend": local_datetime.isoweekday() >= 6,
                "direction_1_count": pedestrian_count // 2,
                "direction_2_count": pedestrian_count - pedestrian_count // 2,
                "pedestrian_count": pedestrian_count,
            }
        )
    return pd.DataFrame(records, columns=HOURLY_FACT_COLUMNS)


def monday_values(values: list[int], *, sensor_id: int = 1, hour: int = 8) -> pd.DataFrame:
    rows = [
        (
            f"record-{index}",
            sensor_id,
            str(pd.Timestamp("2024-01-01") + pd.Timedelta(index * 7, unit="days")),
            hour,
            value,
        )
        for index, value in enumerate(values)
    ]
    return make_hourly(rows)


def test_baseline_groups_across_chunks_with_exact_statistics() -> None:
    first = monday_values([0, 10])
    second = monday_values([20, 30]).assign(
        source_record_id=["record-2", "record-3"],
        sensing_date=[pd.Timestamp("2024-01-15"), pd.Timestamp("2024-01-22")],
        local_observation_datetime=[
            pd.Timestamp("2024-01-15 08:00"),
            pd.Timestamp("2024-01-22 08:00"),
        ],
    )

    baseline = build_historical_baseline([first, second])
    row = baseline.iloc[0]

    assert len(baseline) == 1
    assert row["observation_count"] == 4
    assert row["minimum_sensing_date"] == pd.Timestamp("2024-01-01")
    assert row["maximum_sensing_date"] == pd.Timestamp("2024-01-22")
    assert row["minimum_pedestrian_count"] == 0
    assert row["maximum_pedestrian_count"] == 30
    assert row["mean_pedestrian_count"] == pytest.approx(15.0)
    assert row["median_pedestrian_count"] == pytest.approx(15.0)
    assert row["population_std_pedestrian_count"] == pytest.approx(math.sqrt(125))
    assert row["percentile_25_pedestrian_count"] == pytest.approx(7.5)
    assert row["percentile_75_pedestrian_count"] == pytest.approx(22.5)
    assert row["percentile_90_pedestrian_count"] == pytest.approx(27.0)
    assert QUANTILE_INTERPOLATION == "linear"


def test_baseline_has_exact_schema_unique_key_and_stable_sorting() -> None:
    frame = pd.concat(
        [
            monday_values([20], sensor_id=2, hour=9),
            monday_values([10], sensor_id=1, hour=9),
            monday_values([5], sensor_id=1, hour=8),
        ],
        ignore_index=True,
    )

    baseline = build_historical_baseline([frame])

    assert tuple(baseline.columns) == BASELINE_COLUMNS
    assert not baseline.duplicated(subset=list(BASELINE_KEY_COLUMNS)).any()
    assert list(zip(baseline.sensor_id, baseline.hour, strict=True)) == [
        (1, 8),
        (1, 9),
        (2, 9),
    ]


def test_baseline_is_deterministic() -> None:
    chunks = [monday_values([3, 1]), monday_values([4, 2])]

    assert_frame_equal(
        build_historical_baseline(chunks),
        build_historical_baseline(chunks),
    )


def test_accumulator_reports_compact_retained_state() -> None:
    accumulator = HistoricalBaselineAccumulator()
    accumulator.update(monday_values([1, 2, 3]))

    assert accumulator.row_count == 3
    assert accumulator.chunk_count == 1
    assert accumulator.group_count == 1
    assert accumulator.retained_value_count == 3
    assert accumulator.approximate_retained_bytes > 3 * 8


def test_empty_input_cannot_build_baseline() -> None:
    with pytest.raises(FeatureEngineeringError, match="without observations"):
        build_historical_baseline([])


def test_missing_transformed_column_is_rejected() -> None:
    frame = monday_values([1]).drop(columns="iso_weekday")

    with pytest.raises(FeatureEngineeringError, match="missing transformed columns"):
        build_historical_baseline([frame])


def test_enrichment_preserves_original_rows_and_adds_expected_schema() -> None:
    history = monday_values([0, 10, 20, 30])
    baseline = build_historical_baseline([history])
    current = monday_values([0, 15, 30])
    before = current.copy(deep=True)

    enriched = add_crowd_features(current, baseline)

    assert_frame_equal(current, before)
    assert_frame_equal(enriched.loc[:, HOURLY_FACT_COLUMNS], current)
    assert tuple(enriched.columns) == (*HOURLY_FACT_COLUMNS, *ENRICHED_FEATURE_COLUMNS)
    assert enriched["baseline_observation_count"].tolist() == [4, 4, 4]
    assert enriched["difference_from_median"].tolist() == [-15.0, 0.0, 15.0]
    assert enriched["crowd_ratio"].tolist() == [0.0, 1.0, 2.0]
    assert enriched["z_score"].tolist() == pytest.approx(
        [-15 / math.sqrt(125), 0.0, 15 / math.sqrt(125)]
    )
    assert enriched["crowd_level"].tolist() == ["low", "typical", "high"]
    assert not enriched["baseline_missing"].any()


def test_percentage_difference_from_median() -> None:
    baseline = build_historical_baseline([monday_values([10, 20, 30])])
    enriched = add_crowd_features(monday_values([30]), baseline)

    assert enriched.loc[0, "difference_from_median"] == pytest.approx(10.0)
    assert enriched.loc[0, "percentage_difference_from_median"] == pytest.approx(50.0)


def test_z_score_uses_baseline_mean_and_population_standard_deviation() -> None:
    baseline = build_historical_baseline([monday_values([0, 0, 9])])
    enriched = add_crowd_features(monday_values([9]), baseline)

    assert enriched.loc[0, "baseline_mean"] == pytest.approx(3.0)
    assert enriched.loc[0, "z_score"] == pytest.approx(6 / math.sqrt(18))


def test_equal_percentiles_are_classified_deterministically() -> None:
    baseline = build_historical_baseline([monday_values([5, 5, 5])])
    enriched = add_crowd_features(monday_values([4, 5, 6]), baseline)

    assert enriched["crowd_level"].tolist() == ["low", "typical", "high"]


def test_zero_median_produces_null_ratios_without_infinity() -> None:
    baseline = build_historical_baseline([monday_values([0, 0, 10])])
    enriched = add_crowd_features(monday_values([0]), baseline)

    assert pd.isna(enriched.loc[0, "crowd_ratio"])
    assert pd.isna(enriched.loc[0, "percentage_difference_from_median"])
    assert enriched.loc[0, "crowd_level"] == "typical"
    assert not math.isinf(enriched.loc[0, "z_score"])


def test_zero_standard_deviation_has_zero_z_score() -> None:
    baseline = build_historical_baseline([monday_values([5, 5])])
    enriched = add_crowd_features(monday_values([5]), baseline)

    assert enriched.loc[0, "baseline_standard_deviation"] == 0.0
    assert enriched.loc[0, "z_score"] == 0.0


def test_missing_baseline_preserves_row_without_global_fallback() -> None:
    baseline = build_historical_baseline([monday_values([5], sensor_id=1)])
    current = monday_values([7], sensor_id=999)

    enriched = add_crowd_features(current, baseline)

    assert len(enriched) == 1
    assert enriched.loc[0, "sensor_id"] == 999
    assert bool(enriched.loc[0, "baseline_missing"])
    assert pd.isna(enriched.loc[0, "baseline_mean"])
    assert pd.isna(enriched.loc[0, "crowd_ratio"])
    assert pd.isna(enriched.loc[0, "z_score"])
    assert pd.isna(enriched.loc[0, "crowd_level"])


def test_calculated_features_never_contain_infinity() -> None:
    baseline = build_historical_baseline([monday_values([0, 0, 0])])
    enriched = add_crowd_features(monday_values([0, 1]), baseline)

    calculated = enriched.loc[
        :,
        [
            "difference_from_median",
            "percentage_difference_from_median",
            "crowd_ratio",
            "z_score",
        ],
    ]
    finite_or_null = calculated.apply(
        lambda column: column.map(lambda value: pd.isna(value) or math.isfinite(value))
    )
    assert finite_or_null.all().all()


def test_custom_group_threshold_columns_are_supported() -> None:
    baseline = build_historical_baseline([monday_values([0, 10, 20, 30])])
    config = CrowdFeatureConfig(
        low_threshold_column="median_pedestrian_count",
        high_threshold_column="percentile_90_pedestrian_count",
    )

    enriched = add_crowd_features(monday_values([10, 20, 30]), baseline, config)

    assert enriched["crowd_level"].tolist() == ["low", "typical", "high"]


def test_duplicate_baseline_key_is_rejected() -> None:
    baseline = build_historical_baseline([monday_values([5])])
    duplicate = pd.concat([baseline, baseline], ignore_index=True)

    with pytest.raises(FeatureEngineeringError, match="business key must be unique"):
        add_crowd_features(monday_values([5]), duplicate)


def test_two_pass_workflow_calls_factory_separately_and_second_pass_is_lazy() -> None:
    chunks = [monday_values([1, 2]), monday_values([3])]
    calls = 0

    def chunk_factory() -> Iterable[pd.DataFrame]:
        nonlocal calls
        calls += 1
        return (chunk.copy(deep=True) for chunk in chunks)

    workflow = engineer_historical_features(chunk_factory)

    assert calls == 1
    assert workflow.accumulator_rows == 3
    assert workflow.accumulator_chunks == 2
    assert len(workflow.baseline) == 1
    enriched = list(workflow.enriched_chunks)
    assert calls == 2
    assert [len(chunk) for chunk in enriched] == [2, 1]


def test_workflow_never_uses_dataframe_concat(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [monday_values([1]), monday_values([2])]

    def forbidden_concat(*args: object, **kwargs: object) -> None:
        raise AssertionError("complete hourly data must not be concatenated")

    monkeypatch.setattr(pd, "concat", forbidden_concat)
    workflow = engineer_historical_features(lambda: iter(chunks))
    monkeypatch.undo()
    assert sum(len(chunk) for chunk in workflow.enriched_chunks) == 2


def test_minutely_and_network_remain_excluded() -> None:
    workflow = engineer_historical_features(lambda: iter([monday_values([1])]))

    assert workflow.excluded_datasets == EXCLUDED_FEATURE_DATASETS
    assert EXCLUDED_FEATURE_DATASETS == (
        "pedestrian_counts_minutely",
        "pedestrian_network",
    )


def test_feature_engineering_has_no_file_side_effects(tmp_path: Path) -> None:
    marker = tmp_path / "raw-marker.csv"
    marker.write_text("immutable\n", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    workflow = engineer_historical_features(lambda: iter([monday_values([1, 2])]))
    list(workflow.enriched_chunks)

    after = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert after == before
