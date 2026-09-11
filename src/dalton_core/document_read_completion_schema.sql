CREATE TABLE IF NOT EXISTS document_read_completion_proofs (
    proof_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL UNIQUE,
    source_review_hash TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS document_read_completion_proofs_authorized_insert
BEFORE INSERT ON document_read_completion_proofs
WHEN dalton_document_read_completion_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'document read completion proof insert requires authority');
END;
CREATE TRIGGER IF NOT EXISTS document_read_completion_proofs_no_update
BEFORE UPDATE ON document_read_completion_proofs BEGIN SELECT RAISE(ABORT, 'document read completion proofs are append-only'); END;
CREATE TRIGGER IF NOT EXISTS document_read_completion_proofs_no_delete
BEFORE DELETE ON document_read_completion_proofs BEGIN SELECT RAISE(ABORT, 'document read completion proofs are append-only'); END;
