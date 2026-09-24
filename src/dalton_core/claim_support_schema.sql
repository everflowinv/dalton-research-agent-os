-- 2026-09-24: the independent support check a qualitative statement passes
-- before the mission admits it, and after, for the ones admitted before it
-- existed.
--
-- One verdict per (contract, subject, statement, cited text).  The key is the
-- content, not the Claim: the same statement drafted from the same sentences
-- is the same question, whether it is asked before admission or by the
-- backfill after, and it is asked -- and paid for -- once.  Append-only: a
-- verdict is the record of what an independent model said about exact bytes,
-- and changing it would be changing what it said.
CREATE TABLE IF NOT EXISTS claim_support_verdicts (
    item_key TEXT PRIMARY KEY,
    contract_ref TEXT NOT NULL,
    subject_ref TEXT NOT NULL,
    statement_sha256 TEXT NOT NULL,
    cited_sha256 TEXT NOT NULL,
    support TEXT NOT NULL CHECK(support IN ('supported', 'not_supported')),
    subject_relation TEXT NOT NULL CHECK(subject_relation IN ('about_subject', 'about_other')),
    other_subject TEXT,
    purpose TEXT NOT NULL,
    work_order_ref TEXT NOT NULL,
    route_decision_ref TEXT,
    producer_route_refs_json TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim_support_verdicts_outcome
ON claim_support_verdicts(support, subject_relation, created_at);
CREATE TRIGGER IF NOT EXISTS claim_support_verdicts_authorized_insert
BEFORE INSERT ON claim_support_verdicts WHEN dalton_claim_support_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'claim support verdict insert requires ClaimSupportVerifier'); END;
CREATE TRIGGER IF NOT EXISTS claim_support_verdicts_no_update
BEFORE UPDATE ON claim_support_verdicts BEGIN
    SELECT RAISE(ABORT, 'claim support verdicts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS claim_support_verdicts_no_delete
BEFORE DELETE ON claim_support_verdicts BEGIN
    SELECT RAISE(ABORT, 'claim support verdicts are append-only'); END;

-- How many times one set of statements has failed to be verified, and in which
-- hour last.  Operational state, not evidence: it bounds retries (one paid
-- attempt per set per hour, a fixed number in all) and is updated in place.
CREATE TABLE IF NOT EXISTS claim_support_attempts (
    request_key TEXT PRIMARY KEY,
    purpose TEXT NOT NULL,
    attempts INTEGER NOT NULL CHECK(attempts >= 1),
    last_bucket TEXT NOT NULL,
    last_reason TEXT,
    updated_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS claim_support_attempts_authorized_insert
BEFORE INSERT ON claim_support_attempts WHEN dalton_claim_support_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'claim support attempt insert requires ClaimSupportVerifier'); END;
CREATE TRIGGER IF NOT EXISTS claim_support_attempts_authorized_update
BEFORE UPDATE ON claim_support_attempts WHEN dalton_claim_support_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'claim support attempt update requires ClaimSupportVerifier'); END;
CREATE TRIGGER IF NOT EXISTS claim_support_attempts_no_delete
BEFORE DELETE ON claim_support_attempts BEGIN
    SELECT RAISE(ABORT, 'claim support attempts cannot be deleted'); END;

-- The backfill's marker, per Claim version and pass: which Claims it has
-- looked at, and what it found -- a verdict (by key), an original it could not
-- read, or a Claim no verifier can be independent of.  Append-only; a new
-- pass ref re-opens every Claim that is not settled by a verdict.  The
-- retirement authority reads the verdict a Claim was bound to through here.
CREATE TABLE IF NOT EXISTS claim_support_backfill_marks (
    claim_version_ref TEXT NOT NULL,
    pass_ref TEXT NOT NULL,
    claim_version_hash TEXT NOT NULL,
    item_key TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('verdict', 'unreadable', 'unverifiable')),
    detail TEXT,
    marked_at TEXT NOT NULL,
    PRIMARY KEY (claim_version_ref, pass_ref)
);
CREATE INDEX IF NOT EXISTS idx_claim_support_backfill_marks_key
ON claim_support_backfill_marks(item_key);
CREATE TRIGGER IF NOT EXISTS claim_support_backfill_marks_authorized_insert
BEFORE INSERT ON claim_support_backfill_marks WHEN dalton_claim_support_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'claim support backfill mark insert requires ClaimSupportVerdictStore'); END;
CREATE TRIGGER IF NOT EXISTS claim_support_backfill_marks_no_update
BEFORE UPDATE ON claim_support_backfill_marks BEGIN
    SELECT RAISE(ABORT, 'claim support backfill marks are append-only'); END;
CREATE TRIGGER IF NOT EXISTS claim_support_backfill_marks_no_delete
BEFORE DELETE ON claim_support_backfill_marks BEGIN
    SELECT RAISE(ABORT, 'claim support backfill marks are append-only'); END;
