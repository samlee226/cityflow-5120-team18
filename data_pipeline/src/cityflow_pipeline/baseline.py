"""Chunk-based historical crowd baselines and descriptive crowd features.

The baseline intentionally uses the complete supplied historical period for
descriptive comparison. Predictive work must later use time-based training
splits to avoid future-data leakage.
"""

from __future__ import annotations

from array import array
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
import math
import sys
from typing import Final

import pandas as pd

from cityflow_pipeline.transform import HOURLY_FACT_COLUMNS


BASELINE_KEY_COLUMNS: Final = ("sensor_id", "iso_weekday", "hour")
QUANTILE_INTERPOLATION: Final = "linear"
BASELINE_COLUMNS: Final = (
    *BASELINE_KEY_COLUMNS,
    "observation_count",
    "minimum_sensing_date",
    "maximum_sensing_date",
    "minimum_pedestrian_count",
    "maximum_pedestrian_count",
    "mean_pedestrian_count",
    "median_pedestrian_count",
    "population_std_pedestrian_count",
    "percentile_25_pedestrian_count",
    "percentile_75_pedestrian_count",
    "percentile_90_pedestrian_count",
)
ENRICHED_FEATURE_COLUMNS: Final = (
    "baseline_observation_count",
    "baseline_mean",
    "baseline_median",
    "baseline_standard_deviation",
    "baseline_percentile_25",
    "baseline_percentile_75",
    "baseline_percentile_90",
    "difference_from_median",
    "percentage_difference_from_median",
    "crowd_ratio",
    "z_score",
    "crowd_level",
    "baseline_missing",
)
EXCLUDED_FEATURE_DATASETS: Final = (
    "pedestrian_counts_minutely",
    "pedestrian_network",
)

_BASELINE_TO_FEATURE: Final = {
    "observation_count": "baseline_observation_count",
    "mean_pedestrian_count": "baseline_mean",
    "median_pedestrian_count": "baseline_median",
    "population_std_pedestrian_count": "baseline_standard_deviation",
    "percentile_25_pedestrian_count": "baseline_percentile_25",
    "percentile_75_pedestrian_count": "baseline_percentile_75",
    "percentile_90_pedestrian_count": "baseline_percentile_90",
}
_THRESHOLD_COLUMNS: Final = frozenset(
    {
        "minimum_pedestrian_count",
        "maximum_pedestrian_count",
        "mean_pedestrian_count",
        "median_pedestrian_count",
        "percentile_25_pedestrian_count",
        "percentile_75_pedestrian_count",
        "percentile_90_pedestrian_count",
    }
)


class FeatureEngineeringError(ValueError):
    """Raised when data cannot satisfy the crowd-feature contract."""


@dataclass(frozen=True, slots=True)
class CrowdFeatureConfig:
    """Configurable group-specific thresholds for initial crowd categories."""

    low_threshold_column: str = "percentile_25_pedestrian_count"
    high_threshold_column: str = "percentile_75_pedestrian_count"

    def __post_init__(self) -> None:
        if self.low_threshold_column not in _THRESHOLD_COLUMNS:
            raise ValueError(
                f"unsupported low threshold column: {self.low_threshold_column}"
            )
        if self.high_threshold_column not in _THRESHOLD_COLUMNS:
            raise ValueError(
                f"unsupported high threshold column: {self.high_threshold_column}"
            )


@dataclass(slots=True)
class _GroupState:
    values: array = field(default_factory=lambda: array("q"))
    minimum_sensing_date: pd.Timestamp | None = None
    maximum_sensing_date: pd.Timestamp | None = None
    minimum_pedestrian_count: int | None = None
    maximum_pedestrian_count: int | None = None


@dataclass(frozen=True, slots=True)
class HistoricalFeatureWorkflow:
    """Materialized baseline plus a lazy second-pass enriched chunk iterator."""

    baseline: pd.DataFrame
    enriched_chunks: Iterator[pd.DataFrame]
    accumulator_rows: int
    accumulator_chunks: int
    accumulator_retained_bytes: int
    excluded_datasets: tuple[str, ...] = EXCLUDED_FEATURE_DATASETS


