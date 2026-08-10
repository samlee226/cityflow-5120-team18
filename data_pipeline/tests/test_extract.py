"""Tests for the read-only CityFlow extraction layer."""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd
import pytest

from cityflow_pipeline.extract import (
    HISTORICAL_SOURCE_NAMES,
    LIVE_SOURCE_NAMES,
    SOURCE_SCHEMAS,
    InvalidSourceSchemaError,
    SourceFileNotFoundError,
    extract_historical_sources,
    extract_landmarks,
    extract_pedestrian_counts_hourly,
    extract_pedestrian_network,
    extract_pedestrian_sensors,
)


SAMPLE_ROWS = {
    "pedestrian_sensors.csv": [
        "1",
        "Bourke Street Mall (North)",
        "Bou292_T",
        "2009-03-24",
        "",
        "Outdoor",
        "A",
        "East",
        "West",
        "-37.81349441",
        "144.96515323",
        "-37.81349441, 144.96515323",
    ],
    "pedestrian_counts_hourly.csv": [
        "441520240715",
        "44",
        "2024-07-15",
        "15",
        "44",
        "47",
        "91",
        "UM3_T",
        "-37.79698741, 144.96441306",
    ],
    "landmarks.csv": [
        "Leisure/Recreation",
        "Major Sports & Recreation Facility",
        "Carlton Football Club",
        "-37.7840864379557, 144.961967841559",
    ],
    "pedestrian_network.csv": [
        "-37.8027925911, 144.9523806541",
        '{"coordinates": [144.9523806541, -37.8027925911], "type": "Point"}',
        "10004",
        "110807",
    ],
    "pedestrian_counts_minutely.csv": [
        "4",
        "2026-08-02T20:34:00+10:00",
        "2026-08-02",
        "20:34",
        "16",
        "10",
        "26",
    ],
}


def write_source(
    raw_dir: Path,
    source_name: str,
    *,
    rows: list[list[str]] | None = None,
    columns: tuple[str, ...] | None = None,
) -> Path:
    """Create a temporary UTF-8 BOM CSV fixture."""

    path = raw_dir / source_name
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns or SOURCE_SCHEMAS[source_name])
        writer.writerows(rows or [SAMPLE_ROWS[source_name]])
    return path


def write_historical_sources(raw_dir: Path) -> None:
    for source_name in HISTORICAL_SOURCE_NAMES:
        write_source(raw_dir, source_name)


def test_successful_historical_extraction_excludes_live_source(tmp_path: Path) -> None:
    write_historical_sources(tmp_path)

    extracted = extract_historical_sources(tmp_path, hourly_chunk_size=2)

    assert list(extracted.pedestrian_sensors.columns) == list(
        SOURCE_SCHEMAS["pedestrian_sensors.csv"]
    )
    assert list(extracted.landmarks.columns) == list(SOURCE_SCHEMAS["landmarks.csv"])
    assert list(extracted.pedestrian_network.columns) == list(
        SOURCE_SCHEMAS["pedestrian_network.csv"]
    )
    hourly = pd.concat(list(extracted.pedestrian_counts_hourly), ignore_index=True)
    assert list(hourly.columns) == list(SOURCE_SCHEMAS["pedestrian_counts_hourly.csv"])
    assert len(hourly) == 1
    assert LIVE_SOURCE_NAMES == ("pedestrian_counts_minutely.csv",)
    assert not (tmp_path / LIVE_SOURCE_NAMES[0]).exists()


def test_individual_reference_extractors(tmp_path: Path) -> None:
    write_source(tmp_path, "pedestrian_sensors.csv")
    write_source(tmp_path, "landmarks.csv")
    write_source(tmp_path, "pedestrian_network.csv")

    assert len(extract_pedestrian_sensors(tmp_path)) == 1
    assert len(extract_landmarks(tmp_path)) == 1
    assert len(extract_pedestrian_network(tmp_path)) == 1


def test_missing_file_raises_custom_exception(tmp_path: Path) -> None:
    write_source(tmp_path, "pedestrian_sensors.csv")

    with pytest.raises(SourceFileNotFoundError, match="pedestrian_counts_hourly.csv"):
        extract_historical_sources(tmp_path)


def test_missing_column_raises_custom_schema_exception(tmp_path: Path) -> None:
    expected = SOURCE_SCHEMAS["landmarks.csv"]
    write_source(
        tmp_path,
        "landmarks.csv",
        columns=expected[:-1],
        rows=[["Leisure/Recreation", "Park", "Test landmark"]],
    )

    with pytest.raises(InvalidSourceSchemaError, match="Co-ordinates"):
        extract_landmarks(tmp_path)


def test_empty_file_raises_custom_schema_exception(tmp_path: Path) -> None:
    (tmp_path / "landmarks.csv").write_bytes(b"")

    with pytest.raises(InvalidSourceSchemaError, match="Invalid schema"):
        extract_landmarks(tmp_path)


def test_utf8_bom_is_supported(tmp_path: Path) -> None:
    row = SAMPLE_ROWS["landmarks.csv"].copy()
    row[2] = "Café landmark"
    path = write_source(tmp_path, "landmarks.csv", rows=[row])

    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    frame = extract_landmarks(tmp_path)
    assert frame.columns[0] == "Theme"
    assert frame.loc[0, "Feature Name"] == "Café landmark"


def test_hourly_extraction_is_chunked(tmp_path: Path) -> None:
    rows = []
    for index in range(5):
        row = SAMPLE_ROWS["pedestrian_counts_hourly.csv"].copy()
        row[0] = str(1_000 + index)
        row[3] = str(index)
        rows.append(row)
    write_source(tmp_path, "pedestrian_counts_hourly.csv", rows=rows)

    chunks = list(extract_pedestrian_counts_hourly(tmp_path, chunk_size=2))

    assert [len(chunk) for chunk in chunks] == [2, 2, 1]
    assert all(
        list(chunk.columns) == list(SOURCE_SCHEMAS["pedestrian_counts_hourly.csv"])
        for chunk in chunks
    )


@pytest.mark.parametrize("chunk_size", [0, -1, True, 1.5])
def test_hourly_chunk_size_must_be_a_positive_integer(
    tmp_path: Path,
    chunk_size: object,
) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        extract_pedestrian_counts_hourly(tmp_path, chunk_size=chunk_size)  # type: ignore[arg-type]


def test_extraction_does_not_modify_sources(tmp_path: Path) -> None:
    write_historical_sources(tmp_path)
    before = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.iterdir()
    }

    extracted = extract_historical_sources(tmp_path, hourly_chunk_size=1)
    list(extracted.pedestrian_counts_hourly)

    after = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.iterdir()
    }
    assert after == before
