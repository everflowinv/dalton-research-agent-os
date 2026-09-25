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
        'human_judgment',
        'citation_support_rejected'
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

-- 2026-09-24: a retirement shown to be wrong is withdrawn by a further record,
-- never by editing or deleting the decision.  ``claim_retirement_decisions``
-- keeps one row per Claim for ever; a reinstatement names that exact decision
-- (and its hash) and says who withdrew it and why.  Every read path treats a
-- Claim as retired when it has a ``retired`` decision and no reinstatement of
-- that decision (``claim_retirement.retired_claim_version_refs``).
CREATE TABLE IF NOT EXISTS claim_retirement_reinstatements (
    reinstatement_id TEXT PRIMARY KEY,
    claim_version_ref TEXT NOT NULL REFERENCES claim_versions(claim_version_id),
    decision_ref TEXT NOT NULL UNIQUE REFERENCES claim_retirement_decisions(decision_id),
    decision_hash TEXT NOT NULL,
    reason_code TEXT NOT NULL CHECK(reason_code IN (
        'human_judgment',
        'subject_named_under_current_rule'
    )),
    rule_ref TEXT,
    actor_ref TEXT NOT NULL,
    rationale TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claim_retirement_reinstatements_claim
ON claim_retirement_reinstatements(claim_version_ref, created_at);

CREATE TRIGGER IF NOT EXISTS claim_retirement_reinstatements_authorized_insert
BEFORE INSERT ON claim_retirement_reinstatements WHEN dalton_claim_retirement_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'claim reinstatement insert requires ClaimRetirementAuthority'); END;
CREATE TRIGGER IF NOT EXISTS claim_retirement_reinstatements_no_update
BEFORE UPDATE ON claim_retirement_reinstatements BEGIN
    SELECT RAISE(ABORT, 'claim reinstatements are append-only'); END;
CREATE TRIGGER IF NOT EXISTS claim_retirement_reinstatements_no_delete
BEFORE DELETE ON claim_retirement_reinstatements BEGIN
    SELECT RAISE(ABORT, 'claim reinstatements are append-only'); END;

-- 2026-09-25: an automatic reinstatement shown to be wrong is withdrawn by a
-- further record, the same way a retirement is.  The 2026-09-25 audit found
-- the v3 re-review reinstating on a subject named anywhere in a 1,200-char
-- digest window, or on a document head that names the subject in a list: "it
-- is too early to count META out" put back as Alphabet's.  One withdrawal per
-- reinstatement, binding its id and hash; with it the Claim is retired again
-- (``claim_retirement.reinstated_claim_version_refs`` counts only reinstatements
-- that stand).  The re-review never reinstates a decision twice.
CREATE TABLE IF NOT EXISTS claim_retirement_reinstatement_withdrawals (
    withdrawal_id TEXT PRIMARY KEY,
    claim_version_ref TEXT NOT NULL REFERENCES claim_versions(claim_version_id),
    reinstatement_ref TEXT NOT NULL UNIQUE
        REFERENCES claim_retirement_reinstatements(reinstatement_id),
    reinstatement_hash TEXT NOT NULL,
    reason_code TEXT NOT NULL CHECK(reason_code IN (
        'human_judgment',
        'subject_not_named_under_strict_rule'
    )),
    rule_ref TEXT,
    actor_ref TEXT NOT NULL,
    rationale TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claim_retirement_reinstatement_withdrawals_claim
ON claim_retirement_reinstatement_withdrawals(claim_version_ref, created_at);

CREATE TRIGGER IF NOT EXISTS claim_retirement_reinstatement_withdrawals_authorized_insert
BEFORE INSERT ON claim_retirement_reinstatement_withdrawals WHEN dalton_claim_retirement_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'claim reinstatement withdrawal insert requires ClaimRetirementAuthority'); END;
CREATE TRIGGER IF NOT EXISTS claim_retirement_reinstatement_withdrawals_no_update
BEFORE UPDATE ON claim_retirement_reinstatement_withdrawals BEGIN
    SELECT RAISE(ABORT, 'claim reinstatement withdrawals are append-only'); END;
CREATE TRIGGER IF NOT EXISTS claim_retirement_reinstatement_withdrawals_no_delete
BEFORE DELETE ON claim_retirement_reinstatement_withdrawals BEGIN
    SELECT RAISE(ABORT, 'claim reinstatement withdrawals are append-only'); END;
