"""Transactional PostGIS backfill for fixed routing-edge sensor proximity."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import math
import os
import re
import time
from typing import Any, Final
from urllib.parse import quote

import psycopg
from psycopg import Connection


DEFAULT_RADIUS_M: Final = 150.0
DISTANCE_TOLERANCE_M: Final = 0.01
DEFAULT_SAMPLE_SIZE: Final = 10
REQUIRED_MIGRATION_VERSION: Final = 8

REBUILD_INSERT_SQL: Final = """
INSERT INTO edge_sensor_map_rebuild (edge_id, sensor_id, distance_m)
SELECT
    edge.id,
    sensor.sensor_id,
    ST_Distance(edge.geometry::geography, sensor.geometry::geography)
FROM routing_edges AS edge
JOIN sensors AS sensor
  ON ST_DWithin(
      edge.geometry::geography,
      sensor.geometry::geography,
      %s
  )
ORDER BY edge.id, sensor.sensor_id
"""

SUMMARY_SQL: Final = """
SELECT
    COUNT(*)::BIGINT AS mapping_rows,
    COUNT(DISTINCT mapping.edge_id)::BIGINT AS distinct_edges,
    COUNT(DISTINCT mapping.sensor_id)::BIGINT AS distinct_sensors,
    MIN(mapping.distance_m)::DOUBLE PRECISION AS minimum_distance_m,
    MAX(mapping.distance_m)::DOUBLE PRECISION AS maximum_distance_m,
    (SELECT COUNT(*)::BIGINT FROM routing_edges) AS total_edges,
    COUNT(*) FILTER (
        WHERE mapping.distance_m > %s + %s
    )::BIGINT AS rows_over_radius,
    (
        SELECT COUNT(*)::BIGINT
        FROM (
            SELECT edge_id, sensor_id
            FROM edge_sensor_map
            GROUP BY edge_id, sensor_id
            HAVING COUNT(*) > 1
        ) AS duplicates
    ) AS duplicate_pairs,
    (
        SELECT COUNT(*)::BIGINT
        FROM edge_sensor_map AS candidate
        LEFT JOIN routing_edges AS edge ON edge.id = candidate.edge_id
        WHERE edge.id IS NULL
    ) AS orphaned_edges,
    (
        SELECT COUNT(*)::BIGINT
        FROM edge_sensor_map AS candidate
        LEFT JOIN sensors AS sensor ON sensor.sensor_id = candidate.sensor_id
        WHERE sensor.sensor_id IS NULL
    ) AS orphaned_sensors
FROM edge_sensor_map AS mapping
"""


class EdgeSensorMapError(RuntimeError):
    """Raised when the edge-to-sensor mapping cannot be rebuilt or verified."""


@dataclass(frozen=True, slots=True)
class EdgeSensorMapRow:
    """One deterministic verification sample row."""

    edge_id: int
    sensor_id: int
    distance_m: float

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable representation."""

        return {
            "edge_id": self.edge_id,
            "sensor_id": self.sensor_id,
            "distance_m": self.distance_m,
        }


@dataclass(frozen=True, slots=True)
class EdgeSensorMapStatistics:
    """Read-only integrity and coverage summary for the derived mapping."""

    radius_m: float
    mapping_rows: int
    distinct_edges: int
    distinct_sensors: int
    total_edges: int
    coverage_percentage: float
    minimum_distance_m: float | None
    maximum_distance_m: float | None
    rows_over_radius: int
    duplicate_pairs: int
    orphaned_edges: int
    orphaned_sensors: int
    sample_rows: tuple[EdgeSensorMapRow, ...] = ()

    @property
    def is_valid(self) -> bool:
        """Whether uniqueness, referential, and radius checks all pass."""

        return (
            self.rows_over_radius == 0
            and self.duplicate_pairs == 0
            and self.orphaned_edges == 0
            and self.orphaned_sensors == 0
        )

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-serialisable representation."""

        return {
            "radius_m": self.radius_m,
            "mapping_rows": self.mapping_rows,
            "distinct_edges": self.distinct_edges,
            "distinct_sensors": self.distinct_sensors,
            "total_edges": self.total_edges,
            "coverage_percentage": self.coverage_percentage,
            "minimum_distance_m": self.minimum_distance_m,
            "maximum_distance_m": self.maximum_distance_m,
            "rows_over_radius": self.rows_over_radius,
            "duplicate_pairs": self.duplicate_pairs,
            "orphaned_edges": self.orphaned_edges,
            "orphaned_sensors": self.orphaned_sensors,
            "sample_rows": [row.to_dict() for row in self.sample_rows],
            "is_valid": self.is_valid,
        }


@dataclass(frozen=True, slots=True)
class EdgeSensorMapRebuildResult:
    """Metrics for one committed mapping replacement."""

    rows_inserted: int
    elapsed_seconds: float
    statistics: EdgeSensorMapStatistics

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-serialisable representation."""

        return {
            "status": "succeeded",
            "rows_inserted": self.rows_inserted,
            "elapsed_seconds": self.elapsed_seconds,
            "statistics": self.statistics.to_dict(),
        }


