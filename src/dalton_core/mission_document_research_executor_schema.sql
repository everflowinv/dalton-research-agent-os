PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS mission_document_research_starts (
 start_id TEXT PRIMARY KEY, admission_ref TEXT NOT NULL, admission_hash TEXT NOT NULL,
 run_id TEXT NOT NULL UNIQUE, root_work_order_ref TEXT NOT NULL,
 root_work_order_hash TEXT NOT NULL, record_json TEXT NOT NULL,
 content_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mission_document_research_outcomes (
 outcome_id TEXT PRIMARY KEY, start_ref TEXT NOT NULL, admission_ref TEXT NOT NULL UNIQUE,
 question_version_ref TEXT NOT NULL, question_version_hash TEXT NOT NULL,
 candidate_evidence_ref TEXT NOT NULL, candidate_evidence_hash TEXT NOT NULL,
 candidate_claim_ref TEXT NOT NULL, candidate_claim_hash TEXT NOT NULL,
 record_json TEXT NOT NULL, content_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mission_document_research_observations (
 observation_id TEXT PRIMARY KEY, admission_ref TEXT NOT NULL,
 admission_hash TEXT NOT NULL, mission_version_ref TEXT NOT NULL,
 company_ref TEXT NOT NULL, inquiry_ref TEXT NOT NULL, inquiry_hash TEXT NOT NULL,
 outcome TEXT NOT NULL CHECK(outcome IN ('query_miss','no_verified_claim','recovery_required','candidate_staged')),
 record_json TEXT NOT NULL, content_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS mission_document_research_starts_no_update
BEFORE UPDATE ON mission_document_research_starts BEGIN SELECT RAISE(ABORT,'mission document starts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS mission_document_research_starts_no_delete
BEFORE DELETE ON mission_document_research_starts BEGIN SELECT RAISE(ABORT,'mission document starts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS mission_document_research_outcomes_no_update
BEFORE UPDATE ON mission_document_research_outcomes BEGIN SELECT RAISE(ABORT,'mission document outcomes are append-only'); END;
CREATE TRIGGER IF NOT EXISTS mission_document_research_outcomes_no_delete
BEFORE DELETE ON mission_document_research_outcomes BEGIN SELECT RAISE(ABORT,'mission document outcomes are append-only'); END;
CREATE TRIGGER IF NOT EXISTS mission_document_research_observations_no_update
BEFORE UPDATE ON mission_document_research_observations BEGIN SELECT RAISE(ABORT,'mission document observations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS mission_document_research_observations_no_delete
BEFORE DELETE ON mission_document_research_observations BEGIN SELECT RAISE(ABORT,'mission document observations are append-only'); END;
