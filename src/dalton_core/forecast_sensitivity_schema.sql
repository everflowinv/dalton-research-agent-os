PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sensitivity_projections (
    projection_id TEXT PRIMARY KEY,
    projection_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_projection_id TEXT REFERENCES sensitivity_projections(projection_id),
    company_ref TEXT NOT NULL,
    model_version_ref TEXT NOT NULL,
    model_version_hash TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(projection_ref, version_number)
);

CREATE INDEX IF NOT EXISTS idx_sensitivity_history
ON sensitivity_projections(projection_ref, version_number);

CREATE INDEX IF NOT EXISTS idx_sensitivity_company
ON sensitivity_projections(company_ref, version_number);

CREATE INDEX IF NOT EXISTS idx_sensitivity_model
ON sensitivity_projections(model_version_ref);

CREATE TRIGGER IF NOT EXISTS sensitivity_projection_authorized_insert
BEFORE INSERT ON sensitivity_projections WHEN dalton_sensitivity_projection_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'sensitivity projection insert requires SensitivityProjectionAuthority'); END;

CREATE TRIGGER IF NOT EXISTS sensitivity_projection_no_update
BEFORE UPDATE ON sensitivity_projections BEGIN
    SELECT RAISE(ABORT, 'sensitivity projections are immutable'); END;

CREATE TRIGGER IF NOT EXISTS sensitivity_projection_no_delete
BEFORE DELETE ON sensitivity_projections BEGIN
    SELECT RAISE(ABORT, 'sensitivity projections are immutable'); END;
