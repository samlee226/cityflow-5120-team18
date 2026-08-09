"""Unit and contract tests for bounded live pedestrian ingestion."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any

import pytest

import cityflow_pipeline.live as live
import cityflow_pipeline.live_runner as live_runner
from cityflow_pipeline.live import (
    HttpResponse,
    LiveApiClient,
    LiveIngestionConfig,
    LiveIngestionError,
    LiveIngestionResult,
    canonical_payload_fingerprint,
    determine_live_window,
    prepare_live_records,
    run_live_ingestion,
)


def payload(
    *,
    sensor: int = 1,
    timestamp: str = "2026-08-08T12:00:00+00:00",
    direction_1: object = 2,
    direction_2: object = 3,
    total: object = 5,
    **extra: object,
) -> dict[str, object]:
    return {
        "location_id": sensor,
        "sensing_datetime": timestamp,
        "direction_1": direction_1,
        "direction_2": direction_2,
        "total_of_directions": total,
        **extra,
    }


def response(total: int, rows: list[dict[str, object]], status: int = 200, **headers: str) -> HttpResponse:
    return HttpResponse(
        status,
        json.dumps({"total_count": total, "results": rows}).encode(),
        headers,
    )


class FakeTransport:
    def __init__(self, responses: list[HttpResponse | BaseException]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object], float, float]] = []

    def get(
        self,
        url: str,
        params: dict[str, object],
        *,
        connect_timeout: float,
        read_timeout: float,
    ) -> HttpResponse:
        self.calls.append((url, dict(params), connect_timeout, read_timeout))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


START = datetime(2026, 8, 8, 10, tzinfo=UTC)
END = datetime(2026, 8, 8, 12, tzinfo=UTC)


def test_successful_bounded_pagination_and_timeouts() -> None:
    transport = FakeTransport(
        [
            response(2, [payload(timestamp="2026-08-08T10:01:00+00:00")]),
            response(2, [payload(timestamp="2026-08-08T10:02:00+00:00")]),
        ]
    )
    config = LiveIngestionConfig(page_limit=1, connect_timeout_seconds=3, read_timeout_seconds=7)
    client = LiveApiClient(config, transport=transport)

    rows = client.fetch_window(START, END)

    assert len(rows) == 2
    assert client.request_count == 2
    assert client.partition_count == 1
    assert [call[1]["offset"] for call in transport.calls] == [0, 1]
    assert all(call[2:] == (3, 7) for call in transport.calls)
    assert all("sensing_datetime" in str(call[1]["where"]) for call in transport.calls)
    assert not transport.responses


def test_latest_source_timestamp_uses_one_record_query() -> None:
    transport = FakeTransport(
        [response(102_410, [{"sensing_datetime": END.isoformat()}])]
    )
    client = LiveApiClient(LiveIngestionConfig(), transport=transport)

    assert client.latest_source_timestamp() == END
    assert client.request_count == 1
    assert transport.calls[0][1] == {
        "limit": 1,
        "offset": 0,
        "select": "sensing_datetime",
        "order_by": "sensing_datetime DESC",
    }


def test_timestamp_partition_splitting_is_non_overlapping() -> None:
    left = payload(timestamp="2026-08-08T10:30:00+00:00")
    right = payload(timestamp="2026-08-08T11:30:00+00:00")
    transport = FakeTransport(
        [response(10_001, []), response(1, [left]), response(1, [right])]
    )
    client = LiveApiClient(LiveIngestionConfig(), transport=transport)

    assert client.fetch_window(START, END) == (left, right)
    assert client.partition_count == 2
    parent, first, second = [str(item[1]["where"]) for item in transport.calls]
    assert "10:00:00+00:00" in parent and "12:00:00+00:00" in parent
    assert "10:00:00+00:00" in first and "11:00:00+00:00" in first
    assert "11:00:00+00:00" in second and "12:00:00+00:00" in second


def test_request_budget_is_enforced() -> None:
    transport = FakeTransport([response(2, [payload(timestamp="2026-08-08T10:01:00+00:00")])])
    client = LiveApiClient(
        LiveIngestionConfig(page_limit=1, request_budget=1), transport=transport
    )
    with pytest.raises(LiveIngestionError, match="budget exhausted"):
        client.fetch_window(START, END)


def test_changing_total_count_fails_partition() -> None:
    transport = FakeTransport(
        [
            response(2, [payload(timestamp="2026-08-08T10:01:00+00:00")]),
            response(3, [payload(timestamp="2026-08-08T10:02:00+00:00")]),
        ]
    )
    client = LiveApiClient(LiveIngestionConfig(page_limit=1), transport=transport)
    with pytest.raises(LiveIngestionError, match="total_count changed"):
        client.fetch_window(START, END)


def test_retryable_response_respects_retry_after() -> None:
    sleeps: list[float] = []
    transport = FakeTransport(
        [HttpResponse(429, b"{}", {"retry-after": "2"}), response(0, [])]
    )
    client = LiveApiClient(
        LiveIngestionConfig(backoff_max_seconds=5),
        transport=transport,
        sleep=sleeps.append,
    )
    assert client.fetch_window(START, END) == ()
    assert sleeps == [2.0]
    assert client.request_count == 2


def test_network_failure_retries_with_bounded_backoff() -> None:
    sleeps: list[float] = []
    transport = FakeTransport([TimeoutError("temporary"), response(0, [])])
    client = LiveApiClient(
        LiveIngestionConfig(backoff_base_seconds=1, jitter_ratio=0),
        transport=transport,
        sleep=sleeps.append,
    )
    assert client.fetch_window(START, END) == ()
    assert sleeps == [1]


def test_non_retryable_response_is_not_retried() -> None:
    transport = FakeTransport([HttpResponse(400, b"{}")])
    client = LiveApiClient(LiveIngestionConfig(), transport=transport)
    with pytest.raises(LiveIngestionError, match="non-retryable HTTP 400"):
        client.fetch_window(START, END)
    assert len(transport.calls) == 1


def test_invalid_json_and_response_schema_are_not_retried() -> None:
    invalid = LiveApiClient(
        LiveIngestionConfig(), transport=FakeTransport([HttpResponse(200, b"not-json")])
    )
    with pytest.raises(LiveIngestionError, match="invalid JSON"):
        invalid.fetch_window(START, END)
    malformed = LiveApiClient(
        LiveIngestionConfig(),
        transport=FakeTransport([HttpResponse(200, json.dumps({"results": []}).encode())]),
    )
    with pytest.raises(LiveIngestionError, match="total_count"):
        malformed.fetch_window(START, END)


def test_schema_drift_missing_and_unexpected_fields() -> None:
    missing = payload()
    missing.pop("direction_1")
    with pytest.raises(LiveIngestionError, match="missing required"):
        prepare_live_records([missing])
    with pytest.raises(LiveIngestionError, match="unexpected fields"):
        prepare_live_records([payload(new_field="drift")])
    assert len(prepare_live_records([payload(sensing_date="2026-08-08")]).records) == 1


@pytest.mark.parametrize(
    "timestamp, message",
    [("not-a-date", "malformed"), ("2026-08-08T12:00:00", "timezone-aware")],
)
def test_malformed_or_naive_timestamp(timestamp: str, message: str) -> None:
    with pytest.raises(LiveIngestionError, match=message):
        prepare_live_records([payload(timestamp=timestamp)])


@pytest.mark.parametrize(
    "updates, message",
    [
        ({"direction_1": -1, "total": 2}, "non-negative"),
        ({"direction_1": 1.5, "total": 4.5}, "integer"),
        ({"sensor": 0}, "positive"),
    ],
)
def test_invalid_integer_counts_or_sensor(updates: dict[str, object], message: str) -> None:
    with pytest.raises(LiveIngestionError, match=message):
        prepare_live_records([payload(**updates)])


def test_direction_total_mismatch() -> None:
    with pytest.raises(LiveIngestionError, match="must equal"):
        prepare_live_records([payload(total=99)])


def test_exact_duplicates_collapse_without_input_mutation() -> None:
    source = payload()
    before = dict(source)
    batch = prepare_live_records([source, dict(source)])
    assert len(batch.records) == 1
    assert batch.exact_duplicates_collapsed == 1
    assert not batch.quarantine
    assert source == before


def test_conflicting_duplicates_quarantine_all_variants() -> None:
    batch = prepare_live_records([payload(total=5), payload(direction_1=4, direction_2=3, total=7)])
    assert not batch.records
    assert len(batch.quarantine) == 2
    assert {item.reason for item in batch.quarantine} == {"conflicting_duplicate"}
    assert len({item.fingerprint for item in batch.quarantine}) == 2


def test_fingerprint_is_canonical_and_deterministic() -> None:
    first = payload(sensing_time="12:00")
    second = {key: first[key] for key in reversed(first)}
    assert canonical_payload_fingerprint(first) == canonical_payload_fingerprint(second)
    assert canonical_payload_fingerprint(first) != canonical_payload_fingerprint(payload())


def test_missing_readings_are_not_created_or_zero_filled() -> None:
    rows = [payload(timestamp="2026-08-08T12:00:00+00:00"), payload(timestamp="2026-08-08T12:02:00+00:00")]
    batch = prepare_live_records(rows)
    assert len(batch.records) == 2
    assert [item.sensing_datetime_utc.minute for item in batch.records] == [0, 2]
    assert all(item.pedestrian_count == 5 for item in batch.records)


def test_utc_to_melbourne_conversion_handles_standard_time() -> None:
    record = prepare_live_records([payload(timestamp="2026-08-08T23:30:00+00:00")]).records[0]
    assert str(record.sensing_date_local) == "2026-08-09"
    assert str(record.sensing_time_local).startswith("09:30")
    assert record.local_hour == 9
    assert record.iso_weekday == 7


def test_bounded_bootstrap_and_watermark_overlap_windows() -> None:
    config = LiveIngestionConfig(bootstrap_minutes=90, overlap_minutes=30)
    assert determine_live_window(END, None, config) == (
        END - timedelta(minutes=90),
        END + timedelta(seconds=1),
    )
    watermark = END - timedelta(minutes=20)
    assert determine_live_window(END, watermark, config) == (
        END - timedelta(minutes=50),
        END + timedelta(seconds=1),
    )


def test_bootstrap_cannot_exceed_ninety_minutes() -> None:
    with pytest.raises(ValueError, match="90-minute safety bound"):
        LiveIngestionConfig(bootstrap_minutes=91)


def test_result_is_json_serialisable() -> None:
    result = LiveIngestionResult(
        "run", "succeeded", START, END, END - timedelta(minutes=1),
        1, 1, 0, 0, 0, 2, 1, 0.1,
    )
    decoded = json.loads(json.dumps(result.to_dict(), allow_nan=False))
    assert decoded["status"] == "succeeded"
    assert decoded["max_source_timestamp_utc"].endswith("+00:00")


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def transaction(self) -> Any:
        return nullcontext()

    def close(self) -> None:
        self.closed = True


class FakeDatabase:
    instances: list["FakeDatabase"] = []
    watermark_value: datetime | None = None
    load_result = (1, 0, 0, "succeeded")
    load_error: BaseException | None = None

    def __init__(self, connection: object, config: LiveIngestionConfig) -> None:
        self.connection = connection
        self.config = config
        self.failed: BaseException | None = None
        self.deleted = False
        self.loaded_batch: object = None
        FakeDatabase.instances.append(self)

    def validate_schema(self) -> None: pass
    def watermark(self) -> datetime | None: return self.watermark_value
    def start_run(self, start: datetime, end: datetime) -> str: return "run-id"
    def fail_run(self, run_id: str, error: BaseException) -> None: self.failed = error
    def delete_run(self, run_id: str) -> None: self.deleted = True

    def load_batch(self, run_id: str, batch: object) -> tuple[int, int, int, str]:
        self.loaded_batch = batch
        if self.load_error:
            raise self.load_error
        return self.load_result


@pytest.fixture
def fake_database(monkeypatch: pytest.MonkeyPatch) -> type[FakeDatabase]:
    FakeDatabase.instances.clear()
    FakeDatabase.watermark_value = None
    FakeDatabase.load_result = (1, 0, 0, "succeeded")
    FakeDatabase.load_error = None
    monkeypatch.setattr(live, "_LiveDatabase", FakeDatabase)
    return FakeDatabase


def test_source_records_bypass_http_and_caller_connection_stays_open(fake_database: type[FakeDatabase]) -> None:
    connection = FakeConnection()
    result = run_live_ingestion(
        LiveIngestionConfig(),
        connection=connection,  # type: ignore[arg-type]
        source_records=[payload()],
        now=lambda: END,
    )
    assert result.records_loaded == 1
    assert result.request_count == 0
    assert not connection.closed


def test_delayed_api_anchors_bootstrap_to_latest_source_timestamp(
    fake_database: type[FakeDatabase],
) -> None:
    latest = END - timedelta(hours=3)
    row = payload(timestamp=(latest - timedelta(minutes=1)).isoformat())
    transport = FakeTransport(
        [
            response(102_410, [{"sensing_datetime": latest.isoformat()}]),
            response(1, [row]),
        ]
    )
    client = LiveApiClient(LiveIngestionConfig(), transport=transport)

    result = run_live_ingestion(
        LiveIngestionConfig(),
        connection=FakeConnection(),  # type: ignore[arg-type]
        api_client=client,
        now=lambda: END,
    )

    assert result.window_start_utc == latest - timedelta(minutes=90)
    assert result.window_end_utc == latest + timedelta(seconds=1)
    assert result.request_count == 2
    where = str(transport.calls[1][1]["where"])
    assert "sensing_datetime" in where
    assert (latest - timedelta(minutes=90)).isoformat(timespec="seconds") in where


def test_upgrade_from_legacy_migration004_watermark_caps_catch_up_window(
    fake_database: type[FakeDatabase],
) -> None:
    FakeDatabase.watermark_value = END - timedelta(days=1)
    row = payload(timestamp=(END - timedelta(minutes=1)).isoformat())
    transport = FakeTransport(
        [
            response(102_410, [{"sensing_datetime": END.isoformat()}]),
            response(1, [row]),
        ]
    )
    client = LiveApiClient(LiveIngestionConfig(), transport=transport)

    result = run_live_ingestion(
        client.config,
        connection=FakeConnection(),  # type: ignore[arg-type]
        api_client=client,
    )

    assert result.window_start_utc == END - timedelta(minutes=90)
    assert result.window_end_utc == END + timedelta(seconds=1)
    where = str(transport.calls[1][1]["where"])
    assert (END - timedelta(minutes=90)).isoformat(timespec="seconds") in where
    assert (END - timedelta(days=1, minutes=30)).isoformat(timespec="seconds") not in where
    assert result.request_count == 2


def test_discovery_and_pagination_share_request_budget(
    fake_database: type[FakeDatabase],
) -> None:
    first = payload(timestamp=(END - timedelta(minutes=2)).isoformat())
    transport = FakeTransport(
        [
            response(102_410, [{"sensing_datetime": END.isoformat()}]),
            response(2, [first]),
        ]
    )
    client = LiveApiClient(
        LiveIngestionConfig(page_limit=1, request_budget=2),
        transport=transport,
    )

    with pytest.raises(LiveIngestionError, match="budget exhausted"):
        run_live_ingestion(
            client.config,
            connection=FakeConnection(),  # type: ignore[arg-type]
            api_client=client,
        )
    assert client.request_count == 2


def test_duplicate_rerun_is_reported_as_unchanged(
    fake_database: type[FakeDatabase],
) -> None:
    connection = FakeConnection()
    first = run_live_ingestion(
        LiveIngestionConfig(),
        connection=connection,  # type: ignore[arg-type]
        source_records=[payload()],
        now=lambda: END,
    )
    FakeDatabase.load_result = (0, 1, 0, "succeeded")
    second = run_live_ingestion(
        LiveIngestionConfig(),
        connection=connection,  # type: ignore[arg-type]
        source_records=[payload()],
        now=lambda: END,
    )

    assert first.records_loaded == 1 and first.records_unchanged == 0
    assert second.records_loaded == 0 and second.records_unchanged == 1
    assert second.request_count == 0


def test_loader_owned_connection_closes(fake_database: type[FakeDatabase]) -> None:
    connection = FakeConnection()
    run_live_ingestion(
        LiveIngestionConfig(database_url="postgresql://example.invalid/db"),
        source_records=[payload()],
        now=lambda: END,
        connect=lambda *args, **kwargs: connection,  # type: ignore[arg-type]
    )
    assert connection.closed


def test_dry_run_rolls_back_and_removes_run(fake_database: type[FakeDatabase]) -> None:
    connection = FakeConnection()
    result = run_live_ingestion(
        LiveIngestionConfig(dry_run=True),
        connection=connection,  # type: ignore[arg-type]
        source_records=[payload()],
        now=lambda: END,
    )
    assert result.status == "dry_run"
    assert result.run_id is None
    assert FakeDatabase.instances[-1].deleted


def test_failed_run_is_recorded_separately(fake_database: type[FakeDatabase]) -> None:
    FakeDatabase.load_error = RuntimeError("synthetic load error")
    connection = FakeConnection()
    with pytest.raises(LiveIngestionError, match="live ingestion failed"):
        run_live_ingestion(
            LiveIngestionConfig(),
            connection=connection,  # type: ignore[arg-type]
            source_records=[payload()],
            now=lambda: END,
        )
    assert isinstance(FakeDatabase.instances[-1].failed, RuntimeError)


def test_connection_errors_redact_credentials(fake_database: type[FakeDatabase]) -> None:
    secret = "do-not-leak"
    with pytest.raises(LiveIngestionError) as captured:
        run_live_ingestion(
            LiveIngestionConfig(database_url=f"postgresql://user:{secret}@db/cityflow"),
            connect=lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError(f"failed password={secret} postgresql://user:{secret}@db/cityflow")
            ),
        )
    assert secret not in str(captured.value)
    assert "redacted" in str(captured.value)


def test_cli_parsing_and_json_output(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    args = live_runner._build_parser().parse_args(
        [
            "--dry-run", "--json", "--request-budget", "12",
            "--overlap-minutes", "15", "--bootstrap-minutes", "60",
        ]
    )
    assert args.dry_run and args.json_output and args.request_budget == 12
    assert args.bootstrap_minutes == 60
    result = LiveIngestionResult(None, "dry_run", START, END, None, 0, 0, 0, 0, 0, 0, 0, 0.01, True)
    monkeypatch.setattr(live_runner, "run_live_ingestion", lambda config: result)
    assert live_runner.main(["--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"


def test_migration_defines_quarantine_conflicts_strict_fk_and_view() -> None:
    repository = Path(__file__).resolve().parents[2]
    sql_text = (repository / "database/migrations/004_live_pedestrian_ingestion.sql").read_text().lower()
    assert "references sensors(sensor_id)" in sql_text
    for reason in ("conflicting_duplicate", "existing_record_conflict", "unknown_sensor"):
        assert reason in sql_text
    assert "create view latest_sensor_crowd_levels" in sql_text
    assert "then 'stale'" in sql_text and "then 'no_data'" in sql_text
    assert "else 'medium'" in sql_text
    assert "observed_15m_count * 4" in sql_text


def test_forward_migration_anchors_view_to_latest_source_and_reports_age() -> None:
    repository = Path(__file__).resolve().parents[2]
    sql_text = (
        repository / "database/migrations/006_source_relative_live_view.sql"
    ).read_text().lower()

    assert "create or replace view latest_sensor_crowd_levels" in sql_text
    assert "max(live.sensing_datetime_utc)" in sql_text
    assert "latest_source_timestamp_utc" in sql_text
    assert "data_age" in sql_text
    for status in ("fresh", "delayed", "stale"):
        assert f"'{status}'" in sql_text


def test_historical_pipeline_contract_remains_unchanged() -> None:
    from cityflow_pipeline.runner import EXCLUDED_HISTORICAL_SOURCES, PIPELINE_STAGE_ORDER

    assert EXCLUDED_HISTORICAL_SOURCES == ("pedestrian_counts_minutely.csv",)
    assert PIPELINE_STAGE_ORDER[-1] == "database_loading"
