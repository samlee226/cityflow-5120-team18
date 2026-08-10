-- CityFlow migration 007: support bounded live retention cleanup.

CREATE INDEX idx_live_quarantine_detected_at
    ON pedestrian_counts_minutely_quarantine (detected_at);

CREATE INDEX idx_live_ingestion_runs_completed_at
    ON live_ingestion_runs (completed_at)
    WHERE status <> 'running';
