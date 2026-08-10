# CityFlow Backend

FastAPI service exposing sensory-aware wayfinding data for Melbourne CBD: current crowd conditions, historical trends, and pedestrian routing (plain shortest-path and crowd-aware).

## Setup

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your real DATABASE_URL
```

## Prerequisites

This API reads from a PostgreSQL/PostGIS/pgRouting database built and populated by the `database/` and `data_pipeline/` components of this repo. Before running the API for the first time:

```bash
python database/migrate.py                                  # apply schema
python -m cityflow_pipeline.edge_sensor_map --radius-m 150   # build the routing performance table (used in /api/routes/low-crowd)
```

## Running

```bash
uvicorn app.main:app --reload
```

Visit `http://localhost:8000/docs` for interactive Swagger docs of every endpoint below.

## Endpoints

| Endpoint | What it does |
|---|---|
| `GET /health` | Basic liveness check |
| `GET /api/crowd-conditions` | Latest *historical* hourly crowd reading per sensor |
| `GET /api/crowd-conditions/{sensor_id}/trend` | Hourly crowd trend for one sensor over a date range from latest entry of data for that sensor |
| `GET /api/live-crowd-conditions` | Current crowd conditions, live data with historical fallback |
| `GET /api/network/nearest-node` | Snaps a lat/lon to the nearest routing network node |
| `POST /api/routes` | Shortest walking route between two nodes |
| `POST /api/routes/low-crowd` | Same, but penalises routes near currently-crowded sensors |

## Architecture notes

- **Connection pooling**: one shared `asyncpg` pool, opened at startup and closed at shutdown (`app/core/db.py`). Never a new connection per request.
- **Crowd data fallback**: live data is used when `fresh`/`delayed`; falls back to the latest historical reading when `stale`/`no_data` (`app/core/crowd_sql.py`).
- **Low-crowd routing performance**: uses the precomputed `edge_sensor_map` table (see Prerequisites) instead of a live spatial join. Crowd data is resolved once per request into a temp table and reused, rather than recomputed twice.
- **Response caching**: `/api/routes/low-crowd` caches identical `(start_node, end_node)` requests in memory for 10 minutes.

## Known limitation(s)

- `/api/routes/low-crowd` is slower than `/api/routes` (~3s vs ~1-2s). This is expected, since it resolves live crowd data and returns extra per-step detail the plain endpoint doesn't compute.
