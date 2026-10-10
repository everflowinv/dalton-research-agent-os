-- 2026-09-29: a document read to the end and admitted, whatever happened to
-- its review afterwards.
--
-- DXC's Guidepoint excerpt d50efc... was read under mission v26 at 11:32 and
-- four Claims went in; the mission moved to v27 before the review could be
-- closed ("completion proof failed: source discovery must bind the active
-- mission version"), so the review stayed open, was carried to v27 and v28 as
-- open, and was read -- and admitted from -- again at 12:31.  The review row
-- could not record the conclusion; this row does, the moment the last window
-- has gone through admission.  A carry into a later version, and the reading
-- queue, read it and carry the conclusion instead of opening the document
-- again.  One row per review; append-only.
CREATE TABLE IF NOT EXISTS document_admission_outcomes (
    review_id TEXT PRIMARY KEY,
    document_ref TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    mission_version_ref TEXT NOT NULL,
    -- The staged or committed candidate the review would have been closed with.
    candidate_claim_ref TEXT NOT NULL,
    claim_version_refs_json TEXT NOT NULL,
    admitted INTEGER NOT NULL CHECK (admitted >= 0),
    held INTEGER NOT NULL CHECK (held >= 0),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_document_admission_outcomes_document
ON document_admission_outcomes (document_ref, company_ref, created_at);
CREATE TRIGGER IF NOT EXISTS document_admission_outcomes_authorized_insert
BEFORE INSERT ON document_admission_outcomes WHEN dalton_document_admission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'document admission outcome insert requires the extraction admission'); END;
CREATE TRIGGER IF NOT EXISTS document_admission_outcomes_no_update
BEFORE UPDATE ON document_admission_outcomes BEGIN
    SELECT RAISE(ABORT, 'document admission outcomes are append-only'); END;
CREATE TRIGGER IF NOT EXISTS document_admission_outcomes_no_delete
BEFORE DELETE ON document_admission_outcomes BEGIN
    SELECT RAISE(ABORT, 'document admission outcomes are append-only'); END;
