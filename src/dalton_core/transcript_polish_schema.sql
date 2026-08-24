CREATE TABLE IF NOT EXISTS transcript_polish_artifact_versions (
    version_id TEXT PRIMARY KEY,
    artifact_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES transcript_polish_artifact_versions(version_id),
    source_manifest_ref TEXT NOT NULL,
    source_manifest_hash TEXT NOT NULL,
    source_content_hash TEXT NOT NULL,
    polished_content_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(artifact_ref, version_number)
);

CREATE INDEX IF NOT EXISTS transcript_polish_by_source
ON transcript_polish_artifact_versions(source_manifest_ref, source_content_hash);

CREATE TRIGGER IF NOT EXISTS transcript_polish_insert_guard
BEFORE INSERT ON transcript_polish_artifact_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'transcript polish insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS transcript_polish_no_update
BEFORE UPDATE ON transcript_polish_artifact_versions BEGIN
    SELECT RAISE(ABORT, 'transcript polish artifacts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS transcript_polish_no_delete
BEFORE DELETE ON transcript_polish_artifact_versions BEGIN
    SELECT RAISE(ABORT, 'transcript polish artifacts are immutable');
END;