def validate_radius(radius_m: object) -> float:
    """Return a positive finite radius in metres or raise ``ValueError``."""

    if isinstance(radius_m, bool):
        raise ValueError("radius_m must be a positive finite number")
    try:
        value = float(radius_m)
    except (TypeError, ValueError) as error:
        raise ValueError("radius_m must be a positive finite number") from error
    if not math.isfinite(value) or value <= 0:
        raise ValueError("radius_m must be a positive finite number")
    return value


def _environment_database_url(environment: Mapping[str, str]) -> str:
    for name in ("DATABASE_URL", "CITYFLOW_DATABASE_URL"):
        value = environment.get(name, "").strip()
        if value:
            return value
    host = environment.get("PGHOST") or environment.get("POSTGRES_HOST")
    port = environment.get("PGPORT") or environment.get("POSTGRES_PORT") or "5432"
    database = environment.get("PGDATABASE") or environment.get("POSTGRES_DB")
    user = environment.get("PGUSER") or environment.get("POSTGRES_USER")
    password = environment.get("PGPASSWORD") or environment.get("POSTGRES_PASSWORD")
    if not all((host, database, user, password)):
        raise EdgeSensorMapError(
            "database configuration is missing; set DATABASE_URL, "
            "CITYFLOW_DATABASE_URL, or PostgreSQL environment variables"
        )
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@"
        f"{host}:{port}/{quote(database, safe='')}"
    )


def _safe_error(error: BaseException, maximum_length: int = 1_000) -> str:
    value = re.sub(
        r"(?i)postgres(?:ql)?://[^\s]+",
        "<redacted-database-url>",
        str(error),
    )
    return (" ".join(value.split()) or error.__class__.__name__)[:maximum_length]


def _validate_database_contract(connection: Connection[Any]) -> None:
    row = connection.execute(
        "SELECT to_regclass('edge_sensor_map'), "
        "EXISTS (SELECT 1 FROM schema_migrations WHERE version = %s)",
        (REQUIRED_MIGRATION_VERSION,),
    ).fetchone()
    if row is None or row[0] is None or not bool(row[1]):
        raise EdgeSensorMapError(
            "migration 008 is required; run python database/migrate.py first"
        )


