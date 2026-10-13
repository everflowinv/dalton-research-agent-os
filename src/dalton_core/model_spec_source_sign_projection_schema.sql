CREATE TABLE IF NOT EXISTS company_model_spec_source_sign_projections (
    projection_ref TEXT PRIMARY KEY,
    root_work_ref TEXT NOT NULL UNIQUE,
    source_work_ref TEXT NOT NULL UNIQUE,
    company_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS model_source_sign_projection_owner
BEFORE INSERT ON company_model_spec_source_sign_projections
WHEN dalton_coverage_mission_authorized() != 1
BEGIN SELECT RAISE(ABORT,'source-sign projection requires owner authority'); END;
CREATE TRIGGER IF NOT EXISTS model_source_sign_projection_no_update
BEFORE UPDATE ON company_model_spec_source_sign_projections
BEGIN SELECT RAISE(ABORT,'source-sign projection is immutable'); END;
CREATE TRIGGER IF NOT EXISTS model_source_sign_projection_no_delete
BEFORE DELETE ON company_model_spec_source_sign_projections
BEGIN SELECT RAISE(ABORT,'source-sign projection is immutable'); END;
