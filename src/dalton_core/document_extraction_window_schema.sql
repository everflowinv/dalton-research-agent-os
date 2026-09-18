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

-- C2-3: the reviews a secondary pass has proved it has nothing left to take.
--
-- The window exclusions above say "this *window* is dead".  They cannot say
-- "this *document* is finished", which is the other half of the live stall:
-- every window of a review replayed for free, recording nothing, so the pass
-- re-derived the whole document every tick -- 107 window contexts over four
-- reviews, for two hours, while 114 never-read documents waited behind them.
-- A row here is that verdict, written once, scoped to the same identity the
-- exclusions use: the review's bytes (``source_review_hash``) and the model
-- that read them, so a re-acquisition or a model swap reads it again.
CREATE TABLE IF NOT EXISTS document_extraction_review_exhaustion (
    exhaustion_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    source_review_hash TEXT NOT NULL,
    -- Which pass is finished with it: 'numeric' or 'discovery'.  A document
    -- the figures pass has drained may still owe the discovery pass a read.
    pass_ref TEXT NOT NULL,
    model_config_hash TEXT,
    -- 'windows_exhausted' : every window replayed and recorded nothing.
    -- 'not_attributed'    : the document names no company this lane covers,
    --                       which is a fact about the whole document.
    reason TEXT NOT NULL CHECK (reason IN ('windows_exhausted', 'not_attributed')),
    detail TEXT NOT NULL,
    windows INTEGER NOT NULL DEFAULT 0 CHECK (windows >= 0),
    document_ref TEXT,
    company_ref TEXT,
    hit_count INTEGER NOT NULL DEFAULT 1 CHECK (hit_count >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (review_id, source_review_hash, pass_ref)
);
CREATE INDEX IF NOT EXISTS idx_document_extraction_review_exhaustion_pass
ON document_extraction_review_exhaustion (pass_ref, review_id, source_review_hash);
CREATE TRIGGER IF NOT EXISTS document_extraction_review_exhaustion_authorized_insert
BEFORE INSERT ON document_extraction_review_exhaustion
WHEN dalton_document_extraction_window_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'document extraction review exhaustion insert requires authority');
END;
CREATE TRIGGER IF NOT EXISTS document_extraction_review_exhaustion_authorized_update
BEFORE UPDATE ON document_extraction_review_exhaustion
WHEN dalton_document_extraction_window_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'document extraction review exhaustion update requires authority');
END;
CREATE TRIGGER IF NOT EXISTS document_extraction_review_exhaustion_no_delete
BEFORE DELETE ON document_extraction_review_exhaustion BEGIN
    SELECT RAISE(ABORT, 'document extraction review exhaustion cannot be deleted');
END;