def _statistics(
    connection: Connection[Any],
    radius_m: float,
    sample_size: int,
) -> EdgeSensorMapStatistics:
    row = connection.execute(
        SUMMARY_SQL,
        (radius_m, DISTANCE_TOLERANCE_M),
    ).fetchone()
    if row is None:
        raise EdgeSensorMapError("edge_sensor_map verification returned no summary")
    (
        mapping_rows,
        distinct_edges,
        distinct_sensors,
        minimum_distance_m,
        maximum_distance_m,
        total_edges,
        rows_over_radius,
        duplicate_pairs,
        orphaned_edges,
        orphaned_sensors,
    ) = row
    total_edges_value = int(total_edges)
    distinct_edges_value = int(distinct_edges)
    coverage = (
        distinct_edges_value / total_edges_value * 100.0
        if total_edges_value
        else 0.0
    )
    samples = tuple(
        EdgeSensorMapRow(int(edge_id), int(sensor_id), float(distance_m))
        for edge_id, sensor_id, distance_m in connection.execute(
            "SELECT edge_id, sensor_id, distance_m "
            "FROM edge_sensor_map ORDER BY edge_id, sensor_id LIMIT %s",
            (sample_size,),
        ).fetchall()
    )
    return EdgeSensorMapStatistics(
        radius_m=radius_m,
        mapping_rows=int(mapping_rows),
        distinct_edges=distinct_edges_value,
        distinct_sensors=int(distinct_sensors),
        total_edges=total_edges_value,
        coverage_percentage=coverage,
        minimum_distance_m=(
            None if minimum_distance_m is None else float(minimum_distance_m)
        ),
        maximum_distance_m=(
            None if maximum_distance_m is None else float(maximum_distance_m)
        ),
        rows_over_radius=int(rows_over_radius),
        duplicate_pairs=int(duplicate_pairs),
        orphaned_edges=int(orphaned_edges),
        orphaned_sensors=int(orphaned_sensors),
        sample_rows=samples,
    )


def _with_connection(
    connection: Connection[Any] | None,
    database_url: str | None,
    connect: Callable[..., Connection[Any]],
) -> tuple[Connection[Any], bool]:
    if connection is not None and database_url is not None:
        raise ValueError("provide connection or database_url, not both")
    if connection is not None:
        return connection, False
    url = database_url or _environment_database_url(os.environ)
    try:
        return connect(url, autocommit=True), True
    except psycopg.Error as error:
        raise EdgeSensorMapError(
            f"database connection failed: {_safe_error(error)}"
        ) from error


def verify_edge_sensor_map(
    *,
    connection: Connection[Any] | None = None,
    database_url: str | None = None,
    radius_m: float = DEFAULT_RADIUS_M,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    connect: Callable[..., Connection[Any]] = psycopg.connect,
) -> EdgeSensorMapStatistics:
    """Read and validate mapping coverage without changing database state."""

    radius = validate_radius(radius_m)
    if isinstance(sample_size, bool) or not isinstance(sample_size, int) or sample_size < 0:
        raise ValueError("sample_size must be a non-negative integer")
    active, owned = _with_connection(connection, database_url, connect)
    try:
        with active.transaction():
            active.execute("SET TRANSACTION READ ONLY")
            _validate_database_contract(active)
            return _statistics(active, radius, sample_size)
    except (psycopg.Error, UnicodeError) as error:
        raise EdgeSensorMapError(
            f"edge_sensor_map verification failed: {_safe_error(error)}"
        ) from error
    finally:
        if owned:
            active.close()


def rebuild_edge_sensor_map(
    *,
    connection: Connection[Any] | None = None,
    database_url: str | None = None,
    radius_m: float = DEFAULT_RADIUS_M,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    connect: Callable[..., Connection[Any]] = psycopg.connect,
    clock: Callable[[], float] = time.perf_counter,
) -> EdgeSensorMapRebuildResult:
    """Atomically replace the derived mapping using PostGIS geography metres."""

    radius = validate_radius(radius_m)
    if isinstance(sample_size, bool) or not isinstance(sample_size, int) or sample_size < 0:
        raise ValueError("sample_size must be a non-negative integer")
    active, owned = _with_connection(connection, database_url, connect)
    started = clock()
    try:
        with active.transaction():
            _validate_database_contract(active)
            active.execute(
                "SELECT pg_advisory_xact_lock("
                "hashtext('cityflow-edge-sensor-map-rebuild'))"
            )
            active.execute(
                "CREATE TEMP TABLE edge_sensor_map_rebuild "
                "(LIKE edge_sensor_map INCLUDING ALL) ON COMMIT DROP"
            )
            staged_cursor = active.execute(REBUILD_INSERT_SQL, (radius,))
            staged_rows = int(staged_cursor.rowcount)
            active.execute("LOCK TABLE edge_sensor_map IN ACCESS EXCLUSIVE MODE")
            active.execute("TRUNCATE edge_sensor_map")
            replaced_cursor = active.execute(
                "INSERT INTO edge_sensor_map (edge_id, sensor_id, distance_m) "
                "SELECT edge_id, sensor_id, distance_m "
                "FROM edge_sensor_map_rebuild ORDER BY edge_id, sensor_id"
            )
            inserted_rows = int(replaced_cursor.rowcount)
            if inserted_rows != staged_rows:
                raise EdgeSensorMapError(
                    "mapping replacement row count did not match staged rows"
                )
            statistics = _statistics(active, radius, sample_size)
            if not statistics.is_valid:
                raise EdgeSensorMapError(
                    "mapping verification failed; transaction was rolled back"
                )
            active.execute("ANALYZE edge_sensor_map")
        return EdgeSensorMapRebuildResult(
            rows_inserted=inserted_rows,
            elapsed_seconds=clock() - started,
            statistics=statistics,
        )
    except EdgeSensorMapError:
        raise
    except (psycopg.Error, UnicodeError) as error:
        raise EdgeSensorMapError(
            f"edge_sensor_map rebuild failed: {_safe_error(error)}"
        ) from error
    finally:
        if owned:
            active.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--radius-m", type=float, default=DEFAULT_RADIUS_M)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="run read-only mapping integrity and coverage checks",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="optional database URL; environment variables are safer",
    )
    parser.add_argument(
        "--json", dest="json_output", action="store_true", help="emit JSON output"
    )
    return parser