def _require_hourly_frame(frame: object) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("hourly chunk must be a pandas DataFrame")
    missing = tuple(column for column in HOURLY_FACT_COLUMNS if column not in frame)
    if missing:
        raise FeatureEngineeringError(
            f"hourly chunk is missing transformed columns: {missing}"
        )
    if (
        frame.loc[
            :, BASELINE_KEY_COLUMNS + ("sensing_date", "pedestrian_count")
        ]
        .isna()
        .any()
        .any()
    ):
        raise FeatureEngineeringError("hourly baseline inputs cannot contain null values")

    numeric_columns = ["sensor_id", "iso_weekday", "hour", "pedestrian_count"]
    numeric = frame.loc[:, numeric_columns].apply(pd.to_numeric, errors="coerce")
    finite = numeric.apply(lambda column: column.map(math.isfinite))
    if numeric.isna().any().any() or not finite.all().all():
        raise FeatureEngineeringError("hourly baseline inputs must be finite numeric values")
    if not numeric.mod(1).eq(0).all().all():
        raise FeatureEngineeringError("hourly baseline keys and counts must be integers")
    if not numeric["iso_weekday"].between(1, 7).all():
        raise FeatureEngineeringError("iso_weekday must be between 1 and 7")
    if not numeric["hour"].between(0, 23).all():
        raise FeatureEngineeringError("hour must be between 0 and 23")
    if numeric["pedestrian_count"].lt(0).any():
        raise FeatureEngineeringError("pedestrian_count cannot be negative")
    return frame


class HistoricalBaselineAccumulator:
    """Accumulate compact counts per baseline key without retaining DataFrames.

    Exact quantiles require retaining each pedestrian count. Values are held in
    signed 64-bit ``array`` buffers grouped by ``sensor_id + iso_weekday + hour``.
    No input DataFrame or complete hourly table is retained.
    """

    def __init__(self) -> None:
        self._groups: dict[tuple[int, int, int], _GroupState] = {}
        self._row_count = 0
        self._chunk_count = 0

    @property
    def row_count(self) -> int:
        """Return the number of accumulated hourly observations."""

        return self._row_count

    @property
    def chunk_count(self) -> int:
        """Return the number of accumulated chunks."""

        return self._chunk_count

    @property
    def group_count(self) -> int:
        """Return the number of unique baseline business keys."""

        return len(self._groups)

    @property
    def retained_value_count(self) -> int:
        """Return the exact number of compact numeric values retained."""

        return sum(len(state.values) for state in self._groups.values())

    @property
    def approximate_retained_bytes(self) -> int:
        """Estimate retained accumulator state, including Python containers."""

        total = sys.getsizeof(self._groups)
        for key, state in self._groups.items():
            total += sys.getsizeof(key) + sys.getsizeof(state)
            total += sys.getsizeof(state.values)
            total += sum(sys.getsizeof(value) for value in key)
            if state.minimum_sensing_date is not None:
                total += sys.getsizeof(state.minimum_sensing_date)
            if state.maximum_sensing_date is not None:
                total += sys.getsizeof(state.maximum_sensing_date)
        return total

    def update(self, frame: pd.DataFrame) -> None:
        """Consume one transformed hourly chunk."""

        frame = _require_hourly_frame(frame)
        if frame.empty:
            self._chunk_count += 1
            return

        working = frame.loc[
            :, BASELINE_KEY_COLUMNS + ("sensing_date", "pedestrian_count")
        ].copy()
        working["sensing_date"] = pd.to_datetime(
            working["sensing_date"], errors="raise"
        ).dt.normalize()

        for key, group in working.groupby(
            list(BASELINE_KEY_COLUMNS), sort=False, observed=True
        ):
            normalized_key = tuple(int(value) for value in key)
            counts = array("q", (int(value) for value in group["pedestrian_count"]))
            dates = group["sensing_date"]
            group_minimum = min(counts)
            group_maximum = max(counts)
            date_minimum = pd.Timestamp(dates.min())
            date_maximum = pd.Timestamp(dates.max())

            state = self._groups.setdefault(normalized_key, _GroupState())
            state.values.extend(counts)
            if (
                state.minimum_sensing_date is None
                or date_minimum < state.minimum_sensing_date
            ):
                state.minimum_sensing_date = date_minimum
            if (
                state.maximum_sensing_date is None
                or date_maximum > state.maximum_sensing_date
            ):
                state.maximum_sensing_date = date_maximum
            if (
                state.minimum_pedestrian_count is None
                or group_minimum < state.minimum_pedestrian_count
            ):
                state.minimum_pedestrian_count = group_minimum
            if (
                state.maximum_pedestrian_count is None
                or group_maximum > state.maximum_pedestrian_count
            ):
                state.maximum_pedestrian_count = group_maximum

        self._row_count += len(frame)
        self._chunk_count += 1

    def finalize(self) -> pd.DataFrame:
        """Return a stable baseline using pandas' linear quantile method."""

        if not self._groups:
            raise FeatureEngineeringError("cannot build a baseline without observations")

        rows: list[dict[str, object]] = []
        for (sensor_id, iso_weekday, hour), state in self._groups.items():
            values = pd.Series(state.values, dtype="int64")
            quantiles = values.quantile(
                [0.25, 0.50, 0.75, 0.90],
                interpolation=QUANTILE_INTERPOLATION,
            )
            rows.append(
                {
                    "sensor_id": sensor_id,
                    "iso_weekday": iso_weekday,
                    "hour": hour,
                    "observation_count": len(values),
                    "minimum_sensing_date": state.minimum_sensing_date,
                    "maximum_sensing_date": state.maximum_sensing_date,
                    "minimum_pedestrian_count": state.minimum_pedestrian_count,
                    "maximum_pedestrian_count": state.maximum_pedestrian_count,
                    "mean_pedestrian_count": float(values.mean()),
                    "median_pedestrian_count": float(quantiles.loc[0.50]),
                    "population_std_pedestrian_count": float(
                        values.std(ddof=0)
                    ),
                    "percentile_25_pedestrian_count": float(quantiles.loc[0.25]),
                    "percentile_75_pedestrian_count": float(quantiles.loc[0.75]),
                    "percentile_90_pedestrian_count": float(quantiles.loc[0.90]),
                }
            )

        baseline = pd.DataFrame(rows, columns=BASELINE_COLUMNS)
        return baseline.sort_values(
            list(BASELINE_KEY_COLUMNS), kind="mergesort"
        ).reset_index(drop=True)


