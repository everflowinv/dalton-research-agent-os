-- C2-5: which Claims the retirement patrol has already looked at.
--
-- The pass used to leave no trace when a Claim *passed* the detectors, so the
-- "unchallenged Claims, oldest first" query never shrank and every tick
-- re-read the same oldest forty originals for ever.  Live, that is exactly
-- what happened: 72 challenges and 72 decisions, all written on 2026-09-07,
-- then nothing while the Ledger grew from ~1,000 Claims to 6,390.
--
-- An examination is the marker that was missing.  Append-only, one row per
-- Claim version, carrying what it was examined against so a changed citation
-- chain re-opens it and an unchanged one does not.
CREATE TABLE IF NOT EXISTS claim_review_examinations (
    claim_version_ref TEXT PRIMARY KEY,
    claim_version_hash TEXT NOT NULL,
    -- The original the detectors actually read, or NULL when the citation
    -- chain resolved nothing.
    source_content_hash TEXT,
    detector_ref TEXT NOT NULL,
    -- 'clear'      : the detectors read the original and found nothing wrong
    -- 'challenged' : a challenge was raised from this examination
    -- 'unreadable' : the original could not be read; retried, bounded
    outcome TEXT NOT NULL CHECK (outcome IN ('clear', 'challenged', 'unreadable')),
    attempts INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    examined_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim_review_examinations_outcome
ON claim_review_examinations (outcome, examined_at);
CREATE TRIGGER IF NOT EXISTS claim_review_examinations_authorized_insert
BEFORE INSERT ON claim_review_examinations
WHEN dalton_claim_review_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'claim review examination insert requires authority');
END;
CREATE TRIGGER IF NOT EXISTS claim_review_examinations_authorized_update
BEFORE UPDATE ON claim_review_examinations
WHEN dalton_claim_review_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'claim review examination update requires authority');
END;
CREATE TRIGGER IF NOT EXISTS claim_review_examinations_no_delete
BEFORE DELETE ON claim_review_examinations BEGIN
    SELECT RAISE(ABORT, 'claim review examinations cannot be deleted');
END;
