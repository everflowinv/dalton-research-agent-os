CREATE TABLE IF NOT EXISTS mission_document_research_promotions (
 promotion_id TEXT PRIMARY KEY,
 admission_ref TEXT NOT NULL UNIQUE,
 outcome_ref TEXT NOT NULL UNIQUE,
 record_json TEXT NOT NULL,
 content_hash TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS mission_document_research_promotions_no_update
BEFORE UPDATE ON mission_document_research_promotions
BEGIN SELECT RAISE(ABORT,'mission document promotions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS mission_document_research_promotions_no_delete
BEFORE DELETE ON mission_document_research_promotions
BEGIN SELECT RAISE(ABORT,'mission document promotions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS mission_document_research_promotions_authorized_insert
BEFORE INSERT ON mission_document_research_promotions
WHEN dalton_mission_document_research_executor_authorized()=0
BEGIN SELECT RAISE(ABORT,'mission document promotion insert requires executor'); END;