def build_historical_baseline(
    hourly_chunks: Iterable[pd.DataFrame],
) -> pd.DataFrame:
    """Build an exact descriptive baseline from a single chunk pass."""

    accumulator = HistoricalBaselineAccumulator()
    for chunk in hourly_chunks:
        accumulator.update(chunk)
    return accumulator.finalize()


def _validate_baseline(baseline: object, config: CrowdFeatureConfig) -> pd.DataFrame:
    if not isinstance(baseline, pd.DataFrame):
        raise TypeError("baseline must be a pandas DataFrame")
    required = BASELINE_COLUMNS + (
        config.low_threshold_column,
        config.high_threshold_column,
    )
    missing = tuple(column for column in required if column not in baseline)
    if missing:
        raise FeatureEngineeringError(f"baseline is missing columns: {missing}")
    if baseline.duplicated(subset=list(BASELINE_KEY_COLUMNS), keep=False).any():
        raise FeatureEngineeringError("baseline business key must be unique")

    metric_columns = list(_BASELINE_TO_FEATURE) + [
        config.low_threshold_column,
        config.high_threshold_column,
    ]
    metrics = baseline.loc[:, metric_columns].apply(pd.to_numeric, errors="coerce")
    finite = metrics.apply(lambda column: column.map(math.isfinite))
    if metrics.isna().any().any() or not finite.all().all():
        raise FeatureEngineeringError("baseline metrics must be finite and non-null")
    if (
        baseline[config.low_threshold_column]
        > baseline[config.high_threshold_column]
    ).any():
        raise FeatureEngineeringError("low crowd threshold cannot exceed high threshold")
    return baseline