def _human_statistics(statistics: EdgeSensorMapStatistics) -> str:
    minimum = (
        "none"
        if statistics.minimum_distance_m is None
        else f"{statistics.minimum_distance_m:.3f}"
    )
    maximum = (
        "none"
        if statistics.maximum_distance_m is None
        else f"{statistics.maximum_distance_m:.3f}"
    )
    lines = [
        f"Mapping rows: {statistics.mapping_rows}",
        f"Distinct edges: {statistics.distinct_edges}",
        f"Distinct sensors: {statistics.distinct_sensors}",
        f"Edge coverage: {statistics.coverage_percentage:.3f}%",
        f"Distance range metres: {minimum} to {maximum}",
        f"Rows over radius: {statistics.rows_over_radius}",
        f"Duplicate pairs: {statistics.duplicate_pairs}",
        f"Orphaned edges: {statistics.orphaned_edges}",
        f"Orphaned sensors: {statistics.orphaned_sensors}",
    ]
    if statistics.sample_rows:
        lines.append("Sample rows (edge_id, sensor_id, distance_m):")
        lines.extend(
            f"  {row.edge_id}, {row.sensor_id}, {row.distance_m:.3f}"
            for row in statistics.sample_rows
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the rebuild or read-only verification command."""

    args = _build_parser().parse_args(argv)
    try:
        if args.verify_only:
            statistics = verify_edge_sensor_map(
                database_url=args.database_url,
                radius_m=args.radius_m,
            )
            payload: dict[str, object] = {
                "status": "succeeded" if statistics.is_valid else "failed",
                "mode": "verify-only",
                "statistics": statistics.to_dict(),
            }
            human = _human_statistics(statistics)
            exit_code = 0 if statistics.is_valid else 1
        else:
            result = rebuild_edge_sensor_map(
                database_url=args.database_url,
                radius_m=args.radius_m,
            )
            payload = {"mode": "rebuild", **result.to_dict()}
            human = "\n".join(
                (
                    "edge_sensor_map rebuild succeeded",
                    f"Rows inserted: {result.rows_inserted}",
                    f"Elapsed seconds: {result.elapsed_seconds:.3f}",
                    _human_statistics(result.statistics),
                )
            )
            exit_code = 0
    except (EdgeSensorMapError, ValueError) as error:
        payload = {"status": "failed", "error": str(error)}
        human = f"edge_sensor_map command failed: {error}"
        exit_code = 1
    print(json.dumps(payload, sort_keys=True) if args.json_output else human)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_RADIUS_M",
    "DISTANCE_TOLERANCE_M",
    "EdgeSensorMapError",
    "EdgeSensorMapRebuildResult",
    "EdgeSensorMapRow",
    "EdgeSensorMapStatistics",
    "REBUILD_INSERT_SQL",
    "main",
    "rebuild_edge_sensor_map",
    "validate_radius",
    "verify_edge_sensor_map",
]
