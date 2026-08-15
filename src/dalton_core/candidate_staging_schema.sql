PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS candidate_source_materials (
    material_id TEXT PRIMARY KEY,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_numeric_specs (
    numeric_spec_id TEXT PRIMARY KEY,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_verifications (
    verification_id TEXT PRIMARY KEY,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_evidence_versions (
    version_id TEXT PRIMARY KEY,
    candidate_evidence_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(candidate_evidence_ref, version_number),
    FOREIGN KEY(prior_version_id) REFERENCES candidate_evidence_versions(version_id)
);

CREATE TABLE IF NOT EXISTS candidate_claim_versions (
    version_id TEXT PRIMARY KEY,
    candidate_claim_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT,
    evidence_version_id TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(candidate_claim_ref, version_number),
    FOREIGN KEY(prior_version_id) REFERENCES candidate_claim_versions(version_id),
    FOREIGN KEY(evidence_version_id) REFERENCES candidate_evidence_versions(version_id)
);

CREATE TABLE IF NOT EXISTS candidate_stage_requests (
    idempotency_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS candidate_source_materials_no_update
BEFORE UPDATE ON candidate_source_materials BEGIN SELECT RAISE(ABORT, 'candidate_source_materials is immutable'); END;
CREATE TRIGGER IF NOT EXISTS candidate_source_materials_no_delete
BEFORE DELETE ON candidate_source_materials BEGIN SELECT RAISE(ABORT, 'candidate_source_materials is immutable'); END;
CREATE TRIGGER IF NOT EXISTS candidate_numeric_specs_no_update
BEFORE UPDATE ON candidate_numeric_specs BEGIN SELECT RAISE(ABORT, 'candidate_numeric_specs is immutable'); END;
CREATE TRIGGER IF NOT EXISTS candidate_numeric_specs_no_delete
BEFORE DELETE ON candidate_numeric_specs BEGIN SELECT RAISE(ABORT, 'candidate_numeric_specs is immutable'); END;
CREATE TRIGGER IF NOT EXISTS candidate_verifications_no_update
BEFORE UPDATE ON candidate_verifications BEGIN SELECT RAISE(ABORT, 'candidate_verifications is immutable'); END;
CREATE TRIGGER IF NOT EXISTS candidate_verifications_no_delete
BEFORE DELETE ON candidate_verifications BEGIN SELECT RAISE(ABORT, 'candidate_verifications is immutable'); END;
CREATE TRIGGER IF NOT EXISTS candidate_evidence_versions_no_update
BEFORE UPDATE ON candidate_evidence_versions BEGIN SELECT RAISE(ABORT, 'candidate_evidence_versions is immutable'); END;
CREATE TRIGGER IF NOT EXISTS candidate_evidence_versions_no_delete
BEFORE DELETE ON candidate_evidence_versions BEGIN SELECT RAISE(ABORT, 'candidate_evidence_versions is immutable'); END;
CREATE TRIGGER IF NOT EXISTS candidate_claim_versions_no_update
BEFORE UPDATE ON candidate_claim_versions BEGIN SELECT RAISE(ABORT, 'candidate_claim_versions is immutable'); END;
CREATE TRIGGER IF NOT EXISTS candidate_claim_versions_no_delete
BEFORE DELETE ON candidate_claim_versions BEGIN SELECT RAISE(ABORT, 'candidate_claim_versions is immutable'); END;
CREATE TRIGGER IF NOT EXISTS candidate_stage_requests_no_update
BEFORE UPDATE ON candidate_stage_requests BEGIN SELECT RAISE(ABORT, 'candidate_stage_requests is immutable'); END;
CREATE TRIGGER IF NOT EXISTS candidate_stage_requests_no_delete
BEFORE DELETE ON candidate_stage_requests BEGIN SELECT RAISE(ABORT, 'candidate_stage_requests is immutable'); END;
