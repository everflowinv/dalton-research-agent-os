PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS mission_document_research_admissions (
    admission_id TEXT PRIMARY KEY,
    identity_hash TEXT NOT NULL UNIQUE,
    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
    mission_version_hash TEXT NOT NULL,
    plan_ref TEXT NOT NULL REFERENCES coverage_mission_research_plans(plan_id),
    plan_hash TEXT NOT NULL,
    inquiry_ref TEXT NOT NULL,
    inquiry_hash TEXT NOT NULL,
    question_version_ref TEXT NOT NULL REFERENCES backlog_question_versions(version_id),
    question_version_hash TEXT NOT NULL,
    document_authority_ref TEXT NOT NULL,
    document_authority_hash TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    actor_ref TEXT NOT NULL CHECK(actor_ref LIKE 'automation:%'),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS mission_document_research_admissions_authorized_insert
BEFORE INSERT ON mission_document_research_admissions
WHEN dalton_mission_document_research_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission document research insert requires MissionDocumentResearchAuthority');
END;
CREATE TRIGGER IF NOT EXISTS mission_document_research_admissions_no_update
BEFORE UPDATE ON mission_document_research_admissions BEGIN
    SELECT RAISE(ABORT, 'mission document research admissions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS mission_document_research_admissions_no_delete
BEFORE DELETE ON mission_document_research_admissions BEGIN
    SELECT RAISE(ABORT, 'mission document research admissions are append-only');
END;
