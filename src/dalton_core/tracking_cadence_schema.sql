CREATE TABLE IF NOT EXISTS tracking_cadence_versions (
    version_id TEXT PRIMARY KEY,
    cadence_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES tracking_cadence_versions(version_id),
    company_ref TEXT NOT NULL,
    source_key TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL CHECK(interval_seconds >= 60),
    change_reason TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(cadence_ref, version_number)
);

CREATE INDEX IF NOT EXISTS tracking_cadence_by_company
ON tracking_cadence_versions(company_ref, source_key, version_number DESC);

CREATE TRIGGER IF NOT EXISTS tracking_cadence_insert_guard
BEFORE INSERT ON tracking_cadence_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'tracking cadence insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS tracking_cadence_no_update
BEFORE UPDATE ON tracking_cadence_versions BEGIN
    SELECT RAISE(ABORT, 'tracking cadence versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS tracking_cadence_no_delete
BEFORE DELETE ON tracking_cadence_versions BEGIN
    SELECT RAISE(ABORT, 'tracking cadence versions are immutable');
END;
