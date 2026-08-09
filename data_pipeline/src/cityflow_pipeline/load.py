"""Transactional Psycopg COPY/upsert loading for processed CityFlow data."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import os
import re
import time
from typing import Any, Final
from urllib.parse import quote
from uuid import UUID, uuid4

import numpy as np
import pandas as pd
import psycopg
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from cityflow_pipeline.baseline import BASELINE_COLUMNS
from cityflow_pipeline.spatial import (
    LANDMARK_MAPPING_COLUMNS,
    ROUTING_EDGE_COLUMNS,
    ROUTING_NODE_COLUMNS,
    SENSOR_MAPPING_COLUMNS,
    PedestrianNetworkResult,
    SpatialMappingResult,
)
from cityflow_pipeline.transform import (
    HOURLY_FACT_COLUMNS,
    LANDMARK_DIMENSION_COLUMNS,
    SENSOR_DIMENSION_COLUMNS,
    SENSOR_DIRECTION_TABLE_COLUMNS,
)
from cityflow_pipeline.validate import HistoricalValidationReport


REQUIRED_MIGRATION_VERSIONS: Final = (1, 2, 3, 5)
LOAD_TABLE_ORDER: Final = (
    "sensors",
    "sensor_directions",
    "landmarks",
    "pedestrian_counts_hourly",
    "crowd_baselines",
    "routing_nodes",
    "routing_edges",
    "sensor_network_map",
    "landmark_network_map",
)


class DatabaseLoadError(RuntimeError):
    """Raised when a database load contract or transaction fails."""


@dataclass(frozen=True, slots=True)
class DatabaseLoaderConfig:
    """Connection, observability, and non-destructive load settings."""

    database_url: str | None = None
    pipeline_name: str = "cityflow-historical-load"
    source_fingerprint: str | None = None
    dry_run: bool = False
    copy_batch_size: int = 10_000
    error_message_max_length: int = 1_000
    required_migration_versions: tuple[int, ...] = REQUIRED_MIGRATION_VERSIONS
    expected_source_counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.pipeline_name.strip():
            raise ValueError("pipeline_name must not be blank")
        if (
            isinstance(self.copy_batch_size, bool)
            or not isinstance(self.copy_batch_size, int)
            or self.copy_batch_size <= 0
        ):
            raise ValueError("copy_batch_size must be a positive integer")
        if (
            isinstance(self.error_message_max_length, bool)
            or not isinstance(self.error_message_max_length, int)
            or self.error_message_max_length <= 0
        ):
            raise ValueError("error_message_max_length must be a positive integer")
        if not self.required_migration_versions:
            raise ValueError("required_migration_versions must not be empty")
        expected = dict(self.expected_source_counts)
        unknown = sorted(set(expected) - set(LOAD_TABLE_ORDER))
        if unknown:
            raise ValueError(f"unknown expected_source_counts tables: {unknown}")
        if any(isinstance(value, bool) or value < 0 for value in expected.values()):
            raise ValueError("expected source counts must be non-negative integers")
        object.__setattr__(self, "expected_source_counts", expected)


@dataclass(frozen=True, slots=True)
class TableLoadResult:
    """Deterministic metrics for one table load stage."""

    table_name: str
    source_count: int
    staged_count: int
    affected_count: int
    final_count: int
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable result mapping."""

        return {
            "table_name": self.table_name,
            "source_count": self.source_count,
            "staged_count": self.staged_count,
            "affected_count": self.affected_count,
            "final_count": self.final_count,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class HistoricalLoadResult:
    """Structured result for a complete historical load attempt."""

    pipeline_run_id: str | None
    status: str
    table_results: tuple[TableLoadResult, ...]
    elapsed_seconds: float
    warnings: tuple[str, ...] = ()
    dry_run: bool = False

    @property
    def total_source_rows(self) -> int:
        """Return the sum of source rows across all table stages."""

        return sum(result.source_count for result in self.table_results)

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-serialisable result mapping."""

        return {
            "pipeline_run_id": self.pipeline_run_id,
            "status": self.status,
            "table_results": [result.to_dict() for result in self.table_results],
            "total_source_rows": self.total_source_rows,
            "elapsed_seconds": self.elapsed_seconds,
            "warnings": list(self.warnings),
            "dry_run": self.dry_run,
        }


class _DryRunRollback(Exception):
    pass


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
        raise DatabaseLoadError(
            "database configuration is missing; set DATABASE_URL, "
            "CITYFLOW_DATABASE_URL, or PostgreSQL environment variables"
        )
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@"
        f"{host}:{port}/{quote(database, safe='')}"
    )


def _safe_error(error: BaseException, maximum_length: int) -> str:
    value = re.sub(
        r"(?i)postgres(?:ql)?://[^\s]+",
        "<redacted-database-url>",
        str(error),
    )
    value = " ".join(value.split()) or error.__class__.__name__
    return value[:maximum_length]


def _python_value(value: object) -> object:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    return value


def _uuid_value(value: object, field_name: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise DatabaseLoadError(f"{field_name} contains an invalid UUID") from error


def _bigint_value(
    value: object, field_name: str, *, nullable: bool = False
) -> int | None:
    """Normalise integer-semantic pandas values for PostgreSQL BIGINT COPY."""

    normalised = _python_value(value)
    if normalised is None:
        if nullable:
            return None
        raise DatabaseLoadError(f"{field_name} cannot be null")
    if isinstance(normalised, bool):
        raise DatabaseLoadError(f"{field_name} must be an integer")
    try:
        decimal = Decimal(str(normalised))
    except (InvalidOperation, ValueError) as error:
        raise DatabaseLoadError(f"{field_name} must be an integer") from error
    if not decimal.is_finite() or decimal != decimal.to_integral_value():
        raise DatabaseLoadError(f"{field_name} must be an integer")
    return int(decimal)


def _require_frame(
    frame: object,
    required_columns: Sequence[str],
    dataset_name: str,
    business_keys: Sequence[str],
    *,
    uuid_columns: Sequence[str] = (),
    geometry_column: str | None = None,
) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{dataset_name} must be a pandas DataFrame")
    missing = tuple(column for column in required_columns if column not in frame.columns)
    if missing:
        raise DatabaseLoadError(
            f"{dataset_name} is missing required columns: {missing}"
        )
    if frame.loc[:, list(business_keys)].isna().any().any():
        raise DatabaseLoadError(f"{dataset_name} business keys cannot be null")
    if frame.duplicated(subset=list(business_keys), keep=False).any():
        raise DatabaseLoadError(f"{dataset_name} business keys must be unique")
    for column in uuid_columns:
        for value in frame[column]:
            _uuid_value(value, column)
    if geometry_column is not None:
        values = frame[geometry_column].astype("string").str.strip()
        if values.isna().any() or values.eq("").any():
            raise DatabaseLoadError(
                f"{dataset_name} geometry WKT values cannot be missing"
            )
    return frame


class PostgresLoader:
    """Psycopg loader using COPY staging and deterministic non-destructive upserts."""

    def __init__(
        self,
        *,
        connection: Connection[Any] | None = None,
        database_url: str | None = None,
        config: DatabaseLoaderConfig | None = None,
        connect: Callable[..., Connection[Any]] = psycopg.connect,
    ) -> None:
        if connection is not None and database_url is not None:
            raise ValueError("provide connection or database_url, not both")
        self.config = config or DatabaseLoaderConfig(database_url=database_url)
        if database_url is not None and self.config.database_url not in (None, database_url):
            raise ValueError("database_url conflicts with config.database_url")
        self._connection = connection
        self._owns_connection = False
        self._connect = connect
        self._validated = False

    @property
    def connection(self) -> Connection[Any]:
        """Return the active connection, opening and validating it if needed."""

        if self._connection is None:
            database_url = self.config.database_url or _environment_database_url(os.environ)
            try:
                self._connection = self._connect(database_url, autocommit=True)
            except Exception as error:
                safe = _safe_error(error, self.config.error_message_max_length)
                raise DatabaseLoadError(f"database connection failed: {safe}") from error
            self._owns_connection = True
        if not self._validated:
            self._validate_database()
            self._validated = True
        return self._connection

    @property
    def owns_connection(self) -> bool:
        """Whether this loader created the active connection."""

        return self._owns_connection

    def __enter__(self) -> PostgresLoader:
        self.connection
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        """Close only a loader-owned connection."""

        if self._connection is not None and self._owns_connection:
            self._connection.close()
            self._connection = None
            self._owns_connection = False
            self._validated = False

    def _validate_database(self) -> None:
        connection = self._connection
        assert connection is not None
        try:
            with connection.transaction():
                migrations = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations"
                    ).fetchall()
                }
                missing = sorted(set(self.config.required_migration_versions) - migrations)
                if missing:
                    raise DatabaseLoadError(
                        f"required database migrations are missing: {missing}"
                    )
                extensions = {
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT extname FROM pg_extension
                        WHERE extname IN ('postgis', 'pgrouting')
                        """
                    ).fetchall()
                }
                missing_extensions = sorted({"postgis", "pgrouting"} - extensions)
                if missing_extensions:
                    raise DatabaseLoadError(
                        f"required database extensions are missing: {missing_extensions}"
                    )
        except DatabaseLoadError:
            raise
        except Exception as error:
            raise DatabaseLoadError("database schema validation failed") from error

    def _temporary_name(self, table_name: str) -> str:
        return f"stage_{table_name}_{uuid4().hex}"

    def _create_stage(self, name: str, definitions: Sequence[tuple[str, str]]) -> None:
        columns = sql.SQL(", ").join(
            sql.SQL("{} {}").format(sql.Identifier(column), sql.SQL(data_type))
            for column, data_type in definitions
        )
        self.connection.execute(
            sql.SQL("CREATE TEMP TABLE {} ({}) ON COMMIT DROP").format(
                sql.Identifier(name), columns
            )
        )

    def _copy_rows(
        self,
        stage_name: str,
        columns: Sequence[str],
        rows: Iterable[Sequence[object]],
    ) -> int:
        statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
            sql.Identifier(stage_name),
            sql.SQL(", ").join(map(sql.Identifier, columns)),
        )
        count = 0
        with self.connection.cursor() as cursor:
            with cursor.copy(statement) as copy:
                for row in rows:
                    copy.write_row(tuple(_python_value(value) for value in row))
                    count += 1
        return count

    def _final_count(self, table_name: str) -> int:
        return int(
            self.connection.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table_name))
            ).fetchone()[0]
        )

    def _run_upsert(
        self,
        *,
        table_name: str,
        stage_name: str,
        target_columns: Sequence[str],
        select_expressions: Sequence[sql.Composable],
        conflict_columns: Sequence[str],
        update_columns: Sequence[str],
    ) -> int:
        assignments = sql.SQL(", ").join(
            sql.SQL("{} = EXCLUDED.{}").format(
                sql.Identifier(column), sql.Identifier(column)
            )
            for column in update_columns
        )
        changes = sql.SQL(" OR ").join(
            sql.SQL("{}.{} IS DISTINCT FROM EXCLUDED.{}").format(
                sql.Identifier(table_name),
                sql.Identifier(column),
                sql.Identifier(column),
            )
            for column in update_columns
        )
        statement = sql.SQL(
            "INSERT INTO {} ({}) SELECT {} FROM {} "
            "ON CONFLICT ({}) DO UPDATE SET {} WHERE {}"
        ).format(
            sql.Identifier(table_name),
            sql.SQL(", ").join(map(sql.Identifier, target_columns)),
            sql.SQL(", ").join(select_expressions),
            sql.Identifier(stage_name),
            sql.SQL(", ").join(map(sql.Identifier, conflict_columns)),
            assignments,
            changes,
        )
        cursor = self.connection.execute(statement)
        return max(int(cursor.rowcount), 0)

    def _frame_rows(
        self, frame: pd.DataFrame, columns: Sequence[str]
    ) -> Iterable[tuple[object, ...]]:
        return frame.loc[:, list(columns)].itertuples(index=False, name=None)

    def load_sensors(self, frame: pd.DataFrame) -> TableLoadResult:
        """COPY/upsert transformed canonical sensors by ``sensor_id``."""

        frame = _require_frame(
            frame, SENSOR_DIMENSION_COLUMNS, "sensors", ("sensor_id",),
            geometry_column="geometry_wkt",
        )
        stage = self._temporary_name("sensors")
        definitions = (
            ("sensor_id", "BIGINT"), ("sensor_name", "TEXT"),
            ("sensor_description", "TEXT"), ("installation_date", "DATE"),
            ("note", "TEXT"), ("location_type", "TEXT"), ("status", "TEXT"),
            ("latitude", "DOUBLE PRECISION"), ("longitude", "DOUBLE PRECISION"),
            ("geometry_wkt", "TEXT"),
        )
        self._create_stage(stage, definitions)
        staged = self._copy_rows(stage, [value[0] for value in definitions], self._frame_rows(frame, SENSOR_DIMENSION_COLUMNS))
        target = [column for column in SENSOR_DIMENSION_COLUMNS if column != "geometry_wkt"] + ["geometry"]
        select = [sql.Identifier(column) for column in SENSOR_DIMENSION_COLUMNS if column != "geometry_wkt"] + [sql.SQL("ST_GeomFromText(geometry_wkt, 4326)")]
        update = [column for column in target if column != "sensor_id"]
        affected = self._run_upsert(table_name="sensors", stage_name=stage, target_columns=target, select_expressions=select, conflict_columns=("sensor_id",), update_columns=update)
        return TableLoadResult("sensors", len(frame), staged, affected, self._final_count("sensors"))

    def load_sensor_directions(self, frame: pd.DataFrame) -> TableLoadResult:
        """COPY/upsert sensor direction configurations by composite key."""

        frame = _require_frame(frame, SENSOR_DIRECTION_TABLE_COLUMNS, "sensor directions", ("sensor_id", "direction_config_id"))
        stage = self._temporary_name("sensor_directions")
        definitions = (("sensor_id", "BIGINT"), ("direction_config_id", "BIGINT"), ("direction_1_label", "TEXT"), ("direction_2_label", "TEXT"))
        self._create_stage(stage, definitions)
        staged = self._copy_rows(stage, SENSOR_DIRECTION_TABLE_COLUMNS, self._frame_rows(frame, SENSOR_DIRECTION_TABLE_COLUMNS))
        affected = self._run_upsert(table_name="sensor_directions", stage_name=stage, target_columns=SENSOR_DIRECTION_TABLE_COLUMNS, select_expressions=[sql.Identifier(value) for value in SENSOR_DIRECTION_TABLE_COLUMNS], conflict_columns=("sensor_id", "direction_config_id"), update_columns=("direction_1_label", "direction_2_label"))
        return TableLoadResult("sensor_directions", len(frame), staged, affected, self._final_count("sensor_directions"))

    def load_landmarks(self, frame: pd.DataFrame) -> TableLoadResult:
        """COPY/upsert transformed landmarks by stable UUID5 identifier."""

        frame = _require_frame(frame, LANDMARK_DIMENSION_COLUMNS, "landmarks", ("landmark_id",), uuid_columns=("landmark_id",), geometry_column="geometry_wkt")
        stage = self._temporary_name("landmarks")
        definitions = (("landmark_id", "UUID"), ("name", "TEXT"), ("category", "TEXT"), ("subcategory", "TEXT"), ("latitude", "DOUBLE PRECISION"), ("longitude", "DOUBLE PRECISION"), ("geometry_wkt", "TEXT"))
        self._create_stage(stage, definitions)
        staged = self._copy_rows(stage, [value[0] for value in definitions], self._frame_rows(frame, LANDMARK_DIMENSION_COLUMNS))
        target = [column for column in LANDMARK_DIMENSION_COLUMNS if column != "geometry_wkt"] + ["geometry"]
        select = [sql.Identifier(column) for column in LANDMARK_DIMENSION_COLUMNS if column != "geometry_wkt"] + [sql.SQL("ST_GeomFromText(geometry_wkt, 4326)")]
        affected = self._run_upsert(table_name="landmarks", stage_name=stage, target_columns=target, select_expressions=select, conflict_columns=("landmark_id",), update_columns=tuple(column for column in target if column != "landmark_id"))
        return TableLoadResult("landmarks", len(frame), staged, affected, self._final_count("landmarks"))

    def load_hourly_chunks(
        self, chunks: Iterable[pd.DataFrame], pipeline_run_id: str
    ) -> TableLoadResult:
        """COPY all transformed hourly chunks to one database staging table."""

        if isinstance(chunks, pd.DataFrame):
            raise TypeError("hourly input must be a chunk iterable, not a DataFrame")
        stage = self._temporary_name("pedestrian_counts_hourly")
        definitions = (
            ("source_record_id", "TEXT"), ("sensor_id", "BIGINT"),
            ("sensing_date", "DATE"), ("hour", "SMALLINT"),
            ("local_observation_datetime", "TIMESTAMP WITHOUT TIME ZONE"),
            ("year", "INTEGER"), ("month", "SMALLINT"),
            ("iso_weekday", "SMALLINT"), ("weekday_name", "TEXT"),
            ("is_weekend", "BOOLEAN"), ("direction_1_count", "BIGINT"),
            ("direction_2_count", "BIGINT"), ("pedestrian_count", "BIGINT"),
        )
        self._create_stage(stage, definitions)
        source_count = 0
        chunk_count = 0
        for chunk in chunks:
            chunk = _require_frame(chunk, HOURLY_FACT_COLUMNS, "hourly pedestrian counts", ("sensor_id", "sensing_date", "hour"))
            staged_chunk = self._copy_rows(stage, [value[0] for value in definitions], self._frame_rows(chunk, HOURLY_FACT_COLUMNS))
            source_count += len(chunk)
            chunk_count += 1
            if staged_chunk != len(chunk):
                raise DatabaseLoadError("hourly staged row count does not match source")
        if chunk_count == 0:
            raise DatabaseLoadError("hourly chunk iterable produced no chunks")
        staged = int(self.connection.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(stage))).fetchone()[0])
        duplicate_business = self.connection.execute(sql.SQL("SELECT sensor_id, sensing_date, hour FROM {} GROUP BY sensor_id, sensing_date, hour HAVING count(*) > 1 LIMIT 1").format(sql.Identifier(stage))).fetchone()
        duplicate_source = self.connection.execute(sql.SQL("SELECT source_record_id FROM {} GROUP BY source_record_id HAVING count(*) > 1 LIMIT 1").format(sql.Identifier(stage))).fetchone()
        if duplicate_business or duplicate_source:
            raise DatabaseLoadError("hourly staging contains duplicate business or source keys")
        business_conflict = self.connection.execute(sql.SQL("SELECT 1 FROM pedestrian_counts_hourly AS target JOIN {} AS staged USING (sensor_id, sensing_date, hour) WHERE target.source_record_id <> staged.source_record_id LIMIT 1").format(sql.Identifier(stage))).fetchone()
        source_conflict = self.connection.execute(sql.SQL("SELECT 1 FROM pedestrian_counts_hourly AS target JOIN {} AS staged USING (source_record_id) WHERE (target.sensor_id, target.sensing_date, target.hour) IS DISTINCT FROM (staged.sensor_id, staged.sensing_date, staged.hour) LIMIT 1").format(sql.Identifier(stage))).fetchone()
        if business_conflict or source_conflict:
            raise DatabaseLoadError("hourly source-record identity conflicts with an existing business key")
        target = [*HOURLY_FACT_COLUMNS, "pipeline_run_id"]
        select = [sql.Identifier(column) for column in HOURLY_FACT_COLUMNS] + [
            sql.SQL("{}::uuid").format(sql.Literal(pipeline_run_id))
        ]
        update = [column for column in target if column not in ("sensor_id", "sensing_date", "hour", "source_record_id")]
        affected = self._run_upsert(table_name="pedestrian_counts_hourly", stage_name=stage, target_columns=target, select_expressions=select, conflict_columns=("sensor_id", "sensing_date", "hour"), update_columns=update)
        return TableLoadResult("pedestrian_counts_hourly", source_count, staged, affected, self._final_count("pedestrian_counts_hourly"), (f"consumed {chunk_count} chunk(s)",))

    def load_crowd_baselines(self, frame: pd.DataFrame, pipeline_run_id: str) -> TableLoadResult:
        """COPY/upsert descriptive crowd baselines by baseline business key."""

        frame = _require_frame(frame, BASELINE_COLUMNS, "crowd baselines", ("sensor_id", "iso_weekday", "hour"))
        stage = self._temporary_name("crowd_baselines")
        integer_columns = {"sensor_id", "iso_weekday", "hour", "observation_count", "minimum_pedestrian_count", "maximum_pedestrian_count"}
        date_columns = {"minimum_sensing_date", "maximum_sensing_date"}
        definitions = tuple((column, "BIGINT" if column in integer_columns else "DATE" if column in date_columns else "DOUBLE PRECISION") for column in BASELINE_COLUMNS)
        self._create_stage(stage, definitions)
        staged = self._copy_rows(stage, BASELINE_COLUMNS, self._frame_rows(frame, BASELINE_COLUMNS))
        target = [*BASELINE_COLUMNS, "pipeline_run_id"]
        select = [sql.Identifier(column) for column in BASELINE_COLUMNS] + [
            sql.SQL("{}::uuid").format(sql.Literal(pipeline_run_id))
        ]
        update = [column for column in target if column not in ("sensor_id", "iso_weekday", "hour")]
        affected = self._run_upsert(table_name="crowd_baselines", stage_name=stage, target_columns=target, select_expressions=select, conflict_columns=("sensor_id", "iso_weekday", "hour"), update_columns=update)
        return TableLoadResult("crowd_baselines", len(frame), staged, affected, self._final_count("crowd_baselines"))

    def _node_id_mapping(self) -> dict[str, int]:
        return {str(node_uuid): int(node_id) for node_uuid, node_id in self.connection.execute("SELECT node_uuid, id FROM routing_nodes").fetchall()}

    def load_routing_nodes(self, frame: pd.DataFrame) -> tuple[TableLoadResult, dict[str, int]]:
        """COPY/upsert stable node UUIDs and return database integer IDs."""

        frame = _require_frame(frame, ROUTING_NODE_COLUMNS, "routing nodes", ("node_id",), uuid_columns=("node_id",), geometry_column="geometry_wkt")
        stage = self._temporary_name("routing_nodes")
        definitions = (("node_uuid", "UUID"), ("source_object_id", "BIGINT"), ("source_network_id", "BIGINT"), ("longitude", "DOUBLE PRECISION"), ("latitude", "DOUBLE PRECISION"), ("geometry_wkt", "TEXT"), ("component_id", "BIGINT"), ("is_primary_component", "BOOLEAN"), ("node_origin", "TEXT"))
        self._create_stage(stage, definitions)
        rows = (
            (
                row.node_id,
                _bigint_value(row.source_object_id, "source_object_id", nullable=True),
                _bigint_value(row.source_network_id, "source_network_id", nullable=True),
                row.longitude,
                row.latitude,
                row.geometry_wkt,
                _bigint_value(row.component_id, "component_id"),
                row.is_primary_component,
                row.node_origin,
            )
            for row in frame.loc[:, ROUTING_NODE_COLUMNS].itertuples(index=False)
        )
        staged = self._copy_rows(stage, [value[0] for value in definitions], rows)
        target = ("node_uuid", "source_object_id", "source_network_id", "longitude", "latitude", "geometry", "component_id", "is_primary_component", "node_origin")
        select = [sql.Identifier(column) for column in target if column != "geometry"]
        select.insert(5, sql.SQL("ST_GeomFromText(geometry_wkt, 4326)"))
        update = tuple(column for column in target if column != "node_uuid")
        affected = self._run_upsert(table_name="routing_nodes", stage_name=stage, target_columns=target, select_expressions=select, conflict_columns=("node_uuid",), update_columns=update)
        mapping = self._node_id_mapping()
        expected = {str(UUID(str(value))) for value in frame["node_id"]}
        if not expected <= set(mapping):
            raise DatabaseLoadError("routing node UUID-to-BIGINT resolution is incomplete")
        return TableLoadResult("routing_nodes", len(frame), staged, affected, self._final_count("routing_nodes")), mapping

    def load_routing_edges(self, frame: pd.DataFrame, node_ids: Mapping[str, int]) -> TableLoadResult:
        """Resolve endpoint UUIDs then COPY/upsert routing edges by edge UUID."""

        frame = _require_frame(frame, ROUTING_EDGE_COLUMNS, "routing edges", ("edge_id",), uuid_columns=("edge_id", "source_node_id", "target_node_id"), geometry_column="geometry_wkt")
        referenced = {str(UUID(str(value))) for column in ("source_node_id", "target_node_id") for value in frame[column]}
        missing = sorted(referenced - set(node_ids))
        if missing:
            raise DatabaseLoadError(f"routing edges reference {len(missing)} unresolved node UUID(s)")
        stage = self._temporary_name("routing_edges")
        definitions = (("edge_uuid", "UUID"), ("source_object_id", "BIGINT"), ("source_network_id", "BIGINT"), ("source", "BIGINT"), ("target", "BIGINT"), ("length_m", "DOUBLE PRECISION"), ("cost", "DOUBLE PRECISION"), ("reverse_cost", "DOUBLE PRECISION"), ("geometry_wkt", "TEXT"), ("component_id", "BIGINT"), ("is_primary_component", "BOOLEAN"), ("duplicate_geometry", "BOOLEAN"))
        self._create_stage(stage, definitions)
        def rows() -> Iterable[tuple[object, ...]]:
            for row in frame.loc[:, ROUTING_EDGE_COLUMNS].itertuples(index=False):
                yield (
                    row.edge_id,
                    _bigint_value(row.source_object_id, "source_object_id", nullable=True),
                    _bigint_value(row.source_network_id, "source_network_id", nullable=True),
                    node_ids[str(UUID(str(row.source_node_id)))],
                    node_ids[str(UUID(str(row.target_node_id)))],
                    row.length_m,
                    row.cost,
                    row.reverse_cost,
                    row.geometry_wkt,
                    _bigint_value(row.component_id, "component_id"),
                    row.is_primary_component,
                    row.duplicate_geometry,
                )
        staged = self._copy_rows(stage, [value[0] for value in definitions], rows())
        target = ("edge_uuid", "source_object_id", "source_network_id", "source", "target", "length_m", "cost", "reverse_cost", "geometry", "component_id", "is_primary_component", "duplicate_geometry")
        select = [sql.Identifier(column) for column in target if column != "geometry"]
        select.insert(8, sql.SQL("ST_GeomFromText(geometry_wkt, 4326)"))
        affected = self._run_upsert(table_name="routing_edges", stage_name=stage, target_columns=target, select_expressions=select, conflict_columns=("edge_uuid",), update_columns=tuple(column for column in target if column != "edge_uuid"))
        return TableLoadResult("routing_edges", len(frame), staged, affected, self._final_count("routing_edges"))

    def load_spatial_network(self, network: PedestrianNetworkResult) -> tuple[TableLoadResult, TableLoadResult, dict[str, int]]:
        """Load nodes before edges and preserve database-generated integer IDs."""

        if not isinstance(network, PedestrianNetworkResult):
            raise TypeError("network must be a PedestrianNetworkResult")
        node_result, node_ids = self.load_routing_nodes(network.nodes)
        edge_result = self.load_routing_edges(network.edges, node_ids)
        return node_result, edge_result, node_ids

    def _load_mapping(self, frame: pd.DataFrame, node_ids: Mapping[str, int], *, landmark: bool) -> TableLoadResult:
        columns = LANDMARK_MAPPING_COLUMNS if landmark else SENSOR_MAPPING_COLUMNS
        id_column = "landmark_id" if landmark else "sensor_id"
        table_name = "landmark_network_map" if landmark else "sensor_network_map"
        frame = _require_frame(frame, columns, table_name, (id_column,), uuid_columns=((id_column, "node_id") if landmark else ("node_id",)))
        referenced = {str(UUID(str(value))) for value in frame["node_id"]}
        missing = referenced - set(node_ids)
        if missing:
            raise DatabaseLoadError(f"{table_name} references {len(missing)} unresolved node UUID(s)")
        stage = self._temporary_name(table_name)
        id_type = "UUID" if landmark else "BIGINT"
        definitions = ((id_column, id_type), ("node_id", "BIGINT"), ("snap_distance_m", "DOUBLE PRECISION"), ("within_snap_threshold", "BOOLEAN"), ("component_id", "BIGINT"), ("is_primary_component", "BOOLEAN"))
        self._create_stage(stage, definitions)
        rows = (
            (
                (
                    getattr(row, id_column)
                    if landmark
                    else _bigint_value(getattr(row, id_column), id_column)
                ),
                node_ids[str(UUID(str(row.node_id)))],
                row.snap_distance_m,
                row.within_snap_threshold,
                _bigint_value(row.network_component_id, "network_component_id"),
                row.is_primary_component,
            )
            for row in frame.loc[:, columns].itertuples(index=False)
        )
        staged = self._copy_rows(stage, [value[0] for value in definitions], rows)
        target = tuple(value[0] for value in definitions)
        affected = self._run_upsert(table_name=table_name, stage_name=stage, target_columns=target, select_expressions=[sql.Identifier(value) for value in target], conflict_columns=(id_column,), update_columns=tuple(value for value in target if value != id_column))
        return TableLoadResult(table_name, len(frame), staged, affected, self._final_count(table_name))

    def load_network_mappings(self, sensor_mapping: SpatialMappingResult | pd.DataFrame, landmark_mapping: SpatialMappingResult | pd.DataFrame, node_ids: Mapping[str, int]) -> tuple[TableLoadResult, TableLoadResult]:
        """Resolve stable node UUIDs for sensor and landmark mapping tables."""

        sensors = sensor_mapping.mappings if isinstance(sensor_mapping, SpatialMappingResult) else sensor_mapping
        landmarks = landmark_mapping.mappings if isinstance(landmark_mapping, SpatialMappingResult) else landmark_mapping
        return self._load_mapping(sensors, node_ids, landmark=False), self._load_mapping(landmarks, node_ids, landmark=True)

    def _start_pipeline_run(self) -> str:
        run_id = str(uuid4())
        with self.connection.transaction():
            self.connection.execute(
                """
                INSERT INTO pipeline_runs (
                    run_id, pipeline_name, status, source_fingerprint,
                    rows_processed, metadata
                ) VALUES (%s, %s, 'started', %s, 0, '{}'::jsonb)
                """,
                (run_id, self.config.pipeline_name, self.config.source_fingerprint),
            )
        return run_id

    def _finish_pipeline_run(self, run_id: str, results: Sequence[TableLoadResult]) -> None:
        metadata = {result.table_name: result.to_dict() for result in results}
        rows_processed = sum(result.source_count for result in results)
        self.connection.execute(
            """
            UPDATE pipeline_runs SET status = 'succeeded', completed_at = CURRENT_TIMESTAMP,
                rows_processed = %s, metadata = %s
            WHERE run_id = %s
            """,
            (rows_processed, Jsonb(metadata), run_id),
        )

    def _fail_pipeline_run(self, run_id: str, error: BaseException) -> None:
        safe = _safe_error(error, self.config.error_message_max_length)
        try:
            with self.connection.transaction():
                self.connection.execute(
                    """
                    UPDATE pipeline_runs SET status = 'failed',
                        completed_at = CURRENT_TIMESTAMP, error_message = %s
                    WHERE run_id = %s
                    """,
                    (safe, run_id),
                )
        except Exception:
            pass

    def _delete_dry_run(self, run_id: str) -> None:
        with self.connection.transaction():
            self.connection.execute("DELETE FROM pipeline_runs WHERE run_id = %s", (run_id,))

    def _verify_relationships(self) -> None:
        query = """
        SELECT
            (SELECT count(*) FROM pedestrian_counts_hourly h LEFT JOIN sensors s USING (sensor_id) WHERE s.sensor_id IS NULL)
          + (SELECT count(*) FROM crowd_baselines b LEFT JOIN sensors s USING (sensor_id) WHERE s.sensor_id IS NULL)
          + (SELECT count(*) FROM routing_edges e LEFT JOIN routing_nodes n ON n.id = e.source WHERE n.id IS NULL)
          + (SELECT count(*) FROM routing_edges e LEFT JOIN routing_nodes n ON n.id = e.target WHERE n.id IS NULL)
          + (SELECT count(*) FROM sensor_network_map m LEFT JOIN routing_nodes n ON n.id = m.node_id WHERE n.id IS NULL)
          + (SELECT count(*) FROM landmark_network_map m LEFT JOIN routing_nodes n ON n.id = m.node_id WHERE n.id IS NULL)
        """
        if int(self.connection.execute(query).fetchone()[0]) != 0:
            raise DatabaseLoadError("post-load relationship verification found orphan rows")

    def load_historical_dataset(
        self,
        *,
        sensors: pd.DataFrame,
        sensor_directions: pd.DataFrame,
        landmarks: pd.DataFrame,
        hourly_chunk_factory: Callable[[], Iterable[pd.DataFrame]],
        crowd_baselines: pd.DataFrame,
        spatial_network: PedestrianNetworkResult,
        sensor_mapping: SpatialMappingResult | pd.DataFrame,
        landmark_mapping: SpatialMappingResult | pd.DataFrame,
        validation_report: HistoricalValidationReport,
    ) -> HistoricalLoadResult:
        """Load a complete historical dataset in one business-data transaction."""

        if not isinstance(validation_report, HistoricalValidationReport):
            raise TypeError("validation_report must be a HistoricalValidationReport")
        if not validation_report.passed:
            raise DatabaseLoadError("historical validation report has errors")
        if not callable(hourly_chunk_factory):
            raise TypeError("hourly_chunk_factory must be callable")
        warnings = (
            (f"historical validation contains {validation_report.warning_count} warning(s)",)
            if validation_report.warning_count
            else ()
        )
        started = time.perf_counter()
        run_id = self._start_pipeline_run()
        results: list[TableLoadResult] = []
        try:
            try:
                with self.connection.transaction():
                    results.append(self.load_sensors(sensors))
                    results.append(self.load_sensor_directions(sensor_directions))
                    results.append(self.load_landmarks(landmarks))
                    results.append(self.load_hourly_chunks(hourly_chunk_factory(), run_id))
                    results.append(self.load_crowd_baselines(crowd_baselines, run_id))
                    node_result, edge_result, node_ids = self.load_spatial_network(spatial_network)
                    results.extend((node_result, edge_result))
                    sensor_result, landmark_result = self.load_network_mappings(sensor_mapping, landmark_mapping, node_ids)
                    results.extend((sensor_result, landmark_result))
                    for result in results:
                        expected = self.config.expected_source_counts.get(result.table_name)
                        if expected is not None and result.source_count != expected:
                            raise DatabaseLoadError(
                                f"{result.table_name} expected {expected} source rows; received {result.source_count}"
                            )
                    self._verify_relationships()
                    self._finish_pipeline_run(run_id, results)
                    if self.config.dry_run:
                        raise _DryRunRollback()
            except _DryRunRollback:
                self._delete_dry_run(run_id)
                return HistoricalLoadResult(run_id, "dry_run", tuple(results), time.perf_counter() - started, warnings, True)
        except Exception as error:
            self._fail_pipeline_run(run_id, error)
            safe = _safe_error(error, self.config.error_message_max_length)
            raise DatabaseLoadError(f"historical load failed: {safe}") from error
        return HistoricalLoadResult(run_id, "succeeded", tuple(results), time.perf_counter() - started, warnings, False)