def add_crowd_features(
    frame: pd.DataFrame,
    baseline: pd.DataFrame,
    config: CrowdFeatureConfig | None = None,
) -> pd.DataFrame:
    """Enrich one transformed hourly chunk with group-specific crowd features.

    Ratios and percentage differences are null when the group median is zero.
    A matched group with zero population standard deviation receives z-score
    zero. Missing groups remain in the output with ``baseline_missing=True``
    and null baseline-derived values; no global fallback is applied.
    """

    frame = _require_hourly_frame(frame)
    config = config or CrowdFeatureConfig()
    baseline = _validate_baseline(baseline, config)
    collisions = tuple(column for column in ENRICHED_FEATURE_COLUMNS if column in frame)
    if collisions:
        raise FeatureEngineeringError(
            f"hourly chunk already contains crowd feature columns: {collisions}"
        )

    selected_baseline_columns = list(
        dict.fromkeys(
            [
                *BASELINE_KEY_COLUMNS,
                *_BASELINE_TO_FEATURE,
                config.low_threshold_column,
                config.high_threshold_column,
            ]
        )
    )
    joined = frame.copy(deep=True).merge(
        baseline.loc[:, selected_baseline_columns],
        on=list(BASELINE_KEY_COLUMNS),
        how="left",
        sort=False,
        validate="many_to_one",
    )
    joined.index = frame.index.copy()
    joined.rename(columns=_BASELINE_TO_FEATURE, inplace=True)
    joined["baseline_observation_count"] = joined[
        "baseline_observation_count"
    ].astype("Int64")
    joined["baseline_missing"] = joined["baseline_observation_count"].isna()

    pedestrian_count = pd.to_numeric(joined["pedestrian_count"])
    mean = joined["baseline_mean"]
    median = joined["baseline_median"]
    standard_deviation = joined["baseline_standard_deviation"]
    difference = pedestrian_count - median
    nonzero_median = median.ne(0) & median.notna()
    nonzero_std = standard_deviation.gt(0) & standard_deviation.notna()

    joined["difference_from_median"] = difference
    percentage_difference = pd.Series(float("nan"), index=joined.index)
    percentage_difference.loc[nonzero_median] = (
        difference.loc[nonzero_median] / median.loc[nonzero_median] * 100.0
    )
    joined["percentage_difference_from_median"] = percentage_difference

    crowd_ratio = pd.Series(float("nan"), index=joined.index)
    crowd_ratio.loc[nonzero_median] = (
        pedestrian_count.loc[nonzero_median] / median.loc[nonzero_median]
    )
    joined["crowd_ratio"] = crowd_ratio

    z_score = pd.Series(float("nan"), index=joined.index)
    z_score.loc[~joined["baseline_missing"]] = 0.0
    z_score.loc[nonzero_std] = (
        pedestrian_count.loc[nonzero_std] - mean.loc[nonzero_std]
    ) / standard_deviation.loc[nonzero_std]
    joined["z_score"] = z_score

    low_threshold_name = _BASELINE_TO_FEATURE.get(
        config.low_threshold_column, config.low_threshold_column
    )
    high_threshold_name = _BASELINE_TO_FEATURE.get(
        config.high_threshold_column, config.high_threshold_column
    )
    low_threshold = joined[low_threshold_name]
    high_threshold = joined[high_threshold_name]
    crowd_level = pd.Series(pd.NA, index=joined.index, dtype="string")
    matched = ~joined["baseline_missing"]
    crowd_level.loc[matched] = "typical"
    crowd_level.loc[matched & pedestrian_count.lt(low_threshold)] = "low"
    crowd_level.loc[matched & pedestrian_count.gt(high_threshold)] = "high"
    joined["crowd_level"] = crowd_level

    temporary_thresholds = {
        low_threshold_name,
        high_threshold_name,
    } - set(_BASELINE_TO_FEATURE.values())
    if temporary_thresholds:
        joined.drop(columns=sorted(temporary_thresholds), inplace=True)

    calculated = [
        "difference_from_median",
        "percentage_difference_from_median",
        "crowd_ratio",
        "z_score",
    ]
    joined.loc[:, calculated] = joined.loc[:, calculated].replace(
        [float("inf"), float("-inf")], float("nan")
    )
    return joined.loc[:, (*frame.columns, *ENRICHED_FEATURE_COLUMNS)]


def engineer_historical_features(
    chunk_factory: Callable[[], Iterable[pd.DataFrame]],
    config: CrowdFeatureConfig | None = None,
) -> HistoricalFeatureWorkflow:
    """Run a two-pass, restartable historical feature workflow.

    Pass one consumes the factory to build the complete-period descriptive
    baseline. The returned iterator invokes the factory again lazily for pass
    two and enriches each chunk without caching or concatenating hourly data.
    """

    if not callable(chunk_factory):
        raise TypeError("chunk_factory must be callable")
    config = config or CrowdFeatureConfig()
    accumulator = HistoricalBaselineAccumulator()
    for chunk in chunk_factory():
        accumulator.update(chunk)
    baseline = accumulator.finalize()

    def enriched_chunks() -> Iterator[pd.DataFrame]:
        for chunk in chunk_factory():
            yield add_crowd_features(chunk, baseline, config)

    return HistoricalFeatureWorkflow(
        baseline=baseline,
        enriched_chunks=enriched_chunks(),
        accumulator_rows=accumulator.row_count,
        accumulator_chunks=accumulator.chunk_count,
        accumulator_retained_bytes=accumulator.approximate_retained_bytes,
    )


__all__ = [
    "BASELINE_COLUMNS",
    "BASELINE_KEY_COLUMNS",
    "ENRICHED_FEATURE_COLUMNS",
    "EXCLUDED_FEATURE_DATASETS",
    "QUANTILE_INTERPOLATION",
    "CrowdFeatureConfig",
    "FeatureEngineeringError",
    "HistoricalBaselineAccumulator",
    "HistoricalFeatureWorkflow",
    "add_crowd_features",
    "build_historical_baseline",
    "engineer_historical_features",
]
