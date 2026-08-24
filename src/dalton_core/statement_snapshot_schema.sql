CREATE TABLE IF NOT EXISTS statement_concept_set_versions (
    version_id TEXT PRIMARY KEY,
    concept_set_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES statement_concept_set_versions(version_id),
    statement_type TEXT NOT NULL CHECK(statement_type = 'balance_sheet'),
    taxonomy TEXT NOT NULL,
    unit TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(concept_set_ref, version_number)
);

CREATE TABLE IF NOT EXISTS statement_snapshot_versions (
    version_id TEXT PRIMARY KEY,
    snapshot_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES statement_snapshot_versions(version_id),
    concept_set_version_ref TEXT NOT NULL REFERENCES statement_concept_set_versions(version_id),
    source_artifact_version_ref TEXT NOT NULL,
    source_artifact_version_hash TEXT NOT NULL,
    source_content_hash TEXT NOT NULL,
    accession TEXT NOT NULL,
    issuer_cik TEXT NOT NULL,
    period_end TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(snapshot_ref, version_number)
);

CREATE INDEX IF NOT EXISTS statement_snapshots_by_accession
ON statement_snapshot_versions(issuer_cik, accession, period_end);

CREATE TRIGGER IF NOT EXISTS statement_concept_set_insert_guard
BEFORE INSERT ON statement_concept_set_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'statement concept set insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS statement_snapshot_insert_guard
BEFORE INSERT ON statement_snapshot_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'statement snapshot insert requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS statement_concept_set_no_update
BEFORE UPDATE ON statement_concept_set_versions BEGIN
    SELECT RAISE(ABORT, 'statement concept sets are immutable');
END;
CREATE TRIGGER IF NOT EXISTS statement_concept_set_no_delete
BEFORE DELETE ON statement_concept_set_versions BEGIN
    SELECT RAISE(ABORT, 'statement concept sets are immutable');
END;
CREATE TRIGGER IF NOT EXISTS statement_snapshot_no_update
BEFORE UPDATE ON statement_snapshot_versions BEGIN
    SELECT RAISE(ABORT, 'statement snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS statement_snapshot_no_delete
BEFORE DELETE ON statement_snapshot_versions BEGIN
    SELECT RAISE(ABORT, 'statement snapshots are immutable');
END;
