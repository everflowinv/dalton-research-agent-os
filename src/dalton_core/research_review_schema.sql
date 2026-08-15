PRAGMA foreign_keys = ON;

-- Human review is append-only and lives beside candidate staging, not in the
-- formal Research Ledger.  One candidate version receives exactly one
-- terminal decision.  A revised candidate must be staged as a new version.
CREATE TABLE IF NOT EXISTS human_review_decisions (
    decision_id TEXT PRIMARY KEY,
    candidate_claim_version_ref TEXT NOT NULL UNIQUE
        REFERENCES candidate_claim_versions(version_id),
    candidate_evidence_version_ref TEXT NOT NULL
        REFERENCES candidate_evidence_versions(version_id),
    verdict TEXT NOT NULL CHECK(verdict IN ('accept','revise','reject')),
    reviewer_ref TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS human_review_requests (
    idempotency_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Accepted reviews create a durable local delivery intent.  Delivery state is
-- another append-only chain so a crash between review and Core commit can be
-- reconciled without rewriting the human decision.
CREATE TABLE IF NOT EXISTS human_review_commit_events (
    event_id TEXT PRIMARY KEY,
    decision_ref TEXT NOT NULL REFERENCES human_review_decisions(decision_id),
    state TEXT NOT NULL CHECK(state IN ('queued','committed','failed')),
    prior_event_ref TEXT REFERENCES human_review_commit_events(event_id),
    ledger_result_json TEXT,
    error_code TEXT,
    event_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_human_review_commit_events_decision
ON human_review_commit_events(decision_ref, created_at, event_id);

CREATE TRIGGER IF NOT EXISTS human_review_decisions_no_update
BEFORE UPDATE ON human_review_decisions BEGIN SELECT RAISE(ABORT, 'human_review_decisions is immutable'); END;
CREATE TRIGGER IF NOT EXISTS human_review_decisions_no_delete
BEFORE DELETE ON human_review_decisions BEGIN SELECT RAISE(ABORT, 'human_review_decisions is immutable'); END;
CREATE TRIGGER IF NOT EXISTS human_review_requests_no_update
BEFORE UPDATE ON human_review_requests BEGIN SELECT RAISE(ABORT, 'human_review_requests is immutable'); END;
CREATE TRIGGER IF NOT EXISTS human_review_requests_no_delete
BEFORE DELETE ON human_review_requests BEGIN SELECT RAISE(ABORT, 'human_review_requests is immutable'); END;
CREATE TRIGGER IF NOT EXISTS human_review_commit_events_no_update
BEFORE UPDATE ON human_review_commit_events BEGIN SELECT RAISE(ABORT, 'human_review_commit_events is immutable'); END;
CREATE TRIGGER IF NOT EXISTS human_review_commit_events_no_delete
BEFORE DELETE ON human_review_commit_events BEGIN SELECT RAISE(ABORT, 'human_review_commit_events is immutable'); END;
