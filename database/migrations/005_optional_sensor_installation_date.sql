-- CityFlow migration 005: allow optional sensor installation metadata.

ALTER TABLE sensors
    ALTER COLUMN installation_date DROP NOT NULL;

COMMENT ON COLUMN sensors.installation_date IS
    'Optional source installation date. Missing metadata is stored as SQL NULL; '
    'non-empty malformed source dates are rejected before loading.';
