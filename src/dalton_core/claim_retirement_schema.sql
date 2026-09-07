-- P10b: challenge and retire a Claim without rewriting the Ledger.
--
-- The Research Ledger is append-only and its ClaimVersion contract is frozen,
-- so a wrong Claim is never edited or deleted.  Two append-only records carry
-- the correction instead: a challenge (who says it is wrong, by which
-- deterministic detector, against which exact original) and a retirement (the
-- decision that the challenge stands).  Every read path that answers a
-- question or drafts a deliverable skips retired Claims; the Ledger itself is
-- untouched and every historical hash still verifies.

CREATE TABLE IF NOT EXISTS claim_retirement_challenges (
    challenge_id TEXT PRIMARY KEY,
    claim_version_ref TEXT NOT NULL REFERENCES claim_versions(claim_version_id),
    claim_version_hash TEXT NOT NULL,
    claim_ref TEXT NOT NULL,
    subject_ref TEXT NOT NULL,
    reason_code TEXT NOT NULL CHECK(reason_code IN (
        'subject_absent_from_source',
        'boilerplate_disclaimer',
        'human_judgment'
    )),
    detector_ref TEXT,
    detector_hash TEXT,
    rationale TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(claim_version_ref, reason_code)
);

-- One decision per challenged Claim.  ``kept`` is recorded too, so a Claim a
-- person has already looked at and kept does not come back every tick.
CREATE TABLE IF NOT EXISTS claim_retirement_decisions (
    decision_id TEXT PRIMARY KEY,
    claim_version_ref TEXT NOT NULL UNIQUE REFERENCES claim_versions(claim_version_id),
    challenge_ref TEXT NOT NULL REFERENCES claim_retirement_challenges(challenge_id),
    challenge_hash TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('retired','kept')),
    actor_ref TEXT NOT NULL,
    rationale TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claim_retirement_challenges_claim
ON claim_retirement_challenges(claim_version_ref, created_at);
CREATE INDEX IF NOT EXISTS idx_claim_retirement_challenges_subject
ON claim_retirement_challenges(subject_ref, created_at);

CREATE TRIGGER IF NOT EXISTS claim_retirement_challenges_authorized_insert
BEFORE INSERT ON claim_retirement_challenges WHEN dalton_claim_retirement_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'claim challenge insert requires ClaimRetirementAuthority'); END;
CREATE TRIGGER IF NOT EXISTS claim_retirement_challenges_no_update
BEFORE UPDATE ON claim_retirement_challenges BEGIN
    SELECT RAISE(ABORT, 'claim challenges are append-only'); END;
CREATE TRIGGER IF NOT EXISTS claim_retirement_challenges_no_delete
BEFORE DELETE ON claim_retirement_challenges BEGIN
    SELECT RAISE(ABORT, 'claim challenges are append-only'); END;

CREATE TRIGGER IF NOT EXISTS claim_retirement_decisions_authorized_insert
BEFORE INSERT ON claim_retirement_decisions WHEN dalton_claim_retirement_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'claim challenge decision insert requires ClaimRetirementAuthority'); END;
CREATE TRIGGER IF NOT EXISTS claim_retirement_decisions_no_update
BEFORE UPDATE ON claim_retirement_decisions BEGIN
    SELECT RAISE(ABORT, 'claim challenge decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS claim_retirement_decisions_no_delete
BEFORE DELETE ON claim_retirement_decisions BEGIN
    SELECT RAISE(ABORT, 'claim challenge decisions are append-only'); END;
