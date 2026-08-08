-- Convenience psql entry point for a clean CityFlow database.
--
-- database/migrations/ is the authoritative schema source. This file contains
-- only psql include commands so it cannot drift into a second schema copy.
-- Existing databases must use database/migrate.py to retain version/checksum
-- tracking; this wrapper is intended only for clean, manually managed setup.

\ir migrations/001_extensions_and_core_tables.sql
\ir migrations/002_spatial_and_routing_tables.sql
\ir migrations/003_indexes_and_views.sql
\ir migrations/004_live_pedestrian_ingestion.sql
