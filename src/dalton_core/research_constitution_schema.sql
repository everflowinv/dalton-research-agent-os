PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS research_constitution_versions (
    constitution_version_id TEXT PRIMARY KEY,
    constitution_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES research_constitution_versions(constitution_version_id),
    industry_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(constitution_ref, version_number)
);

CREATE TABLE IF NOT EXISTS research_constitution_pointer (
    constitution_ref TEXT PRIMARY KEY,
    constitution_version_id TEXT NOT NULL UNIQUE REFERENCES research_constitution_versions(constitution_version_id),
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_constitution_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_constitution_history
ON research_constitution_versions(constitution_ref, version_number);

CREATE TRIGGER IF NOT EXISTS research_constitution_versions_authorized_insert
BEFORE INSERT ON research_constitution_versions WHEN dalton_research_constitution_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research constitution insert requires ResearchConstitutionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS research_constitution_pointer_authorized_insert
BEFORE INSERT ON research_constitution_pointer WHEN dalton_research_constitution_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research constitution pointer insert requires ResearchConstitutionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS research_constitution_pointer_authorized_update
BEFORE UPDATE ON research_constitution_pointer WHEN dalton_research_constitution_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research constitution pointer update requires ResearchConstitutionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS research_constitution_idempotency_authorized_insert
BEFORE INSERT ON research_constitution_idempotency WHEN dalton_research_constitution_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research constitution idempotency insert requires ResearchConstitutionAuthority'); END;

CREATE TRIGGER IF NOT EXISTS research_constitution_versions_no_update
BEFORE UPDATE ON research_constitution_versions BEGIN SELECT RAISE(ABORT, 'research constitution versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_constitution_versions_no_delete
BEFORE DELETE ON research_constitution_versions BEGIN SELECT RAISE(ABORT, 'research constitution versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_constitution_pointer_no_delete
BEFORE DELETE ON research_constitution_pointer BEGIN SELECT RAISE(ABORT, 'research constitution pointer cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS research_constitution_idempotency_no_update
BEFORE UPDATE ON research_constitution_idempotency BEGIN SELECT RAISE(ABORT, 'research constitution idempotency is immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_constitution_idempotency_no_delete
BEFORE DELETE ON research_constitution_idempotency BEGIN SELECT RAISE(ABORT, 'research constitution idempotency is immutable'); END;
