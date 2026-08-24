CREATE TABLE IF NOT EXISTS transcript_correction_set_versions (
    version_id TEXT PRIMARY KEY,
    correction_set_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES transcript_correction_set_versions(version_id),
    source_manifest_ref TEXT NOT NULL,
    source_manifest_hash TEXT NOT NULL,
    source_content_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(correction_set_ref, version_number)
);

CREATE INDEX IF NOT EXISTS transcript_corrections_by_source
ON transcript_correction_set_versions(source_manifest_ref, source_content_hash);

CREATE TABLE IF NOT EXISTS transcript_claim_citation_bindings (
    binding_id TEXT PRIMARY KEY,
    correction_set_version_ref TEXT NOT NULL
        REFERENCES transcript_correction_set_versions(version_id),
    source_manifest_ref TEXT NOT NULL,
    source_manifest_hash TEXT NOT NULL,
    source_content_hash TEXT NOT NULL,
    source_start INTEGER NOT NULL CHECK(source_start >= 0),
    source_end INTEGER NOT NULL CHECK(source_end > source_start),
    claim_eligible INTEGER NOT NULL CHECK(claim_eligible IN (0, 1)),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS transcript_citations_by_source
ON transcript_claim_citation_bindings(source_manifest_ref, source_content_hash);

CREATE TRIGGER IF NOT EXISTS transcript_correction_insert_guard
BEFORE INSERT ON transcript_correction_set_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'transcript correction insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS transcript_correction_no_update
BEFORE UPDATE ON transcript_correction_set_versions BEGIN
    SELECT RAISE(ABORT, 'transcript correction sets are immutable');
END;
CREATE TRIGGER IF NOT EXISTS transcript_correction_no_delete
BEFORE DELETE ON transcript_correction_set_versions BEGIN
    SELECT RAISE(ABORT, 'transcript correction sets are immutable');
END;

CREATE TRIGGER IF NOT EXISTS transcript_citation_insert_guard
BEFORE INSERT ON transcript_claim_citation_bindings WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'transcript citation insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS transcript_citation_no_update
BEFORE UPDATE ON transcript_claim_citation_bindings BEGIN
    SELECT RAISE(ABORT, 'transcript citation bindings are immutable');
END;
CREATE TRIGGER IF NOT EXISTS transcript_citation_no_delete
BEFORE DELETE ON transcript_claim_citation_bindings BEGIN
    SELECT RAISE(ABORT, 'transcript citation bindings are immutable');
END;
