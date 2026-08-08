# CityFlow PostgreSQL and PostGIS schema

`database/migrations/` is the authoritative CityFlow database schema. The
migration runner records the version, filename, SHA-256 checksum and applied
timestamp in `schema_migrations`. An applied migration must never be edited;
add a new numbered migration instead.

`schema.sql` is only a small `psql` convenience wrapper that includes the same
versioned files for a completely clean, manually managed database. Existing
databases should always use `database/migrate.py` so migration history remains
tracked.

## Architecture

The schema separates observability, source-aligned analytical facts, spatial
routing data and application-facing views:

- `pipeline_runs` records pipeline status, source fingerprints, row counts and
  bounded error/metadata details.
- `sensors`, `sensor_directions` and `landmarks` store transformed dimensions.
- `pedestrian_counts_hourly` stores historical hourly facts in Melbourne local
  `timestamp without time zone`; it is not UTC.
- `crowd_baselines` stores one descriptive baseline per sensor, ISO weekday and
  hour. The large hourly table does not repeat baseline statistics.
- `routing_nodes` and `routing_edges` preserve stable UUID business IDs while
  adding `BIGINT IDENTITY` IDs required by pgRouting.
- `sensor_network_map` and `landmark_network_map` connect application locations
  to routing-node integer IDs and preserve snap quality flags.
- `routing_edges_pgr` exposes the primary component in pgRouting form.
- `hourly_crowd_features` reproduces the Python baseline feature formulas at
  query time.
- `live_ingestion_runs`, `pedestrian_counts_minutely_live`, and the live
  quarantine table provide incremental observability, strict sensor identity,
  and auditable source conflicts.
- `latest_sensor_crowd_levels` returns one frontend-oriented row per canonical
  sensor for the latest completed 15-minute window.

PostGIS geometries remain WGS84 (`EPSG:4326`). Metric lengths and snapping are
calculated by the Python Spatial Layer in Melbourne's projected `EPSG:32755`
CRS before values are stored.

## Environment setup

Do not commit a populated `.env` file or put credentials into migration SQL.
The runner checks the following configuration in order:

1. `DATABASE_URL`;
2. `CITYFLOW_DATABASE_URL`;
3. standard `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` variables;
4. matching `POSTGRES_*` variables.

Example variable names are maintained in `data_pipeline/.env.example`. Set real
values only in your local shell, secret manager or untracked `.env` file.

## Applying migrations

From the repository root with the data-pipeline virtual environment active:

```bash
export DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/DATABASE'
python database/migrate.py
```

For local Docker development, use the database settings from the separately
managed Compose environment and connect through its loopback PostgreSQL port.
Re-running the command is safe: applied checksums are verified and unchanged
migrations are skipped.

For a brand-new manually managed database, `psql` can apply the convenience
wrapper:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f database/schema.sql
```

The wrapper does not create migration tracking records, so the Python runner is
preferred for team and deployed environments.

## Clean-database setup

Create an isolated empty database using an authorised PostgreSQL administrator,
grant the application/migration role ownership or suitable DDL privileges, and
run `python database/migrate.py`. The PostgreSQL installation must provide both
PostGIS and pgRouting. Migration `001` fails immediately and clearly if either
extension cannot be created.

Never delete or reset the shared development database to test migrations. Use a
temporary database with a unique name, then remove only that database after the
test finishes.

## pgRouting identifiers and duplicate edges

The Spatial Layer emits stable UUIDs. They are stored as `node_uuid` and
`edge_uuid` and remain the data-pipeline/business identifiers. PostgreSQL adds
identity `BIGINT` values:

- `routing_nodes.id` is the pgRouting vertex ID;
- `routing_edges.id`, `source` and `target` are pgRouting edge/vertex IDs;
- `source` and `target` reference `routing_nodes.id`.

The base edge table preserves legitimate parallel edges and exact duplicate
source records. `routing_edges_pgr` is a route-computation view: it normalises
each LineString so forward and reversed copies share a duplicate group, ranks
that group by `edge_uuid` then integer `id`, and selects rank one. Different
geometries connecting the same nodes remain available as parallel edges.

The current source has no direction restriction, so walking is assumed
bidirectional. Initial `cost` and `reverse_cost` both equal positive metric edge
length. Sensory-weighted costs belong to a later integration stage.

## FastAPI connection guidance for Elyas

FastAPI should receive the same secret-managed `DATABASE_URL`, create a bounded
connection pool during application startup, and query the views/tables through
parameterised SQL. Route endpoints should use `routing_edges_pgr`; crowd display
queries should use `hourly_crowd_features` or narrower reviewed queries.

Do not run migrations on every API request and do not embed database passwords
in backend source. Apply migrations as an explicit local/deployment step before
starting a version of the API that requires the new schema.

## Later loader responsibilities

`load.py` will be implemented separately. It will:

1. create a `pipeline_runs` row;
2. load transformed dimensions and facts using business-key upserts;
3. translate stable spatial UUID references to routing integer IDs;
4. populate sensor and landmark mapping tables;
5. finish the pipeline run with row counts or a bounded error; and
6. never recompute the schema or silently modify migration history.

No migration in this directory loads application or real raw data.

## Loading processed CityFlow data

Run `python database/migrate.py` against the intended database before calling
the loading layer. The loader verifies migrations 001-003 plus the `postgis`
and `pgrouting` extensions and refuses to continue when they are missing.

Use an isolated database for development and tests. The default load is a
non-destructive, idempotent upsert; it does not replace a snapshot or delete
rows absent from the incoming dataset. Routing UUIDs are stable pipeline keys,
while `routing_nodes.id` and `routing_edges.id` remain database-generated
BIGINT values used by pgRouting.

After loading, Elyas can expose read-only queries through FastAPI using views
such as `hourly_crowd_features` and `routing_edges_pgr`. Keep database access in
the backend service, use a least-privilege application role, parameterise all
filters, paginate large responses, and never send credentials to the frontend.

## Live ingestion schema

Migration 004 adds the live-run log, UTC minutely curated facts, auditable
quarantine records, and `latest_sensor_crowd_levels`. The live loader does not
apply migrations automatically. Apply `python database/migrate.py` explicitly
before running `python -m cityflow_pipeline.live_runner`.

The curated natural key is `(sensor_id, sensing_datetime_utc)`. Unknown sensor
IDs retain their original JSON payload in quarantine rather than weakening the
foreign key. Conflicting source variants and different payloads for existing
keys are also quarantined and never overwrite curated history. Missing minutes
are absent, not zero.

The view uses the latest completed 15-minute wall-clock window, reports the
observed sum and a clearly named `hourly_equivalent_estimate`, and joins the
matching Melbourne-local historical baseline. The historical `typical` band is
named `medium` in this frontend-facing view. Older live history with no current
window record is `stale`; a sensor with no live history is `no_data`.
