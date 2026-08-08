"""Unit tests for CityFlow versioned PostgreSQL/PostGIS migrations."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import sys

import pytest


REPOSITORY = Path(__file__).resolve().parents[2]
DATABASE = REPOSITORY / "database"
MIGRATIONS = DATABASE / "migrations"


def load_runner():
    spec = importlib.util.spec_from_file_location(
        "cityflow_database_migrate", DATABASE / "migrate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = load_runner()


def migration_sql() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(MIGRATIONS.glob("*.sql"))
    )


def normalized_sql() -> str:
    return re.sub(r"\s+", " ", migration_sql().lower())


def table_block(table: str) -> str:
    sql = migration_sql()
    match = re.search(
        rf"CREATE TABLE\s+{re.escape(table)}\s*\((.*?)\n\);",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert match is not None, f"missing table {table}"
    return match.group(1).lower()


def test_migration_discovery_is_version_ordered() -> None:
    migrations = runner.discover_migrations(MIGRATIONS)

    assert [migration.version for migration in migrations] == [1, 2, 3, 4]
    assert [migration.filename for migration in migrations] == [
        "001_extensions_and_core_tables.sql",
        "002_spatial_and_routing_tables.sql",
        "003_indexes_and_views.sql",
        "004_live_pedestrian_ingestion.sql",
    ]


def test_discovery_orders_numeric_versions_not_creation_order(tmp_path: Path) -> None:
    (tmp_path / "010_last.sql").write_text("SELECT 10;\n", encoding="utf-8")
    (tmp_path / "002_first.sql").write_text("SELECT 2;\n", encoding="utf-8")

    assert [value.version for value in runner.discover_migrations(tmp_path)] == [2, 10]


def test_invalid_filename_and_duplicate_version_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "001_valid.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (tmp_path / "readme.sql").write_text("SELECT 2;\n", encoding="utf-8")
    with pytest.raises(runner.MigrationError, match="invalid migration filename"):
        runner.discover_migrations(tmp_path)

    (tmp_path / "readme.sql").unlink()
    (tmp_path / "001_duplicate.sql").write_text("SELECT 3;\n", encoding="utf-8")
    with pytest.raises(runner.MigrationError, match="duplicate migration version"):
        runner.discover_migrations(tmp_path)


def test_checksum_is_deterministic_and_detects_changes() -> None:
    assert runner.calculate_checksum("SELECT 1;\n") == runner.calculate_checksum(
        b"SELECT 1;\n"
    )
    assert runner.calculate_checksum("SELECT 1;\n") != runner.calculate_checksum(
        "SELECT 2;\n"
    )


def test_tracking_returns_only_pending_migrations() -> None:
    migrations = runner.discover_migrations(MIGRATIONS)
    applied = {
        migrations[0].version: (
            migrations[0].filename,
            migrations[0].checksum,
        )
    }

    pending = runner.validate_applied_migrations(migrations, applied)

    assert [migration.version for migration in pending] == [2, 3, 4]


def test_modified_applied_migration_is_rejected() -> None:
    migrations = runner.discover_migrations(MIGRATIONS)
    applied = {1: (migrations[0].filename, "0" * 64)}

    with pytest.raises(runner.MigrationError, match="checksum changed"):
        runner.validate_applied_migrations(migrations, applied)


def test_renamed_or_missing_applied_migration_is_rejected() -> None:
    migrations = runner.discover_migrations(MIGRATIONS)
    with pytest.raises(runner.MigrationError, match="filename changed"):
        runner.validate_applied_migrations(
            migrations, {1: ("001_old_name.sql", migrations[0].checksum)}
        )
    with pytest.raises(runner.MigrationError, match="missing from source"):
        runner.validate_applied_migrations(migrations, {999: ("999_old.sql", "0" * 64)})


def test_database_url_resolution_supports_urls_and_existing_variables() -> None:
    assert runner.resolve_database_url(
        environment={"DATABASE_URL": "postgresql://example/db"}
    ) == "postgresql://example/db"
    assert runner.resolve_database_url(
        environment={"CITYFLOW_DATABASE_URL": "postgresql://cityflow/db"}
    ) == "postgresql://cityflow/db"
    built = runner.resolve_database_url(
        environment={
            "PGHOST": "db",
            "PGPORT": "5433",
            "PGDATABASE": "city flow",
            "PGUSER": "user@example",
            "PGPASSWORD": "not-a-real-value",
        }
    )
    assert built == "postgresql://user%40example:not-a-real-value@db:5433/city%20flow"


def test_missing_database_configuration_is_clear() -> None:
    with pytest.raises(runner.MigrationError, match="configuration is missing"):
        runner.resolve_database_url(environment={})


def test_required_extensions_and_tables_are_defined() -> None:
    sql = normalized_sql()
    assert "create extension if not exists postgis" in sql
    assert "create extension if not exists pgrouting" in sql
    for table in (
        "pipeline_runs",
        "sensors",
        "sensor_directions",
        "landmarks",
        "pedestrian_counts_hourly",
        "crowd_baselines",
        "routing_nodes",
        "routing_edges",
        "sensor_network_map",
        "landmark_network_map",
        "live_ingestion_runs",
        "pedestrian_counts_minutely_live",
        "pedestrian_counts_minutely_quarantine",
    ):
        assert f"create table {table}" in sql


def test_core_tables_contain_pipeline_output_columns() -> None:
    expected = {
        "pipeline_runs": (
            "run_id", "pipeline_name", "status", "started_at", "completed_at",
            "source_fingerprint", "rows_processed", "error_message", "metadata",
        ),
        "sensors": (
            "sensor_id", "sensor_name", "sensor_description", "installation_date",
            "note", "location_type", "status", "latitude", "longitude", "geometry",
        ),
        "pedestrian_counts_hourly": (
            "source_record_id", "sensor_id", "sensing_date", "hour",
            "local_observation_datetime", "year", "month", "iso_weekday",
            "weekday_name", "is_weekend", "direction_1_count", "direction_2_count",
            "pedestrian_count", "pipeline_run_id",
        ),
        "crowd_baselines": (
            "sensor_id", "iso_weekday", "hour", "observation_count",
            "minimum_sensing_date", "maximum_sensing_date",
            "minimum_pedestrian_count", "maximum_pedestrian_count",
            "mean_pedestrian_count", "median_pedestrian_count",
            "population_std_pedestrian_count", "percentile_25_pedestrian_count",
            "percentile_75_pedestrian_count", "percentile_90_pedestrian_count",
        ),
    }
    for table, columns in expected.items():
        block = table_block(table)
        for column in columns:
            assert re.search(rf"\b{re.escape(column)}\b", block)


def test_hourly_and_baseline_business_keys_are_unique() -> None:
    hourly = re.sub(r"\s+", " ", table_block("pedestrian_counts_hourly"))
    baseline = re.sub(r"\s+", " ", table_block("crowd_baselines"))
    assert "unique (sensor_id, sensing_date, hour)" in hourly
    assert "primary key (sensor_id, iso_weekday, hour)" in baseline


def test_foreign_keys_and_check_constraints_are_defined() -> None:
    sql = normalized_sql()
    for reference in (
        "references sensors(sensor_id)",
        "references pipeline_runs(run_id)",
        "references routing_nodes(id)",
        "references landmarks(landmark_id)",
    ):
        assert reference in sql
    assert "check (hour between 0 and 23)" in sql
    assert "check (iso_weekday between 1 and 7)" in sql
    assert "check (direction_1_count >= 0)" in sql
    assert "check (source <> target)" in sql
    assert "check (length_m > 0)" in sql
    assert "check (cost > 0)" in sql


def test_geometry_types_srid_and_spatial_indexes() -> None:
    sql = normalized_sql()
    assert sql.count("geometry(point, 4326)") >= 3
    assert "geometry(linestring, 4326)" in sql
    for table in ("sensors", "landmarks", "routing_nodes", "routing_edges"):
        assert re.search(
            rf"create index \w+ on {table} using gist \(geometry\)", sql
        )


def test_routing_tables_preserve_uuid_and_add_pgrouting_bigints() -> None:
    nodes = re.sub(r"\s+", " ", table_block("routing_nodes"))
    edges = re.sub(r"\s+", " ", table_block("routing_edges"))
    assert "id bigint generated always as identity primary key" in nodes
    assert "node_uuid uuid not null unique" in nodes
    assert "id bigint generated always as identity primary key" in edges
    assert "edge_uuid uuid not null unique" in edges
    assert "source bigint not null" in edges
    assert "target bigint not null" in edges


def test_routing_view_is_pgrouting_compatible_and_deterministic() -> None:
    sql = normalized_sql()
    assert "create view routing_edges_pgr as" in sql
    for column in ("id", "source", "target", "cost", "reverse_cost", "geometry"):
        assert re.search(rf"\b{column}\b", sql)
    assert "where edge.is_primary_component" in sql
    assert "row_number() over" in sql
    assert "partition by st_asewkb(st_normalize(edge.geometry))" in sql
    assert "order by edge.edge_uuid, edge.id" in sql
    assert "where duplicate_rank = 1" in sql


def test_crowd_feature_view_matches_python_boundaries() -> None:
    sql = normalized_sql()
    assert "create view hourly_crowd_features as" in sql
    assert "baseline.median_pedestrian_count = 0 then null" in sql
    assert "baseline.population_std_pedestrian_count > 0" in sql
    assert "else 0.0 end as z_score" in sql
    assert "< baseline.percentile_25_pedestrian_count then 'low'" in sql
    assert "> baseline.percentile_75_pedestrian_count then 'high'" in sql
    assert "else 'typical' end as crowd_level" in sql
    assert "baseline.sensor_id is null as baseline_missing" in sql
    assert "baseline.iso_weekday = hourly.iso_weekday" in sql


def test_reusable_updated_at_function_is_used() -> None:
    sql = normalized_sql()
    assert sql.count("create or replace function set_updated_at()") == 1
    assert sql.count("execute function set_updated_at()") >= 8


def test_indexes_support_hourly_routing_and_mapping_queries() -> None:
    sql = normalized_sql()
    assert "on pedestrian_counts_hourly (sensor_id, local_observation_datetime desc)" in sql
    assert "on routing_edges (source)" in sql
    assert "on routing_edges (target)" in sql
    assert "on sensor_network_map (node_id)" in sql
    assert "on landmark_network_map (node_id)" in sql


def test_schema_sql_is_only_a_migration_wrapper() -> None:
    schema = (DATABASE / "schema.sql").read_text(encoding="utf-8")
    assert "\\ir migrations/001_extensions_and_core_tables.sql" in schema
    assert "\\ir migrations/002_spatial_and_routing_tables.sql" in schema
    assert "\\ir migrations/003_indexes_and_views.sql" in schema
    assert "\\ir migrations/004_live_pedestrian_ingestion.sql" in schema
    assert "CREATE TABLE" not in schema.upper()


def test_runner_has_no_credentials_or_data_loading() -> None:
    source = (DATABASE / "migrate.py").read_text(encoding="utf-8")
    lowered = source.lower()
    assert "hard-coded-password" not in lowered
    assert "copy pedestrian" not in lowered
    assert "load.py" not in lowered
    assert "insert into schema_migrations" in lowered
    assert "with connection.transaction()" in lowered


def test_environment_example_contains_names_not_real_credentials() -> None:
    example = (REPOSITORY / "data_pipeline/.env.example").read_text(encoding="utf-8")
    assert "DATABASE_URL=" in example
    assert "PGPASSWORD=" in example
    assert "postgresql://" not in example
