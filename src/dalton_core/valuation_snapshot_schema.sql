CREATE TABLE IF NOT EXISTS valuation_snapshot_versions (
    version_id TEXT PRIMARY KEY,
    snapshot_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES valuation_snapshot_versions(version_id),
    company_ref TEXT NOT NULL,
    formula_version TEXT NOT NULL,
    as_of TEXT NOT NULL,
    price_version_ref TEXT NOT NULL,
    price_version_hash TEXT NOT NULL,
    binding_hash TEXT NOT NULL,
    available_metric_count INTEGER NOT NULL CHECK(available_metric_count >= 0),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(snapshot_ref, version_number)
);

CREATE INDEX IF NOT EXISTS valuation_snapshots_by_company
ON valuation_snapshot_versions(company_ref, version_number DESC);

CREATE TRIGGER IF NOT EXISTS valuation_snapshot_insert_guard
BEFORE INSERT ON valuation_snapshot_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'valuation snapshot insert requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS valuation_snapshot_no_update
BEFORE UPDATE ON valuation_snapshot_versions BEGIN
    SELECT RAISE(ABORT, 'valuation snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS valuation_snapshot_no_delete
BEFORE DELETE ON valuation_snapshot_versions BEGIN
    SELECT RAISE(ABORT, 'valuation snapshots are immutable');
END;