def load_sensors(loader: PostgresLoader, frame: pd.DataFrame) -> TableLoadResult:
    return loader.load_sensors(frame)


def load_sensor_directions(loader: PostgresLoader, frame: pd.DataFrame) -> TableLoadResult:
    return loader.load_sensor_directions(frame)


def load_landmarks(loader: PostgresLoader, frame: pd.DataFrame) -> TableLoadResult:
    return loader.load_landmarks(frame)


def load_hourly_chunks(loader: PostgresLoader, chunks: Iterable[pd.DataFrame], pipeline_run_id: str) -> TableLoadResult:
    return loader.load_hourly_chunks(chunks, pipeline_run_id)


def load_crowd_baselines(loader: PostgresLoader, frame: pd.DataFrame, pipeline_run_id: str) -> TableLoadResult:
    return loader.load_crowd_baselines(frame, pipeline_run_id)


def load_spatial_network(loader: PostgresLoader, network: PedestrianNetworkResult) -> tuple[TableLoadResult, TableLoadResult, dict[str, int]]:
    return loader.load_spatial_network(network)


def load_network_mappings(loader: PostgresLoader, sensor_mapping: SpatialMappingResult | pd.DataFrame, landmark_mapping: SpatialMappingResult | pd.DataFrame, node_ids: Mapping[str, int]) -> tuple[TableLoadResult, TableLoadResult]:
    return loader.load_network_mappings(sensor_mapping, landmark_mapping, node_ids)


def load_historical_dataset(*, connection: Connection[Any] | None = None, database_url: str | None = None, config: DatabaseLoaderConfig | None = None, **dataset: object) -> HistoricalLoadResult:
    """Open a loader when needed and load one complete historical dataset."""

    with PostgresLoader(connection=connection, database_url=database_url, config=config) as loader:
        return loader.load_historical_dataset(**dataset)  # type: ignore[arg-type]


__all__ = [
    "LOAD_TABLE_ORDER",
    "REQUIRED_MIGRATION_VERSIONS",
    "DatabaseLoadError",
    "DatabaseLoaderConfig",
    "HistoricalLoadResult",
    "PostgresLoader",
    "TableLoadResult",
    "load_crowd_baselines",
    "load_historical_dataset",
    "load_hourly_chunks",
    "load_landmarks",
    "load_network_mappings",
    "load_sensor_directions",
    "load_sensors",
    "load_spatial_network",
]
