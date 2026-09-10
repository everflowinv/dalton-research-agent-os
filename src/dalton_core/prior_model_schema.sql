PRAGMA foreign_keys = ON;

-- W3: a prior human Excel model, imported cell by cell.
--
-- Deliberately its own table and not a shape inside `forecast_model_versions`.
-- An assumption row of a ForecastModelVersion is load-bearing arithmetic: it
-- names a driver that exists because a filing carries it, and its value feeds
-- `compute_results` and then published forecast lines. A prior model's
-- assumptions are about lines that frequently have no filed driver at all,
-- and they must be inert -- readable, comparable, never computed with. The
-- only way to make a row inert is to keep it out of the record that computes.
--
-- So nothing here is reachable from the statement, forecast-actual or
-- `mission_figure_authority` paths, and the dependency arrow is one-way: M3
-- may import this module, and this module imports nothing from M2, the claim
-- layer or the statements layer.

CREATE TABLE IF NOT EXISTS prior_model_versions (
    version_id TEXT PRIMARY KEY,
    model_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES prior_model_versions(version_id),
    company_ref TEXT NOT NULL,
    -- The prior-research document this workbook was read from, so a band can
    -- always be walked back to a file the owner named in a manifest.
    source_document_ref TEXT NOT NULL,
    -- The manifest's date. Not the file's mtime and not today: a model
    -- maintained in 2024 holds 2024's assumptions however recently it was
    -- opened.
    as_of TEXT NOT NULL,
    workbook_sha256 TEXT NOT NULL,
    assumption_count INTEGER NOT NULL CHECK(assumption_count >= 0),
    body_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(model_ref, version_number)
);

CREATE INDEX IF NOT EXISTS idx_prior_model_history
ON prior_model_versions(model_ref, version_number);

CREATE INDEX IF NOT EXISTS idx_prior_model_company
ON prior_model_versions(company_ref, as_of);

CREATE TRIGGER IF NOT EXISTS prior_model_authorized_insert
BEFORE INSERT ON prior_model_versions WHEN dalton_prior_model_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'prior model insert requires PriorModelAuthority'); END;

CREATE TRIGGER IF NOT EXISTS prior_model_no_update
BEFORE UPDATE ON prior_model_versions BEGIN
    SELECT RAISE(ABORT, 'prior model versions are immutable'); END;

CREATE TRIGGER IF NOT EXISTS prior_model_no_delete
BEFORE DELETE ON prior_model_versions BEGIN
    SELECT RAISE(ABORT, 'prior model versions are immutable'); END;
