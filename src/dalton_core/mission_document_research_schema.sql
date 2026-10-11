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

-- 2026-09-30: the owner's governed close of a document-research admission.
-- Admissions stay append-only; a withdrawal is one more immutable fact about
-- one admission.  One per admission, only a person may write it, and it is
-- never edited or removed.
CREATE TABLE IF NOT EXISTS mission_document_research_withdrawals (
    withdrawal_id TEXT PRIMARY KEY,
    admission_ref TEXT NOT NULL UNIQUE,
    admission_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL CHECK(actor_ref LIKE 'human:%'),
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS mission_document_research_withdrawals_authorized_insert
BEFORE INSERT ON mission_document_research_withdrawals
WHEN dalton_mission_document_withdrawal_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission document withdrawal requires MissionDocumentWithdrawalAuthority');
END;
CREATE TRIGGER IF NOT EXISTS mission_document_research_withdrawals_no_update
BEFORE UPDATE ON mission_document_research_withdrawals BEGIN
    SELECT RAISE(ABORT, 'mission document withdrawals are append-only');
END;
CREATE TRIGGER IF NOT EXISTS mission_document_research_withdrawals_no_delete
BEFORE DELETE ON mission_document_research_withdrawals BEGIN
    SELECT RAISE(ABORT, 'mission document withdrawals are append-only');
END;

-- One local-only, versioned reconciliation of existing paid numeric proofs.
-- The existing MissionDocumentResearchAuthority owns this additive migration.
CREATE TABLE IF NOT EXISTS mission_document_numeric_local_reconciliations (
    admission_ref TEXT NOT NULL REFERENCES mission_document_research_admissions(admission_id),
    repair_version TEXT NOT NULL CHECK(repair_version = 'numeric-local-figure-reconciliation:0.1'),
    authorization_ref TEXT NOT NULL UNIQUE,
    admission_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(admission_ref, repair_version)
);
CREATE TRIGGER IF NOT EXISTS numeric_local_reconciliations_authorized_insert
BEFORE INSERT ON mission_document_numeric_local_reconciliations
WHEN dalton_mission_document_research_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'local numeric reconciliation requires MissionDocumentResearchAuthority');
END;
CREATE TRIGGER IF NOT EXISTS numeric_local_reconciliations_no_update
BEFORE UPDATE ON mission_document_numeric_local_reconciliations BEGIN
    SELECT RAISE(ABORT, 'local numeric reconciliation is append-only');
END;
CREATE TRIGGER IF NOT EXISTS numeric_local_reconciliations_no_delete
BEFORE DELETE ON mission_document_numeric_local_reconciliations BEGIN
    SELECT RAISE(ABORT, 'local numeric reconciliation is append-only');
END;

CREATE TABLE IF NOT EXISTS mission_document_numeric_measure_reconciliations (
 admission_ref TEXT PRIMARY KEY REFERENCES mission_document_research_admissions(admission_id),
 authorization_ref TEXT NOT NULL UNIQUE,
 admission_hash TEXT NOT NULL, record_json TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS mission_document_numeric_measure_reconciliations_authorized_insert
BEFORE INSERT ON mission_document_numeric_measure_reconciliations WHEN dalton_mission_document_research_authorized()=0
BEGIN SELECT RAISE(ABORT,'numeric measure reconciliation requires authority'); END;
CREATE TRIGGER IF NOT EXISTS mission_document_numeric_measure_reconciliations_no_update BEFORE UPDATE ON mission_document_numeric_measure_reconciliations
BEGIN SELECT RAISE(ABORT,'numeric measure reconciliation is append-only'); END;
CREATE TRIGGER IF NOT EXISTS mission_document_numeric_measure_reconciliations_no_delete BEFORE DELETE ON mission_document_numeric_measure_reconciliations
BEGIN SELECT RAISE(ABORT,'numeric measure reconciliation is append-only'); END;
