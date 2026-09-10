PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS forecast_model_versions (
    version_id TEXT PRIMARY KEY,
    model_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES forecast_model_versions(version_id),
    company_ref TEXT NOT NULL,
    spec_ref TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(model_ref, version_number)
);

CREATE INDEX IF NOT EXISTS idx_forecast_model_history
ON forecast_model_versions(model_ref, version_number);

CREATE INDEX IF NOT EXISTS idx_forecast_model_company
ON forecast_model_versions(company_ref, version_number);

CREATE TABLE IF NOT EXISTS forecast_model_filing_proofs (
    model_version_id TEXT PRIMARY KEY REFERENCES forecast_model_versions(version_id),
    company_ref TEXT NOT NULL,
    model_content_hash TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    statement_rows_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS forecast_model_authorized_insert
BEFORE INSERT ON forecast_model_versions WHEN dalton_forecast_model_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'forecast model insert requires ForecastModelAuthority'); END;

CREATE TRIGGER IF NOT EXISTS forecast_model_no_update
BEFORE UPDATE ON forecast_model_versions BEGIN
    SELECT RAISE(ABORT, 'forecast model versions are immutable'); END;

CREATE TRIGGER IF NOT EXISTS forecast_model_no_delete
BEFORE DELETE ON forecast_model_versions BEGIN
SELECT RAISE(ABORT, 'forecast model versions are immutable'); END;

CREATE TRIGGER IF NOT EXISTS forecast_model_filing_proof_authorized_insert
BEFORE INSERT ON forecast_model_filing_proofs WHEN dalton_forecast_model_authorized() = 0 BEGIN
SELECT RAISE(ABORT, 'forecast model filing proof insert requires ForecastModelAuthority'); END;
CREATE TRIGGER IF NOT EXISTS forecast_model_filing_proof_no_update
BEFORE UPDATE ON forecast_model_filing_proofs BEGIN
SELECT RAISE(ABORT, 'forecast model filing proofs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS forecast_model_filing_proof_no_delete
BEFORE DELETE ON forecast_model_filing_proofs BEGIN
SELECT RAISE(ABORT, 'forecast model filing proofs are immutable'); END;
