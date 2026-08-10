"""Read immutable CityFlow CSV sources into pandas DataFrames."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import pandas as pd


CSV_ENCODING = "utf-8-sig"
DEFAULT_HOURLY_CHUNK_SIZE = 100_000

SOURCE_SCHEMAS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "pedestrian_sensors.csv": (
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
        ),
        "pedestrian_counts_hourly.csv": (
            "ID",
            "Location_ID",
            "Sensing_Date",
            "HourDay",
            "Direction_1",
            "Direction_2",
            "Total_of_Directions",
            "Sensor_Name",
            "Location",
        ),
        "landmarks.csv": (
            "Theme",
            "Sub Theme",
            "Feature Name",
            "Co-ordinates",
        ),
        "pedestrian_network.csv": (
            "Geo Point",
            "Geo Shape",
            "OBJECTID",
            "NeworkID",
        ),
        "pedestrian_counts_minutely.csv": (
            "Location_ID",
            "Sensing_DateTime",
            "Sensing_Date",
            "Sensing_Time",
            "Direction_1",
            "Direction_2",
            "Total_of_Directions",
        ),
    }
)

HISTORICAL_SOURCE_NAMES = (
    "pedestrian_sensors.csv",
    "pedestrian_counts_hourly.csv",
    "landmarks.csv",
    "pedestrian_network.csv",
)
LIVE_SOURCE_NAMES = ("pedestrian_counts_minutely.csv",)


class ExtractionError(Exception):
    """Base exception for CityFlow extraction failures."""


class SourceFileNotFoundError(ExtractionError):
    """Raised when a required raw source file is missing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"Required CityFlow source file is missing: {path}")


class InvalidSourceSchemaError(ExtractionError):
    """Raised when a raw source does not match its expected columns."""

    def __init__(
        self,
        path: Path,
        expected: tuple[str, ...],
        actual: tuple[str, ...],
    ) -> None:
        self.path = path
        self.expected = expected
        self.actual = actual
        missing = tuple(column for column in expected if column not in actual)
        unexpected = tuple(column for column in actual if column not in expected)
        details = [f"Expected columns {expected}; received {actual}."]
        if missing:
            details.append(f"Missing columns: {missing}.")
        if unexpected:
            details.append(f"Unexpected columns: {unexpected}.")
        if not missing and not unexpected and actual != expected:
            details.append("Column order does not match the source contract.")
        super().__init__(f"Invalid schema for {path}. {' '.join(details)}")


@dataclass(frozen=True, slots=True)
class HistoricalExtraction:
    """V1 historical reference DataFrames and lazy hourly chunks."""

    pedestrian_sensors: pd.DataFrame
    pedestrian_counts_hourly: Iterator[pd.DataFrame]
    landmarks: pd.DataFrame
    pedestrian_network: pd.DataFrame


def _source_path(raw_data_dir: str | Path, source_name: str) -> Path:
    path = Path(raw_data_dir).expanduser() / source_name
    if not path.is_file():
        raise SourceFileNotFoundError(path)
    return path


def _validate_schema(path: Path, expected: tuple[str, ...]) -> None:
    try:
        actual = tuple(pd.read_csv(path, encoding=CSV_ENCODING, nrows=0).columns)
    except FileNotFoundError as error:
        raise SourceFileNotFoundError(path) from error
    except (pd.errors.EmptyDataError, pd.errors.ParserError, UnicodeError) as error:
        raise InvalidSourceSchemaError(path, expected, ()) from error
    if actual != expected:
        raise InvalidSourceSchemaError(path, expected, actual)


def _validated_path(raw_data_dir: str | Path, source_name: str) -> Path:
    path = _source_path(raw_data_dir, source_name)
    _validate_schema(path, SOURCE_SCHEMAS[source_name])
    return path


def _read_dataframe(path: Path, schema: tuple[str, ...]) -> pd.DataFrame:
    try:
        frame = pd.read_csv(path, encoding=CSV_ENCODING)
    except FileNotFoundError as error:
        raise SourceFileNotFoundError(path) from error
    return frame.loc[:, list(schema)]


