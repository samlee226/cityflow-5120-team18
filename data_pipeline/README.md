# CityFlow Data Pipeline

This directory contains the initial Data Science workspace for the FIT5120 CityFlow project. It currently provides project structure only; data-source integrations, database operations, analytics, and AI functionality will be added in later iterations.

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
