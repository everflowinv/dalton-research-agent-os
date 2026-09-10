-- P14b: what a person decided about a ThesisRevisionCandidate.
--
-- ADR-0007 let automation say "the thesis this Claim supported may be weaker
-- than we said" and left the deciding to a person. This table is the other
-- half of that sentence. It is append-only for the same reason the candidate
-- is: a rejection with a reason is how the next tick knows not to re-raise
-- the proposal, and how a weekly review can ask what the machine kept
-- proposing that the analyst kept declining.
--
-- ``defer`` is not terminal. It is the honest answer to "I want to see one
-- more quarter before I move on this", and the candidate comes back. Only
-- ``accept`` and ``reject`` close a candidate, which is why the uniqueness
-- here is on the decision and not on the candidate: the chain of defers is
-- itself a record of how long a question stayed open.
CREATE TABLE IF NOT EXISTS thesis_revision_decisions (
    decision_id TEXT PRIMARY KEY,
    candidate_ref TEXT NOT NULL,
    candidate_hash TEXT NOT NULL,
    thesis_ref TEXT NOT NULL,
    thesis_version_ref TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    candidate_decision TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('accept','reject','defer')),
    reason TEXT NOT NULL,
    terminal INTEGER NOT NULL CHECK(terminal IN (0,1)),
    resulting_thesis_version_ref TEXT,
    admission_candidate_ref TEXT,
    admission_decision_ref TEXT,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS thesis_revision_decisions_by_candidate
ON thesis_revision_decisions(candidate_ref, created_at, decision_id);
CREATE INDEX IF NOT EXISTS thesis_revision_decisions_open
ON thesis_revision_decisions(terminal, candidate_ref);

CREATE TRIGGER IF NOT EXISTS thesis_revision_decisions_insert_guard
BEFORE INSERT ON thesis_revision_decisions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'thesis revision decision insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS thesis_revision_decisions_no_update
BEFORE UPDATE ON thesis_revision_decisions BEGIN
    SELECT RAISE(ABORT, 'thesis revision decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS thesis_revision_decisions_no_delete
BEFORE DELETE ON thesis_revision_decisions BEGIN
    SELECT RAISE(ABORT, 'thesis revision decisions are immutable');
END;
