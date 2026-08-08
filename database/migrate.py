"""Lightweight transactional PostgreSQL migration runner for CityFlow."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote

import psycopg
from psycopg import Connection


MIGRATION_PATTERN = re.compile(r"^(?P<version>\d{3,})_(?P<name>[a-z0-9_]+)\.sql$")
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
TRACKING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE,
    checksum CHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT schema_migrations_checksum_format
        CHECK (checksum ~ '^[0-9a-f]{64}$')
)
"""


class MigrationError(RuntimeError):
    """Raised when migration discovery or integrity validation fails."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One immutable versioned SQL migration."""

    version: int
    filename: str
    path: Path
    checksum: str
    sql: str


def calculate_checksum(content: bytes | str) -> str:
    """Return a deterministic SHA-256 checksum for migration content."""

    value = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(value).hexdigest()


def discover_migrations(directory: str | Path = DEFAULT_MIGRATIONS_DIR) -> tuple[Migration, ...]:
    """Discover strictly named migration files in numeric version order."""

    root = Path(directory)
    if not root.is_dir():
        raise MigrationError(f"migration directory does not exist: {root}")
    migrations: list[Migration] = []
    seen_versions: dict[int, str] = {}
    for path in sorted(root.glob("*.sql")):
        match = MIGRATION_PATTERN.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"invalid migration filename: {path.name}")
        version = int(match.group("version"))
        if version in seen_versions:
            raise MigrationError(
                f"duplicate migration version {version}: "
                f"{seen_versions[version]} and {path.name}"
            )
        content = path.read_bytes()
        seen_versions[version] = path.name
        migrations.append(
            Migration(
                version=version,
                filename=path.name,
                path=path,
                checksum=calculate_checksum(content),
                sql=content.decode("utf-8-sig"),
            )
        )
    if not migrations:
        raise MigrationError(f"no migration files found in {root}")
    migrations.sort(key=lambda migration: (migration.version, migration.filename))
    return tuple(migrations)


def _environment_url(environment: Mapping[str, str]) -> str:
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
        raise MigrationError(
            "database configuration is missing; set DATABASE_URL, "
            "CITYFLOW_DATABASE_URL, or PostgreSQL environment variables"
        )
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@"
        f"{host}:{port}/{quote(database, safe='')}"
    )


def resolve_database_url(
    explicit_url: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Resolve a connection URL without embedding credentials in source."""

    if explicit_url is not None and explicit_url.strip():
        return explicit_url.strip()
    return _environment_url(os.environ if environment is None else environment)


def validate_applied_migrations(
    migrations: Sequence[Migration],
    applied: Mapping[int, tuple[str, str]],
) -> tuple[Migration, ...]:
    """Reject modified history and return migrations that remain pending."""

    available = {migration.version: migration for migration in migrations}
    unknown = sorted(set(applied) - set(available))
    if unknown:
        raise MigrationError(
            f"database contains migration versions missing from source: {unknown}"
        )
    pending: list[Migration] = []
    for migration in migrations:
        tracked = applied.get(migration.version)
        if tracked is None:
            pending.append(migration)
            continue
        filename, checksum = tracked
        if filename != migration.filename:
            raise MigrationError(
                f"migration {migration.version} filename changed: "
                f"database has {filename}, source has {migration.filename}"
            )
        if checksum != migration.checksum:
            raise MigrationError(
                f"migration {migration.version} checksum changed for {migration.filename}"
            )
    return tuple(pending)


def _applied_migrations(connection: Connection[object]) -> dict[int, tuple[str, str]]:
    rows = connection.execute(
        "SELECT version, filename, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {int(version): (str(filename), str(checksum)) for version, filename, checksum in rows}


def apply_migrations(
    database_url: str,
    migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR,
    *,
    connect: Callable[..., Connection[object]] = psycopg.connect,
    log: Callable[[str], None] = print,
) -> tuple[int, ...]:
    """Apply pending migrations once, transactionally, and record checksums."""

    migrations = discover_migrations(migrations_dir)
    applied_versions: list[int] = []
    with connect(database_url, autocommit=True) as connection:
        with connection.transaction():
            connection.execute(TRACKING_TABLE_SQL)
        tracked = _applied_migrations(connection)
        pending = validate_applied_migrations(migrations, tracked)
        for migration in migrations:
            if migration not in pending:
                log(f"skip {migration.filename} (already applied)")
                continue
            log(f"apply {migration.filename}")
            with connection.transaction():
                connection.execute(migration.sql)
                connection.execute(
                    """
                    INSERT INTO schema_migrations (version, filename, checksum)
                    VALUES (%s, %s, %s)
                    """,
                    (migration.version, migration.filename, migration.checksum),
                )
            applied_versions.append(migration.version)
            log(f"applied {migration.filename}")
    if not applied_versions:
        log("database schema is up to date")
    return tuple(applied_versions)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="PostgreSQL URL; prefer DATABASE_URL in shell history-sensitive contexts",
    )
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=DEFAULT_MIGRATIONS_DIR,
        help="Directory containing versioned SQL migrations",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run migrations and return a process-compatible exit status."""

    args = _parser().parse_args(argv)
    try:
        database_url = resolve_database_url(args.database_url)
        apply_migrations(database_url, args.migrations_dir)
    except (MigrationError, psycopg.Error, UnicodeError) as error:
        print(f"migration failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
