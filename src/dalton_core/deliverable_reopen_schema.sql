-- P14d: the proposal that a passed gate should be re-opened, and the answer.
--
-- ADR-0008 says no output has a terminal status: ``gate_passed`` is the state
-- of a *version*, not of the company. The mechanism half of that is here, and
-- it is deliberately only a proposal. Nothing in this schema can move a gate;
-- an approved row is a permission the Initial Screen lane reads, and the lane
-- then writes a *new* version with its own ``prior_version_ref``. The old
-- version and its ``gate_passed`` stage record are never touched, because the
-- chain is what the owner reads to watch the understanding iterate.
--
-- Idempotency is on (company, assessment_hash): the same evidence base
-- proposes once, however many times the weekly lane looks at it. Evidence that
-- thickened again is a different hash and a second proposal.
CREATE TABLE IF NOT EXISTS gate_reopen_proposals (
    proposal_id TEXT PRIMARY KEY,
    company_ref TEXT NOT NULL,
    deliverable_ref TEXT NOT NULL,
    stage_ref TEXT NOT NULL,
    passed_version_ref TEXT NOT NULL,
    passed_version_hash TEXT NOT NULL,
    assessment_hash TEXT NOT NULL,
    flipped_count INTEGER NOT NULL CHECK(flipped_count >= 1),
    checkpoint_kind TEXT NOT NULL CHECK(checkpoint_kind = 'gate_reopen'),
    change_reason TEXT NOT NULL,
    mission_version_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(company_ref, assessment_hash)
);

CREATE INDEX IF NOT EXISTS gate_reopen_proposals_by_company
ON gate_reopen_proposals(company_ref, created_at DESC, proposal_id);

CREATE TABLE IF NOT EXISTS gate_reopen_decisions (
    decision_id TEXT PRIMARY KEY,
    proposal_ref TEXT NOT NULL UNIQUE REFERENCES gate_reopen_proposals(proposal_id),
    proposal_hash TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('approve','decline')),
    reason TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS gate_reopen_proposals_insert_guard
BEFORE INSERT ON gate_reopen_proposals WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'gate reopen proposal insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS gate_reopen_proposals_no_update
BEFORE UPDATE ON gate_reopen_proposals BEGIN
    SELECT RAISE(ABORT, 'gate reopen proposals are immutable');
END;
CREATE TRIGGER IF NOT EXISTS gate_reopen_proposals_no_delete
BEFORE DELETE ON gate_reopen_proposals BEGIN
    SELECT RAISE(ABORT, 'gate reopen proposals are immutable');
END;

CREATE TRIGGER IF NOT EXISTS gate_reopen_decisions_insert_guard
BEFORE INSERT ON gate_reopen_decisions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'gate reopen decision insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS gate_reopen_decisions_no_update
BEFORE UPDATE ON gate_reopen_decisions BEGIN
    SELECT RAISE(ABORT, 'gate reopen decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS gate_reopen_decisions_no_delete
BEFORE DELETE ON gate_reopen_decisions BEGIN
    SELECT RAISE(ABORT, 'gate reopen decisions are immutable');
END;