def _read_chunks(
    path: Path,
    schema: tuple[str, ...],
    chunk_size: int,
) -> Iterator[pd.DataFrame]:
    try:
        reader = pd.read_csv(
            path,
            encoding=CSV_ENCODING,
            chunksize=chunk_size,
        )
    except FileNotFoundError as error:
        raise SourceFileNotFoundError(path) from error
    with reader:
        for chunk in reader:
            yield chunk.loc[:, list(schema)]


def extract_pedestrian_sensors(raw_data_dir: str | Path) -> pd.DataFrame:
    """Read and validate the pedestrian sensor reference dataset."""

    source_name = "pedestrian_sensors.csv"
    path = _validated_path(raw_data_dir, source_name)
    return _read_dataframe(path, SOURCE_SCHEMAS[source_name])


def extract_landmarks(raw_data_dir: str | Path) -> pd.DataFrame:
    """Read and validate the landmark reference dataset."""

    source_name = "landmarks.csv"
    path = _validated_path(raw_data_dir, source_name)
    return _read_dataframe(path, SOURCE_SCHEMAS[source_name])


def extract_pedestrian_network(raw_data_dir: str | Path) -> pd.DataFrame:
    """Read and validate the pedestrian network reference dataset."""

    source_name = "pedestrian_network.csv"
    path = _validated_path(raw_data_dir, source_name)
    return _read_dataframe(path, SOURCE_SCHEMAS[source_name])


def extract_pedestrian_counts_hourly(
    raw_data_dir: str | Path,
    *,
    chunk_size: int = DEFAULT_HOURLY_CHUNK_SIZE,
) -> Iterator[pd.DataFrame]:
    """Validate hourly counts and return a memory-efficient chunk iterator."""

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    source_name = "pedestrian_counts_hourly.csv"
    path = _validated_path(raw_data_dir, source_name)
    return _read_chunks(path, SOURCE_SCHEMAS[source_name], chunk_size)


def extract_historical_sources(
    raw_data_dir: str | Path,
    *,
    hourly_chunk_size: int = DEFAULT_HOURLY_CHUNK_SIZE,
) -> HistoricalExtraction:
    """Validate and open the four V1 historical sources.

    The minutely dataset is intentionally excluded because it belongs to the
    later live-data workflow.
    """

    if (
        isinstance(hourly_chunk_size, bool)
        or not isinstance(hourly_chunk_size, int)
        or hourly_chunk_size <= 0
    ):
        raise ValueError("hourly_chunk_size must be a positive integer")

    paths = {
        source_name: _validated_path(raw_data_dir, source_name)
        for source_name in HISTORICAL_SOURCE_NAMES
    }
    return HistoricalExtraction(
        pedestrian_sensors=_read_dataframe(
            paths["pedestrian_sensors.csv"],
            SOURCE_SCHEMAS["pedestrian_sensors.csv"],
        ),
        pedestrian_counts_hourly=_read_chunks(
            paths["pedestrian_counts_hourly.csv"],
            SOURCE_SCHEMAS["pedestrian_counts_hourly.csv"],
            hourly_chunk_size,
        ),
        landmarks=_read_dataframe(
            paths["landmarks.csv"],
            SOURCE_SCHEMAS["landmarks.csv"],
        ),
        pedestrian_network=_read_dataframe(
            paths["pedestrian_network.csv"],
            SOURCE_SCHEMAS["pedestrian_network.csv"],
        ),
    )


__all__ = [
    "CSV_ENCODING",
    "DEFAULT_HOURLY_CHUNK_SIZE",
    "HISTORICAL_SOURCE_NAMES",
    "LIVE_SOURCE_NAMES",
    "SOURCE_SCHEMAS",
    "ExtractionError",
    "HistoricalExtraction",
    "InvalidSourceSchemaError",
    "SourceFileNotFoundError",
    "extract_historical_sources",
    "extract_landmarks",
    "extract_pedestrian_counts_hourly",
    "extract_pedestrian_network",
    "extract_pedestrian_sensors",
]
