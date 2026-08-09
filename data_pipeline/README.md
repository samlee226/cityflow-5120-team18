# CityFlow Data Pipeline

This directory contains the FIT5120 CityFlow historical and bounded live pedestrian data pipelines. Both workflows preserve raw inputs, validate source contracts, and load PostgreSQL/PostGIS through explicit migrations.

## Structure

- `src/cityflow_pipeline/`: pipeline package and stage modules
- `tests/`: automated tests
- `notebooks/`: exploratory notebooks
- `data/raw/`: local source data, ignored by Git
- `data/interim/`: local intermediate data, ignored by Git
- `data/processed/`: local generated outputs, ignored by Git
- `data/sample/`: small, non-sensitive sample data that may be committed

## Local setup

From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
pytest
```

Keep real data and secrets out of version control.

## PostgreSQL/PostGIS loading

Apply the versioned migrations before loading data, then provide either an
explicit Psycopg connection, `DATABASE_URL`, `CITYFLOW_DATABASE_URL`, or the
standard `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, and `PGPASSWORD`
variables. Never commit a populated `.env` file.

```python
from cityflow_pipeline.load import DatabaseLoaderConfig, load_historical_dataset

result = load_historical_dataset(
    config=DatabaseLoaderConfig(dry_run=True),
    sensors=transformed.sensors,
    sensor_directions=transformed.sensor_directions,
    landmarks=transformed.landmarks,
    hourly_chunk_factory=lambda: transformed.iter_hourly_chunks(),
    crowd_baselines=baselines,
    spatial_network=spatial.network,
    sensor_mapping=spatial.sensor_mapping,
    landmark_mapping=spatial.landmark_mapping,
    validation_report=validation,
)
print(result.to_dict())
```

The foreign-key-safe order is pipeline run, sensors, sensor directions,
landmarks, hourly chunks, crowd baselines, routing nodes, routing edges, sensor
mapping, and landmark mapping. Each complete business load is one transaction.
COPY-backed temporary tables feed deterministic upserts, so rerunning the same
dataset does not create duplicates and existing pgRouting integer IDs remain
stable. A failure rolls back all business rows and records a safe failed-run
summary separately.

`dry_run=True` executes validation, staging, relationships, and upsert SQL, then
rolls the business transaction back and removes its temporary pipeline-run
record. It intentionally leaves no misleading completed run.

For local development, create an isolated database, apply migrations, run a dry
run, perform the real load, and check table/view counts before allowing an API
to use it. The loader does not create databases, apply migrations, delete stale
snapshot rows, calculate spatial distances, or ingest the minutely live feed.

## Historical V1 end-to-end runner

The runner connects the completed Week 3 layers without writing intermediate
files:

```text
Raw CSV -> extract -> clean -> validate -> transform
        -> crowd baseline/features -> spatial network -> PostgreSQL/PostGIS
```

Prerequisites:

- create and activate the project virtual environment;
- make the four historical CSV files available under a local ignored raw-data
  directory;
- configure `DATABASE_URL`, `CITYFLOW_DATABASE_URL`, or the supported
  `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, and `PGPASSWORD` variables;
- apply all pending migrations to the target database with
  `python database/migrate.py` before running the pipeline; and
- confirm PostGIS and pgRouting are installed in that database.

Run the historical pipeline from the repository root:

```bash
python -m cityflow_pipeline.runner \
  --raw-data-dir data_pipeline/data/raw
```

Exercise the full computation and loading path without retaining database rows:

```bash
python -m cityflow_pipeline.runner \
  --raw-data-dir data_pipeline/data/raw \
  --dry-run
```

Emit structured JSON for local logs or later EC2 automation:

```bash
python -m cityflow_pipeline.runner \
  --raw-data-dir data_pipeline/data/raw \
  --json
```

The source schema still requires the `Installation_Date` column, but an
individual sensor may omit this optional metadata. Blank, whitespace-only and
CSV/pandas null values become a missing date and are loaded as SQL `NULL`;
non-empty malformed dates fail cleaning. No date is inferred or filled.

The result reports stage timings, validation and spatial summaries, hourly-pass
chunk metrics, baseline metrics, per-table load counts, warnings, and the
persisted pipeline-run ID. Hourly data is re-read through fresh iterators for
validation, baseline construction, and feature-enriched loading; the complete
hourly dataset is never concatenated into one DataFrame.

This command handles historical V1 data only. It deliberately excludes
`pedestrian_counts_minutely.csv`, does not call the live API, does not apply or
change migrations, and does not create interim or processed data files. Live
ingestion, AWS scheduling, and EC2 deployment remain later infrastructure work.
Raw source data remains local and ignored by Git.

## Live pedestrian ingestion

The live workflow reads a bounded UTC window from the official City of
Melbourne dataset:

`https://data.melbourne.vic.gov.au/api/explore/v2.1/catalog/datasets/pedestrian-counting-system-past-hour-counts-per-minute/records`

Apply all pending migrations, including migration 006, before a live run:

```bash
python database/migrate.py
```

Then run locally from the repository root:

```bash
python -m cityflow_pipeline.live_runner
python -m cityflow_pipeline.live_runner --dry-run
python -m cityflow_pipeline.live_runner --json
```

Each API run first requests the latest available `sensing_datetime`. The first
successful run uses a source-anchored bootstrap window of at most 90 minutes.
Later runs start at the last successful source watermark minus a 30-minute
overlap and end immediately after the latest source timestamp. The natural key
and payload fingerprint make that overlap idempotent. A legacy watermark older
than the source-anchored 90-minute safety window is capped at that window rather
than triggering an unbounded catch-up. Exact duplicates collapse.
Conflicting duplicates, payload conflicts with an existing curated row, and
unknown sensors are retained in `pedestrian_counts_minutely_quarantine` while
valid rows continue loading. Curated records are never silently overwritten.

Missing source minutes remain missing: the loader neither creates rows nor
fills counts with zero. `latest_sensor_crowd_levels` therefore exposes null
counts and explicit data-age status rather than misleading zero traffic.
Its hourly-equivalent value is explicitly an estimate based on the latest
completed 15-minute window relative to source availability. `data_age` compares
each sensor's latest reading with database time; status is `fresh` through 15
minutes, `delayed` through 60 minutes, and then `stale` (`no_data` means no live
history). Historical `typical` is presented as frontend-friendly `medium`.

This command is manual/local only. AWS or EC2 scheduling remains an IT and
infrastructure task. Route scoring and consumption of the view remain backend
integration tasks; they are outside this pipeline component.
