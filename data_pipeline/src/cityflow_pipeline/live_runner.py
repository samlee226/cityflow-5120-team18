"""Command-line runner for bounded CityFlow live pedestrian ingestion."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from cityflow_pipeline.live import (
    LiveIngestionConfig,
    LiveIngestionError,
    LiveIngestionResult,
    run_live_ingestion,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap-minutes", type=int, default=90)
    parser.add_argument("--overlap-minutes", type=int, default=30)
    parser.add_argument("--request-budget", type=int, default=250)
    parser.add_argument("--connect-timeout", type=float, default=5.0)
    parser.add_argument("--read-timeout", type=float, default=20.0)
    parser.add_argument(
        "--database-url",
        default=None,
        help="optional database URL; environment variables are safer for normal use",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="exercise extraction and database loading, then roll business writes back",
    )
    parser.add_argument(
        "--json", dest="json_output", action="store_true", help="emit one JSON object"
    )
    return parser


def _human_output(result: LiveIngestionResult) -> str:
    return "\n".join(
        (
            f"Live ingestion status: {result.status}",
            f"Run ID: {result.run_id or 'not persisted'}",
            f"Window: {result.window_start_utc.isoformat()} to "
            f"{result.window_end_utc.isoformat()}",
            f"Fetched: {result.records_fetched}",
            f"Loaded: {result.records_loaded}",
            f"Unchanged: {result.records_unchanged}",
            f"Quarantined: {result.records_quarantined}",
            f"Requests: {result.request_count}",
            f"Elapsed seconds: {result.elapsed_seconds:.3f}",
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run live ingestion and return a process-compatible exit code."""

    args = _build_parser().parse_args(argv)
    try:
        config = LiveIngestionConfig(
            database_url=args.database_url,
            bootstrap_minutes=args.bootstrap_minutes,
            overlap_minutes=args.overlap_minutes,
            request_budget=args.request_budget,
            connect_timeout_seconds=args.connect_timeout,
            read_timeout_seconds=args.read_timeout,
            dry_run=args.dry_run,
        )
        result = run_live_ingestion(config)
    except (LiveIngestionError, ValueError) as error:
        if args.json_output:
            print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True))
        else:
            print(f"Live ingestion failed: {error}")
        return 1
    print(
        json.dumps(result.to_dict(), sort_keys=True)
        if args.json_output
        else _human_output(result)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
