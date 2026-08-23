PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS industry_evidence_pack_versions (
    version_id TEXT PRIMARY KEY,
    evidence_pack_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES industry_evidence_pack_versions(version_id),
    industry_ref TEXT NOT NULL,
    driver_pack_version_ref TEXT NOT NULL REFERENCES driver_pack_versions(version_id),
    driver_pack_version_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(evidence_pack_ref, version_number)
);

CREATE TABLE IF NOT EXISTS industry_evidence_pack_pointer (
    evidence_pack_ref TEXT PRIMARY KEY,
    version_id TEXT NOT NULL UNIQUE REFERENCES industry_evidence_pack_versions(version_id),
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_overlay_versions (
    version_id TEXT PRIMARY KEY,
    overlay_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES company_overlay_versions(version_id),
    company_ref TEXT NOT NULL,
    industry_ref TEXT NOT NULL,
    evidence_pack_version_ref TEXT NOT NULL REFERENCES industry_evidence_pack_versions(version_id),
    evidence_pack_version_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(overlay_ref, version_number)
);

CREATE TABLE IF NOT EXISTS company_overlay_pointer (
    overlay_ref TEXT PRIMARY KEY,
    version_id TEXT NOT NULL UNIQUE REFERENCES company_overlay_versions(version_id),
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS industry_research_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_industry_evidence_pack_history
ON industry_evidence_pack_versions(evidence_pack_ref, version_number);

CREATE INDEX IF NOT EXISTS idx_company_overlay_history
ON company_overlay_versions(overlay_ref, version_number);

CREATE TRIGGER IF NOT EXISTS industry_evidence_pack_versions_authorized_insert
BEFORE INSERT ON industry_evidence_pack_versions WHEN dalton_industry_research_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'industry evidence pack insert requires IndustryResearchAuthority'); END;
CREATE TRIGGER IF NOT EXISTS industry_evidence_pack_pointer_authorized_insert
BEFORE INSERT ON industry_evidence_pack_pointer WHEN dalton_industry_research_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'industry evidence pack pointer insert requires IndustryResearchAuthority'); END;
CREATE TRIGGER IF NOT EXISTS industry_evidence_pack_pointer_authorized_update
BEFORE UPDATE ON industry_evidence_pack_pointer WHEN dalton_industry_research_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'industry evidence pack pointer update requires IndustryResearchAuthority'); END;
CREATE TRIGGER IF NOT EXISTS company_overlay_versions_authorized_insert
BEFORE INSERT ON company_overlay_versions WHEN dalton_industry_research_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'company overlay insert requires IndustryResearchAuthority'); END;
CREATE TRIGGER IF NOT EXISTS company_overlay_pointer_authorized_insert
BEFORE INSERT ON company_overlay_pointer WHEN dalton_industry_research_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'company overlay pointer insert requires IndustryResearchAuthority'); END;
CREATE TRIGGER IF NOT EXISTS company_overlay_pointer_authorized_update
BEFORE UPDATE ON company_overlay_pointer WHEN dalton_industry_research_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'company overlay pointer update requires IndustryResearchAuthority'); END;
CREATE TRIGGER IF NOT EXISTS industry_research_idempotency_authorized_insert
BEFORE INSERT ON industry_research_idempotency WHEN dalton_industry_research_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'industry research idempotency insert requires IndustryResearchAuthority'); END;

CREATE TRIGGER IF NOT EXISTS industry_evidence_pack_versions_no_update
BEFORE UPDATE ON industry_evidence_pack_versions BEGIN SELECT RAISE(ABORT, 'industry evidence pack versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS industry_evidence_pack_versions_no_delete
BEFORE DELETE ON industry_evidence_pack_versions BEGIN SELECT RAISE(ABORT, 'industry evidence pack versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS industry_evidence_pack_pointer_no_delete
BEFORE DELETE ON industry_evidence_pack_pointer BEGIN SELECT RAISE(ABORT, 'industry evidence pack pointer cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS company_overlay_versions_no_update
BEFORE UPDATE ON company_overlay_versions BEGIN SELECT RAISE(ABORT, 'company overlay versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS company_overlay_versions_no_delete
BEFORE DELETE ON company_overlay_versions BEGIN SELECT RAISE(ABORT, 'company overlay versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS company_overlay_pointer_no_delete
BEFORE DELETE ON company_overlay_pointer BEGIN SELECT RAISE(ABORT, 'company overlay pointer cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS industry_research_idempotency_no_update
BEFORE UPDATE ON industry_research_idempotency BEGIN SELECT RAISE(ABORT, 'industry research idempotency is immutable'); END;
CREATE TRIGGER IF NOT EXISTS industry_research_idempotency_no_delete
BEFORE DELETE ON industry_research_idempotency BEGIN SELECT RAISE(ABORT, 'industry research idempotency is immutable'); END;
