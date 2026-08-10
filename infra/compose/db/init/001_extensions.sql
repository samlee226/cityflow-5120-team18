-- Runs once, when the data volume is first created. Later schema changes
-- belong in database/migrations/ and are applied with database/migrate.py.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgrouting;
