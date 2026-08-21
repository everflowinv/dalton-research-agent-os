PRAGMA foreign_keys = ON;

-- A model-written interpretation of one exact formal ClaimVersion against
-- one exact current ThesisVersion.  This is an append-only pre-commit
-- judgment: it never changes the thesis pointer or creates a ThesisVersion.
CREATE TABLE IF NOT EXISTS thesis_impact_assessments (
    assessment_id TEXT PRIMARY KEY,
    claim_version_ref TEXT NOT NULL REFERENCES claim_versions(claim_version_id),
    claim_version_hash TEXT NOT NULL,
    thesis_version_ref TEXT NOT NULL REFERENCES thesis_versions(version_id),
    thesis_version_hash TEXT NOT NULL,
    impact TEXT NOT NULL CHECK(impact IN ('supports','weakens','no_change','insufficient')),
    producer_invocation_ref TEXT NOT NULL REFERENCES model_invocations(invocation_id),
    producer_result_envelope_ref TEXT NOT NULL UNIQUE,
    producer_result_envelope_hash TEXT NOT NULL,
    input_context_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Independent model verification.  A reject is kept as evidence of failure;
-- only a pass under the still-active policy is eligible for a later thesis
-- updater.  No row is mutable and no status column acts as a second authority.
CREATE TABLE IF NOT EXISTS thesis_impact_verifications (
    verification_id TEXT PRIMARY KEY,
    assessment_ref TEXT NOT NULL UNIQUE REFERENCES thesis_impact_assessments(assessment_id),
    assessment_hash TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('pass','reject')),
    verifier_invocation_ref TEXT NOT NULL REFERENCES model_invocations(invocation_id),
    verifier_result_envelope_ref TEXT NOT NULL UNIQUE,
    verifier_result_envelope_hash TEXT NOT NULL,
    policy_version_ref TEXT NOT NULL REFERENCES governance_policy_versions(policy_version_id),
    policy_version_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(assessment_ref, verifier_invocation_ref)
);

CREATE INDEX IF NOT EXISTS idx_thesis_impact_claim
ON thesis_impact_assessments(claim_version_ref, thesis_version_ref);
CREATE INDEX IF NOT EXISTS idx_thesis_impact_verification
ON thesis_impact_verifications(assessment_ref, verdict);

CREATE TRIGGER IF NOT EXISTS thesis_impact_assessments_authorized_insert
BEFORE INSERT ON thesis_impact_assessments WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'thesis impact assessment insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS thesis_impact_verifications_authorized_insert
BEFORE INSERT ON thesis_impact_verifications WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'thesis impact verification insert requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS thesis_impact_assessments_no_update
BEFORE UPDATE ON thesis_impact_assessments BEGIN
    SELECT RAISE(ABORT, 'thesis impact assessments are immutable');
END;
CREATE TRIGGER IF NOT EXISTS thesis_impact_assessments_no_delete
BEFORE DELETE ON thesis_impact_assessments BEGIN
    SELECT RAISE(ABORT, 'thesis impact assessments are immutable');
END;
CREATE TRIGGER IF NOT EXISTS thesis_impact_verifications_no_update
BEFORE UPDATE ON thesis_impact_verifications BEGIN
    SELECT RAISE(ABORT, 'thesis impact verifications are immutable');
END;
CREATE TRIGGER IF NOT EXISTS thesis_impact_verifications_no_delete
BEFORE DELETE ON thesis_impact_verifications BEGIN
    SELECT RAISE(ABORT, 'thesis impact verifications are immutable');
END;
