"""Bounded, deterministic ingestion of City of Melbourne live pedestrian data."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import json
import os
import random
import re
import time
from typing import Any, Final, Protocol
from urllib.parse import urlencode, urlsplit
from urllib.parse import quote as urlquote
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
from psycopg import Connection, sql
from psycopg.types.json import Jsonb


LIVE_DATASET_ID: Final = (
    "pedestrian-counting-system-past-hour-counts-per-minute"
)
LIVE_API_URL: Final = (
    "https://data.melbourne.vic.gov.au/api/explore/v2.1/catalog/datasets/"
    f"{LIVE_DATASET_ID}/records"
)
MELBOURNE: Final = ZoneInfo("Australia/Melbourne")
RETRYABLE_HTTP_STATUSES: Final = frozenset({408, 425, 429, 500, 502, 503, 504})


class LiveIngestionError(RuntimeError):
    """Raised when the live source, contract, or database load fails safely."""


@dataclass(frozen=True, slots=True)
class LiveSourceContract:
    """Immutable field contract for the verified City of Melbourne dataset."""

    dataset_id: str = LIVE_DATASET_ID
    endpoint: str = LIVE_API_URL
    required_fields: tuple[str, ...] = (
        "location_id",
        "sensing_datetime",
        "direction_1",
        "direction_2",
        "total_of_directions",
    )
    optional_fields: tuple[str, ...] = ("sensing_date", "sensing_time")

    @property
    def allowed_fields(self) -> frozenset[str]:
        """Return every source field allowed by the profiled contract."""

        return frozenset((*self.required_fields, *self.optional_fields))


@dataclass(frozen=True, slots=True)
class LiveIngestionConfig:
    """Safe operational bounds for one live ingestion run."""

    database_url: str | None = None
    contract: LiveSourceContract = field(default_factory=LiveSourceContract)
    initial_lookback_hours: float = 2.0
    overlap_minutes: int = 30
    request_budget: int = 250
    page_limit: int = 100
    maximum_partition_rows: int = 10_000
    minimum_partition_seconds: int = 60
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 20.0
    maximum_attempts: int = 4
    backoff_base_seconds: float = 0.25
    backoff_max_seconds: float = 8.0
    jitter_ratio: float = 0.2
    error_message_max_length: int = 1_000
    dry_run: bool = False

    def __post_init__(self) -> None:
        positive_numbers = {
            "initial_lookback_hours": self.initial_lookback_hours,
            "request_budget": self.request_budget,
            "page_limit": self.page_limit,
            "maximum_partition_rows": self.maximum_partition_rows,
            "minimum_partition_seconds": self.minimum_partition_seconds,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "read_timeout_seconds": self.read_timeout_seconds,
            "maximum_attempts": self.maximum_attempts,
            "error_message_max_length": self.error_message_max_length,
        }
        for name, value in positive_numbers.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.page_limit > 100:
            raise ValueError("page_limit cannot exceed the API maximum of 100")
        if self.maximum_partition_rows > 10_000:
            raise ValueError("maximum_partition_rows cannot exceed 10000")
        if isinstance(self.overlap_minutes, bool) or self.overlap_minutes < 0:
            raise ValueError("overlap_minutes must be a non-negative integer")
        if self.backoff_base_seconds < 0 or self.backoff_max_seconds < 0:
            raise ValueError("backoff values must be non-negative")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between zero and one")


@dataclass(frozen=True, slots=True)
class LiveIngestionResult:
    """JSON-serialisable metrics for a complete live ingestion attempt."""

    run_id: str | None
    status: str
    window_start_utc: datetime
    window_end_utc: datetime
    max_source_timestamp_utc: datetime | None
    records_fetched: int
    records_loaded: int
    records_unchanged: int
    records_quarantined: int
    exact_duplicates_collapsed: int
    request_count: int
    partition_count: int
    elapsed_seconds: float
    dry_run: bool = False
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic mapping accepted by ``json.dumps``."""

        return {
            "run_id": self.run_id,
            "status": self.status,
            "window_start_utc": self.window_start_utc.isoformat(),
            "window_end_utc": self.window_end_utc.isoformat(),
            "max_source_timestamp_utc": (
                None
                if self.max_source_timestamp_utc is None
                else self.max_source_timestamp_utc.isoformat()
            ),
            "records_fetched": self.records_fetched,
            "records_loaded": self.records_loaded,
            "records_unchanged": self.records_unchanged,
            "records_quarantined": self.records_quarantined,
            "exact_duplicates_collapsed": self.exact_duplicates_collapsed,
            "request_count": self.request_count,
            "partition_count": self.partition_count,
            "elapsed_seconds": self.elapsed_seconds,
            "dry_run": self.dry_run,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Small transport-neutral HTTP response used by deterministic tests."""

    status_code: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


class HttpTransport(Protocol):
    """Protocol implemented by the standard transport and test doubles."""

    def get(
        self,
        url: str,
        params: Mapping[str, str | int],
        *,
        connect_timeout: float,
        read_timeout: float,
    ) -> HttpResponse: ...


class StandardHttpTransport:
    """Standard-library HTTPS transport with separate connect/read timeouts."""

    def get(
        self,
        url: str,
        params: Mapping[str, str | int],
        *,
        connect_timeout: float,
        read_timeout: float,
    ) -> HttpResponse:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise LiveIngestionError("live endpoint must be an absolute HTTP(S) URL")
        query = urlencode(params)
        target = parsed.path or "/"
        if parsed.query:
            query = f"{parsed.query}&{query}"
        if query:
            target = f"{target}?{query}"
        connection_class = (
            http.client.HTTPSConnection if parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_class(
            parsed.hostname,
            parsed.port,
            timeout=connect_timeout,
        )
        try:
            connection.request("GET", target, headers={"Accept": "application/json"})
            if connection.sock is not None:
                connection.sock.settimeout(read_timeout)
            response = connection.getresponse()
            body = response.read()
            headers = {name.lower(): value for name, value in response.getheaders()}
            return HttpResponse(response.status, body, headers)
        finally:
            connection.close()


@dataclass(frozen=True, slots=True)
class _Page:
    total_count: int
    records: tuple[dict[str, object], ...]


class LiveApiClient:
    """Bounded Opendatasoft client with pagination, retries, and time splitting."""

    def __init__(
        self,
        config: LiveIngestionConfig | None = None,
        *,
        transport: HttpTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self.config = config or LiveIngestionConfig()
        self.transport = transport or StandardHttpTransport()
        self.sleep = sleep
        self.now = now
        self.random_value = random_value
        self.request_count = 0
        self.partition_count = 0

    def _consume_budget(self) -> None:
        if self.request_count >= self.config.request_budget:
            raise LiveIngestionError(
                f"live API request budget exhausted at {self.request_count} request(s)"
            )
        self.request_count += 1

    def _retry_delay(self, response: HttpResponse | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after") or response.headers.get(
                "Retry-After"
            )
            if retry_after:
                value = retry_after.strip()
                try:
                    return min(max(float(value), 0.0), self.config.backoff_max_seconds)
                except ValueError:
                    try:
                        parsed = parsedate_to_datetime(value)
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=UTC)
                        delay = (parsed.astimezone(UTC) - self.now().astimezone(UTC)).total_seconds()
                        return min(max(delay, 0.0), self.config.backoff_max_seconds)
                    except (TypeError, ValueError, OverflowError):
                        pass
        base = min(
            self.config.backoff_base_seconds * (2 ** attempt),
            self.config.backoff_max_seconds,
        )
        jitter = base * self.config.jitter_ratio * self.random_value()
        return min(base + jitter, self.config.backoff_max_seconds)

    def _request(self, params: Mapping[str, str | int]) -> HttpResponse:
        last_error: BaseException | None = None
        for attempt in range(self.config.maximum_attempts):
            self._consume_budget()
            response: HttpResponse | None = None
            try:
                response = self.transport.get(
                    self.config.contract.endpoint,
                    params,
                    connect_timeout=self.config.connect_timeout_seconds,
                    read_timeout=self.config.read_timeout_seconds,
                )
            except (OSError, TimeoutError, http.client.HTTPException) as error:
                last_error = error
                if attempt + 1 >= self.config.maximum_attempts:
                    break
                self.sleep(self._retry_delay(None, attempt))
                continue
            if 200 <= response.status_code < 300:
                return response
            if response.status_code not in RETRYABLE_HTTP_STATUSES:
                raise LiveIngestionError(
                    f"live API returned non-retryable HTTP {response.status_code}"
                )
            last_error = LiveIngestionError(
                f"live API returned retryable HTTP {response.status_code}"
            )
            if attempt + 1 >= self.config.maximum_attempts:
                break
            self.sleep(self._retry_delay(response, attempt))
        detail = _safe_error(last_error or RuntimeError("unknown HTTP failure"), 300)
        raise LiveIngestionError(f"live API request failed after retries: {detail}")

    def _page(self, start: datetime, end: datetime, offset: int) -> _Page:
        where = (
            f'sensing_datetime >= "{_api_timestamp(start)}" '
            f'and sensing_datetime < "{_api_timestamp(end)}"'
        )
        response = self._request(
            {
                "limit": self.config.page_limit,
                "offset": offset,
                "where": where,
                "order_by": "sensing_datetime ASC, location_id ASC",
            }
        )
        try:
            decoded = json.loads(response.body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise LiveIngestionError("live API returned invalid JSON") from error
        if not isinstance(decoded, dict):
            raise LiveIngestionError("live API response must be a JSON object")
        total = decoded.get("total_count")
        results = decoded.get("results")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise LiveIngestionError("live API total_count must be a non-negative integer")
        if not isinstance(results, list) or any(not isinstance(row, dict) for row in results):
            raise LiveIngestionError("live API results must be a list of objects")
        if len(results) > self.config.page_limit:
            raise LiveIngestionError("live API page exceeded the requested limit")
        return _Page(total, tuple(dict(row) for row in results))

    def _fetch_partition(
        self, start: datetime, end: datetime
    ) -> list[dict[str, object]]:
        first = self._page(start, end, 0)
        if first.total_count > self.config.maximum_partition_rows:
            duration = (end - start).total_seconds()
            if duration <= self.config.minimum_partition_seconds:
                raise LiveIngestionError(
                    "live API partition exceeds 10000 rows at minimum time resolution"
                )
            midpoint = start + (end - start) / 2
            return [
                *self._fetch_partition(start, midpoint),
                *self._fetch_partition(midpoint, end),
            ]

        self.partition_count += 1
        records = list(first.records)
        offset = len(first.records)
        while offset < first.total_count:
            if not records and first.total_count:
                raise LiveIngestionError("live API pagination returned an empty first page")
            page = self._page(start, end, offset)
            if page.total_count != first.total_count:
                raise LiveIngestionError(
                    "live API total_count changed during partition pagination"
                )
            if not page.records:
                raise LiveIngestionError("live API pagination ended before total_count")
            records.extend(page.records)
            offset += len(page.records)
        if len(records) != first.total_count:
            raise LiveIngestionError("live API pagination row count did not match total_count")
        return records

    def fetch_window(
        self, window_start_utc: datetime, window_end_utc: datetime
    ) -> tuple[dict[str, object], ...]:
        """Fetch one bounded half-open UTC window in deterministic source order."""

        start = _aware_utc(window_start_utc, "window_start_utc")
        end = _aware_utc(window_end_utc, "window_end_utc")
        if start >= end:
            raise ValueError("window_start_utc must be before window_end_utc")
        records = self._fetch_partition(start, end)
        previous: tuple[datetime, int] | None = None
        for payload in records:
            timestamp = _parse_timestamp(payload.get("sensing_datetime"))
            sensor_id = _integer(payload.get("location_id"), "location_id", positive=True)
            if not start <= timestamp < end:
                raise LiveIngestionError("live API returned a record outside the requested window")
            key = (timestamp, sensor_id)
            if previous is not None and key < previous:
                raise LiveIngestionError("live API results are not deterministically ordered")
            previous = key
        return tuple(records)


@dataclass(frozen=True, slots=True)
class _LiveRecord:
    sensor_id: int
    sensing_datetime_utc: datetime
    sensing_date_local: object
    sensing_time_local: object
    iso_weekday: int
    local_hour: int
    direction_1_count: int
    direction_2_count: int
    pedestrian_count: int
    source_dataset_id: str
    fingerprint: str
    original_payload: dict[str, object]

    @property
    def key(self) -> tuple[int, datetime]:
        return self.sensor_id, self.sensing_datetime_utc


@dataclass(frozen=True, slots=True)
class _QuarantineRecord:
    reason: str
    sensor_id: int | None
    sensing_datetime_utc: datetime | None
    fingerprint: str
    original_payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class _PreparedBatch:
    records: tuple[_LiveRecord, ...]
    quarantine: tuple[_QuarantineRecord, ...]
    fetched_count: int
    exact_duplicates_collapsed: int
    max_source_timestamp_utc: datetime | None


def canonical_payload_fingerprint(payload: Mapping[str, object]) -> str:
    """Return a stable SHA-256 fingerprint of canonical JSON source payload."""

    try:
        canonical = json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise LiveIngestionError("live payload is not canonical-JSON serialisable") from error
    return hashlib.sha256(canonical).hexdigest()


def _integer(value: object, field_name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or value is None:
        raise LiveIngestionError(f"{field_name} must be an integer")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise LiveIngestionError(f"{field_name} must be an integer") from error
    if not number.is_finite() or number != number.to_integral_value():
        raise LiveIngestionError(f"{field_name} must be an integer")
    result = int(number)
    if positive and result <= 0:
        raise LiveIngestionError(f"{field_name} must be positive")
    if not positive and result < 0:
        raise LiveIngestionError(f"{field_name} must be non-negative")
    return result


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise LiveIngestionError("sensing_datetime must be a timezone-aware ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise LiveIngestionError("sensing_datetime is malformed") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LiveIngestionError("sensing_datetime must be timezone-aware")
    return parsed.astimezone(UTC)


def _transform_payload(
    payload: Mapping[str, object], contract: LiveSourceContract
) -> _LiveRecord:
    keys = frozenset(payload)
    missing = sorted(set(contract.required_fields) - keys)
    unexpected = sorted(keys - contract.allowed_fields)
    if missing:
        raise LiveIngestionError(f"live payload missing required fields: {missing}")
    if unexpected:
        raise LiveIngestionError(f"live payload contains unexpected fields: {unexpected}")
    original = dict(payload)
    sensor_id = _integer(original["location_id"], "location_id", positive=True)
    timestamp = _parse_timestamp(original["sensing_datetime"])
    direction_1 = _integer(original["direction_1"], "direction_1")
    direction_2 = _integer(original["direction_2"], "direction_2")
    total = _integer(original["total_of_directions"], "total_of_directions")
    if direction_1 + direction_2 != total:
        raise LiveIngestionError(
            "direction_1 + direction_2 must equal total_of_directions"
        )
    local = timestamp.astimezone(MELBOURNE)
    return _LiveRecord(
        sensor_id=sensor_id,
        sensing_datetime_utc=timestamp,
        sensing_date_local=local.date(),
        sensing_time_local=local.time().replace(tzinfo=None),
        iso_weekday=local.isoweekday(),
        local_hour=local.hour,
        direction_1_count=direction_1,
        direction_2_count=direction_2,
        pedestrian_count=total,
        source_dataset_id=contract.dataset_id,
        fingerprint=canonical_payload_fingerprint(original),
        original_payload=original,
    )


def prepare_live_records(
    payloads: Sequence[Mapping[str, object]],
    contract: LiveSourceContract | None = None,
) -> _PreparedBatch:
    """Validate, transform, collapse exact duplicates, and quarantine conflicts."""

    source_contract = contract or LiveSourceContract()
    transformed = [_transform_payload(payload, source_contract) for payload in payloads]
    grouped: dict[tuple[int, datetime], list[_LiveRecord]] = defaultdict(list)
    for record in transformed:
        grouped[record.key].append(record)
    curated: list[_LiveRecord] = []
    quarantine: list[_QuarantineRecord] = []
    exact_duplicates = 0
    for key in sorted(grouped):
        records = sorted(grouped[key], key=lambda item: item.fingerprint)
        fingerprints = {record.fingerprint for record in records}
        if len(fingerprints) == 1:
            curated.append(records[0])
            exact_duplicates += len(records) - 1
            continue
        for record in records:
            quarantine.append(
                _QuarantineRecord(
                    "conflicting_duplicate",
                    record.sensor_id,
                    record.sensing_datetime_utc,
                    record.fingerprint,
                    record.original_payload,
                )
            )
    maximum = max(
        (record.sensing_datetime_utc for record in transformed),
        default=None,
    )
    return _PreparedBatch(
        records=tuple(curated),
        quarantine=tuple(quarantine),
        fetched_count=len(payloads),
        exact_duplicates_collapsed=exact_duplicates,
        max_source_timestamp_utc=maximum,
    )


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
        raise LiveIngestionError(
            "database configuration is missing; set DATABASE_URL, "
            "CITYFLOW_DATABASE_URL, or PostgreSQL environment variables"
        )
    return (
        f"postgresql://{urlquote(user, safe='')}:{urlquote(password, safe='')}@"
        f"{host}:{port}/{urlquote(database, safe='')}"
    )


def _safe_error(error: BaseException, maximum_length: int) -> str:
    value = re.sub(
        r"(?i)postgres(?:ql)?://[^\s]+",
        "<redacted-database-url>",
        str(error),
    )
    value = re.sub(
        r"(?i)\b(password|token|secret)\s*[=:]\s*[^\s,;]+",
        r"\1=<redacted>",
        value,
    )
    return (" ".join(value.split()) or error.__class__.__name__)[:maximum_length]


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _api_timestamp(value: datetime) -> str:
    return _aware_utc(value, "API timestamp").isoformat(timespec="seconds")


def determine_live_window(
    now_utc: datetime,
    last_successful_watermark: datetime | None,
    config: LiveIngestionConfig,
) -> tuple[datetime, datetime]:
    """Return the first-run lookback or overlapped incremental half-open window."""

    end = _aware_utc(now_utc, "now_utc")
    if last_successful_watermark is None:
        start = end - timedelta(hours=config.initial_lookback_hours)
    else:
        start = _aware_utc(
            last_successful_watermark, "last_successful_watermark"
        ) - timedelta(minutes=config.overlap_minutes)
    if start >= end:
        raise LiveIngestionError("calculated live ingestion window is empty")
    return start, end


class _LiveDatabase:
    def __init__(self, connection: Connection[Any], config: LiveIngestionConfig) -> None:
        self.connection = connection
        self.config = config

    def validate_schema(self) -> None:
        try:
            versions = {
                int(row[0])
                for row in self.connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
        except Exception as error:
            raise LiveIngestionError("database schema validation failed") from error
        if 4 not in versions:
            raise LiveIngestionError("required database migration 004 is missing")

    def watermark(self) -> datetime | None:
        row = self.connection.execute(
            """
            SELECT max(max_source_timestamp_utc)
            FROM live_ingestion_runs
            WHERE status IN ('succeeded', 'succeeded_with_warnings')
            """
        ).fetchone()
        return None if row is None or row[0] is None else _aware_utc(row[0], "watermark")

    def start_run(self, start: datetime, end: datetime) -> str:
        run_id = str(uuid4())
        with self.connection.transaction():
            self.connection.execute(
                """
                INSERT INTO live_ingestion_runs (
                    run_id, status, window_start_utc, window_end_utc
                ) VALUES (%s, 'running', %s, %s)
                """,
                (run_id, start, end),
            )
        return run_id

    def fail_run(self, run_id: str, error: BaseException) -> None:
        safe = _safe_error(error, self.config.error_message_max_length)
        try:
            with self.connection.transaction():
                self.connection.execute(
                    """
                    UPDATE live_ingestion_runs
                    SET status = 'failed', completed_at = CURRENT_TIMESTAMP,
                        error_message = %s
                    WHERE run_id = %s
                    """,
                    (safe, run_id),
                )
        except Exception:
            pass

    def delete_run(self, run_id: str) -> None:
        with self.connection.transaction():
            self.connection.execute(
                "DELETE FROM live_ingestion_runs WHERE run_id = %s", (run_id,)
            )

    def _insert_direct_quarantine(
        self, run_id: str, records: Sequence[_QuarantineRecord]
    ) -> int:
        if not records:
            return 0
        with self.connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO pedestrian_counts_minutely_quarantine (
                    issue_reason, location_id, sensing_datetime_utc,
                    source_payload_fingerprint, original_payload, live_run_id
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        item.reason,
                        item.sensor_id,
                        item.sensing_datetime_utc,
                        item.fingerprint,
                        Jsonb(item.original_payload),
                        run_id,
                    )
                    for item in records
                ],
            )
        return len(records)

    def load_batch(
        self, run_id: str, batch: _PreparedBatch
    ) -> tuple[int, int, int, str]:
        stage = f"stage_live_{uuid4().hex}"
        self.connection.execute("SELECT pg_advisory_xact_lock(hashtext('cityflow-live-ingestion'))")
        self.connection.execute(
            sql.SQL(
                """
                CREATE TEMP TABLE {} (
                    sensor_id BIGINT NOT NULL,
                    sensing_datetime_utc TIMESTAMPTZ NOT NULL,
                    sensing_date_local DATE NOT NULL,
                    sensing_time_local TIME WITHOUT TIME ZONE NOT NULL,
                    iso_weekday SMALLINT NOT NULL,
                    local_hour SMALLINT NOT NULL,
                    direction_1_count BIGINT NOT NULL,
                    direction_2_count BIGINT NOT NULL,
                    pedestrian_count BIGINT NOT NULL,
                    source_dataset_id TEXT NOT NULL,
                    source_payload_fingerprint CHAR(64) NOT NULL,
                    original_payload JSONB NOT NULL
                ) ON COMMIT DROP
                """
            ).format(sql.Identifier(stage))
        )
        statement = sql.SQL(
            "COPY {} (sensor_id, sensing_datetime_utc, sensing_date_local, "
            "sensing_time_local, iso_weekday, local_hour, direction_1_count, "
            "direction_2_count, pedestrian_count, source_dataset_id, "
            "source_payload_fingerprint, original_payload) FROM STDIN"
        ).format(sql.Identifier(stage))
        with self.connection.cursor() as cursor:
            with cursor.copy(statement) as copy:
                for item in batch.records:
                    copy.write_row(
                        (
                            item.sensor_id,
                            item.sensing_datetime_utc,
                            item.sensing_date_local,
                            item.sensing_time_local,
                            item.iso_weekday,
                            item.local_hour,
                            item.direction_1_count,
                            item.direction_2_count,
                            item.pedestrian_count,
                            item.source_dataset_id,
                            item.fingerprint,
                            Jsonb(item.original_payload),
                        )
                    )
        quarantined = self._insert_direct_quarantine(run_id, batch.quarantine)
        unknown = self.connection.execute(
            sql.SQL(
                """
                INSERT INTO pedestrian_counts_minutely_quarantine (
                    issue_reason, location_id, sensing_datetime_utc,
                    source_payload_fingerprint, original_payload, live_run_id
                )
                SELECT 'unknown_sensor', staged.sensor_id,
                    staged.sensing_datetime_utc,
                    staged.source_payload_fingerprint, staged.original_payload, %s
                FROM {} AS staged
                LEFT JOIN sensors AS sensor USING (sensor_id)
                WHERE sensor.sensor_id IS NULL
                """
            ).format(sql.Identifier(stage)),
            (run_id,),
        ).rowcount
        quarantined += max(int(unknown), 0)
        conflicts = self.connection.execute(
            sql.SQL(
                """
                INSERT INTO pedestrian_counts_minutely_quarantine (
                    issue_reason, location_id, sensing_datetime_utc,
                    source_payload_fingerprint, original_payload, live_run_id
                )
                SELECT 'existing_record_conflict', staged.sensor_id,
                    staged.sensing_datetime_utc,
                    staged.source_payload_fingerprint, staged.original_payload, %s
                FROM {} AS staged
                JOIN sensors AS sensor USING (sensor_id)
                JOIN pedestrian_counts_minutely_live AS existing
                  ON existing.sensor_id = staged.sensor_id
                 AND existing.sensing_datetime_utc = staged.sensing_datetime_utc
                WHERE existing.source_payload_fingerprint
                    IS DISTINCT FROM staged.source_payload_fingerprint
                """
            ).format(sql.Identifier(stage)),
            (run_id,),
        ).rowcount
        quarantined += max(int(conflicts), 0)
        unchanged = int(
            self.connection.execute(
                sql.SQL(
                    """
                    SELECT count(*)
                    FROM {} AS staged
                    JOIN pedestrian_counts_minutely_live AS existing
                      ON existing.sensor_id = staged.sensor_id
                     AND existing.sensing_datetime_utc = staged.sensing_datetime_utc
                    WHERE existing.source_payload_fingerprint
                        = staged.source_payload_fingerprint
                    """
                ).format(sql.Identifier(stage))
            ).fetchone()[0]
        )
        loaded = self.connection.execute(
            sql.SQL(
                """
                INSERT INTO pedestrian_counts_minutely_live (
                    sensor_id, sensing_datetime_utc, sensing_date_local,
                    sensing_time_local, iso_weekday, local_hour,
                    direction_1_count, direction_2_count, pedestrian_count,
                    source_dataset_id, source_payload_fingerprint, live_run_id
                )
                SELECT staged.sensor_id, staged.sensing_datetime_utc,
                    staged.sensing_date_local, staged.sensing_time_local,
                    staged.iso_weekday, staged.local_hour,
                    staged.direction_1_count, staged.direction_2_count,
                    staged.pedestrian_count, staged.source_dataset_id,
                    staged.source_payload_fingerprint, %s
                FROM {} AS staged
                JOIN sensors AS sensor USING (sensor_id)
                LEFT JOIN pedestrian_counts_minutely_live AS existing
                  ON existing.sensor_id = staged.sensor_id
                 AND existing.sensing_datetime_utc = staged.sensing_datetime_utc
                WHERE existing.sensor_id IS NULL
                ORDER BY staged.sensing_datetime_utc, staged.sensor_id
                ON CONFLICT (sensor_id, sensing_datetime_utc) DO NOTHING
                """
            ).format(sql.Identifier(stage)),
            (run_id,),
        ).rowcount
        loaded = max(int(loaded), 0)
        status = "succeeded_with_warnings" if quarantined else "succeeded"
        self.connection.execute(
            """
            UPDATE live_ingestion_runs
            SET status = %s, completed_at = CURRENT_TIMESTAMP,
                max_source_timestamp_utc = %s, records_fetched = %s,
                records_loaded = %s, records_unchanged = %s,
                records_quarantined = %s, error_message = NULL
            WHERE run_id = %s
            """,
            (
                status,
                batch.max_source_timestamp_utc,
                batch.fetched_count,
                loaded,
                unchanged,
                quarantined,
                run_id,
            ),
        )
        return loaded, unchanged, quarantined, status


def run_live_ingestion(
    config: LiveIngestionConfig | None = None,
    *,
    connection: Connection[Any] | None = None,
    api_client: LiveApiClient | None = None,
    source_records: Sequence[Mapping[str, object]] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    connect: Callable[..., Connection[Any]] = psycopg.connect,
) -> LiveIngestionResult:
    """Fetch, validate, quarantine, and load one bounded incremental live window."""

    settings = config or LiveIngestionConfig()
    started = time.perf_counter()
    owned = connection is None
    if connection is None:
        database_url = settings.database_url or _environment_database_url(os.environ)
        try:
            connection = connect(database_url, autocommit=True)
        except Exception as error:
            safe = _safe_error(error, settings.error_message_max_length)
            raise LiveIngestionError(f"database connection failed: {safe}") from error
    database = _LiveDatabase(connection, settings)
    run_id: str | None = None
    client = api_client or LiveApiClient(settings)
    try:
        database.validate_schema()
        start, end = determine_live_window(now(), database.watermark(), settings)
        run_id = database.start_run(start, end)
        try:
            payloads = (
                tuple(dict(item) for item in source_records)
                if source_records is not None
                else client.fetch_window(start, end)
            )
            batch = prepare_live_records(payloads, settings.contract)
            try:
                with connection.transaction():
                    loaded, unchanged, quarantined, status = database.load_batch(
                        run_id, batch
                    )
                    if settings.dry_run:
                        raise _DryRunRollback()
            except _DryRunRollback:
                database.delete_run(run_id)
                return LiveIngestionResult(
                    run_id=None,
                    status="dry_run",
                    window_start_utc=start,
                    window_end_utc=end,
                    max_source_timestamp_utc=batch.max_source_timestamp_utc,
                    records_fetched=batch.fetched_count,
                    records_loaded=loaded,
                    records_unchanged=unchanged,
                    records_quarantined=quarantined,
                    exact_duplicates_collapsed=batch.exact_duplicates_collapsed,
                    request_count=client.request_count,
                    partition_count=client.partition_count,
                    elapsed_seconds=time.perf_counter() - started,
                    dry_run=True,
                )
            warnings = (
                (f"{quarantined} source record(s) quarantined",)
                if quarantined
                else ()
            )
            return LiveIngestionResult(
                run_id=run_id,
                status=status,
                window_start_utc=start,
                window_end_utc=end,
                max_source_timestamp_utc=batch.max_source_timestamp_utc,
                records_fetched=batch.fetched_count,
                records_loaded=loaded,
                records_unchanged=unchanged,
                records_quarantined=quarantined,
                exact_duplicates_collapsed=batch.exact_duplicates_collapsed,
                request_count=client.request_count,
                partition_count=client.partition_count,
                elapsed_seconds=time.perf_counter() - started,
                warnings=warnings,
            )
        except Exception as error:
            database.fail_run(run_id, error)
            if isinstance(error, LiveIngestionError):
                raise
            safe = _safe_error(error, settings.error_message_max_length)
            raise LiveIngestionError(f"live ingestion failed: {safe}") from error
    finally:
        if owned:
            connection.close()


__all__ = [
    "HttpResponse",
    "LiveApiClient",
    "LiveIngestionConfig",
    "LiveIngestionError",
    "LiveIngestionResult",
    "LiveSourceContract",
    "canonical_payload_fingerprint",
    "determine_live_window",
    "prepare_live_records",
    "run_live_ingestion",
]
