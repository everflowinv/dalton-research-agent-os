PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS model_forecast_line_versions (
    version_id TEXT PRIMARY KEY,
    line_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES model_forecast_line_versions(version_id),
    subject_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(line_ref, version_number)
);

CREATE TABLE IF NOT EXISTS model_forecast_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_model_forecast_line_history
ON model_forecast_line_versions(line_ref, version_number);

CREATE TRIGGER IF NOT EXISTS model_forecast_line_authorized_insert
BEFORE INSERT ON model_forecast_line_versions WHEN dalton_model_forecast_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'forecast line insert requires ModelForecastAuthority'); END;
CREATE TRIGGER IF NOT EXISTS model_forecast_idempotency_authorized_insert
BEFORE INSERT ON model_forecast_idempotency WHEN dalton_model_forecast_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'forecast idempotency insert requires ModelForecastAuthority'); END;

CREATE TRIGGER IF NOT EXISTS model_forecast_line_no_update
BEFORE UPDATE ON model_forecast_line_versions BEGIN SELECT RAISE(ABORT, 'forecast line versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS model_forecast_line_no_delete
BEFORE DELETE ON model_forecast_line_versions BEGIN SELECT RAISE(ABORT, 'forecast line versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS model_forecast_idempotency_no_update
BEFORE UPDATE ON model_forecast_idempotency BEGIN SELECT RAISE(ABORT, 'forecast idempotency is immutable'); END;
CREATE TRIGGER IF NOT EXISTS model_forecast_idempotency_no_delete
BEFORE DELETE ON model_forecast_idempotency BEGIN SELECT RAISE(ABORT, 'forecast idempotency is immutable'); END;
