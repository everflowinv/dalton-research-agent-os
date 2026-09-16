-- C2-1: the windows the extraction lane has already proved it cannot read.
--
-- Append-mostly rather than append-only: a row is one window's *current*
-- exclusion, and it is re-stated (hit_count, updated_at) every time the lane
-- walks past it.  Deleting is still refused -- a window that stops being
-- excluded is superseded by a row whose ``retry_after`` has passed, never by
-- forgetting that it failed.
CREATE TABLE IF NOT EXISTS document_extraction_window_exclusions (
    exclusion_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    source_review_hash TEXT NOT NULL,
    window_offset INTEGER NOT NULL CHECK (window_offset >= 0),
    -- Where the reader continues without re-deriving this window's context.
    -- NULL means "this was the last window of the document".
    next_offset INTEGER,
    document_ref TEXT,
    company_ref TEXT,
    work_order_ref TEXT,
    error_code TEXT,
    budget_status TEXT,
    -- The model configuration the window failed under.  A verdict like "this
    -- window's output could not be parsed" is a fact about one model, so
    -- swapping the model must re-open it rather than inherit it.
    model_config_hash TEXT,
    -- 'permanent'  : this window will fail again for the same reason forever.
    -- 'deferred'   : the refusal was about the day (a spent pool), not the
    --                bytes; ``retry_after`` says when it may be read again.
    failure_class TEXT NOT NULL CHECK (failure_class IN ('permanent', 'deferred')),
    retry_after TEXT,
    recorded INTEGER NOT NULL DEFAULT 0 CHECK (recorded >= 0),
    reason TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 1 CHECK (hit_count >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (review_id, source_review_hash, window_offset)
);
CREATE INDEX IF NOT EXISTS idx_document_extraction_window_exclusions_review
ON document_extraction_window_exclusions (review_id, source_review_hash, window_offset);
CREATE INDEX IF NOT EXISTS idx_document_extraction_window_exclusions_class
ON document_extraction_window_exclusions (failure_class, retry_after);
CREATE TRIGGER IF NOT EXISTS document_extraction_window_exclusions_authorized_insert
BEFORE INSERT ON document_extraction_window_exclusions
WHEN dalton_document_extraction_window_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'document extraction window exclusion insert requires authority');
END;
CREATE TRIGGER IF NOT EXISTS document_extraction_window_exclusions_authorized_update
BEFORE UPDATE ON document_extraction_window_exclusions
WHEN dalton_document_extraction_window_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'document extraction window exclusion update requires authority');
END;
CREATE TRIGGER IF NOT EXISTS document_extraction_window_exclusions_no_delete
BEFORE DELETE ON document_extraction_window_exclusions BEGIN
    SELECT RAISE(ABORT, 'document extraction window exclusions cannot be deleted');
END;
