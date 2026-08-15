PRAGMA foreign_keys = ON;

-- ResearchQuestionBacklog append-only authority.
--
-- ``backlog_questions`` holds the canonical identity binding for one logical
-- research question.  The question_ref is derived deterministically from the
-- binding (mandate_ref + company scope + question text); callers can never
-- supply a question_ref, version id, or content hash.
--
-- ``backlog_question_versions`` are immutable content snapshots for a
-- question.  This slice only ever writes version 1 (identical content is
-- deduplicated, divergent content for the same identity fails closed);
-- the version chain exists so a future revision slice can append revisions
-- without an authority migration.
--
-- ``backlog_question_events`` is the state machine: every legal transition
-- appends one immutable event row; the head state is the latest event.
-- Transitions are validated in the same Core transaction that appends the
-- event, so illegal or out-of-order transitions fail closed with no residue.
--
-- ``backlog_selection_links`` binds a selected question to the exact
-- AgendaDecision/AgendaCycle that selected it (decision hash, cycle hash and
-- the matching candidate ref are all re-derived from Core authority).
--
-- ``backlog_answer_bindings`` binds an answered question to one or more
-- exact formal ClaimVersion refs/hashes re-read from the Core Ledger.  An
-- AgendaDecision is never an answer; only an answered event with verified
-- formal ClaimVersion bindings may move a question to answered.
--
-- ``backlog_idempotency`` mirrors the agenda idempotency convention so a
-- replayed request returns the original result instead of double-writing.

CREATE TABLE IF NOT EXISTS backlog_questions (
    question_ref TEXT PRIMARY KEY,
    identity_json TEXT NOT NULL,
    identity_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backlog_question_versions (
    version_id TEXT PRIMARY KEY,
    question_ref TEXT NOT NULL REFERENCES backlog_questions(question_ref),
    version_number INTEGER NOT NULL CHECK(version_number > 0),
    prior_version_id TEXT REFERENCES backlog_question_versions(version_id),
    mandate_ref TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    question TEXT NOT NULL,
    answer_criteria TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(question_ref, version_number)
);

CREATE TABLE IF NOT EXISTS backlog_question_pointer (
    question_ref TEXT PRIMARY KEY REFERENCES backlog_questions(question_ref),
    version_id TEXT NOT NULL REFERENCES backlog_question_versions(version_id)
);

CREATE TABLE IF NOT EXISTS backlog_question_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    question_ref TEXT NOT NULL REFERENCES backlog_questions(question_ref),
    state TEXT NOT NULL CHECK(state IN ('open','selected','planned','in_progress','answered','blocked','retired')),
    reason TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backlog_selection_links (
    link_id TEXT PRIMARY KEY,
    question_ref TEXT NOT NULL REFERENCES backlog_questions(question_ref),
    decision_ref TEXT NOT NULL,
    decision_hash TEXT NOT NULL,
    cycle_ref TEXT NOT NULL,
    cycle_hash TEXT NOT NULL,
    candidate_ref TEXT NOT NULL,
    event_ref TEXT NOT NULL REFERENCES backlog_question_events(event_id),
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backlog_answer_bindings (
    binding_id TEXT PRIMARY KEY,
    question_ref TEXT NOT NULL REFERENCES backlog_questions(question_ref),
    claim_version_ref TEXT NOT NULL,
    claim_version_hash TEXT NOT NULL,
    claim_ref TEXT NOT NULL,
    event_ref TEXT NOT NULL REFERENCES backlog_question_events(event_id),
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backlog_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_backlog_question_events_question
ON backlog_question_events(question_ref, event_seq);
CREATE INDEX IF NOT EXISTS idx_backlog_versions_question
ON backlog_question_versions(question_ref, version_number);
CREATE INDEX IF NOT EXISTS idx_backlog_answer_claims
ON backlog_answer_bindings(claim_version_ref, question_ref);
CREATE INDEX IF NOT EXISTS idx_backlog_selection_decisions
ON backlog_selection_links(decision_ref, question_ref);

CREATE TRIGGER IF NOT EXISTS backlog_questions_authorized_insert
BEFORE INSERT ON backlog_questions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'backlog question insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS backlog_question_versions_authorized_insert
BEFORE INSERT ON backlog_question_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'backlog question version insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS backlog_question_pointer_authorized_insert
BEFORE INSERT ON backlog_question_pointer WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'backlog question pointer insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS backlog_question_pointer_authorized_update
BEFORE UPDATE ON backlog_question_pointer WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'backlog question pointer update requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS backlog_question_events_authorized_insert
BEFORE INSERT ON backlog_question_events WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'backlog question event insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS backlog_selection_links_authorized_insert
BEFORE INSERT ON backlog_selection_links WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'backlog selection link insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS backlog_answer_bindings_authorized_insert
BEFORE INSERT ON backlog_answer_bindings WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'backlog answer binding insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS backlog_idempotency_authorized_insert
BEFORE INSERT ON backlog_idempotency WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'backlog idempotency insert requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS backlog_questions_no_update
BEFORE UPDATE ON backlog_questions BEGIN SELECT RAISE(ABORT, 'backlog questions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS backlog_question_versions_no_update
BEFORE UPDATE ON backlog_question_versions BEGIN SELECT RAISE(ABORT, 'backlog question versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS backlog_question_events_no_update
BEFORE UPDATE ON backlog_question_events BEGIN SELECT RAISE(ABORT, 'backlog question events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS backlog_selection_links_no_update
BEFORE UPDATE ON backlog_selection_links BEGIN SELECT RAISE(ABORT, 'backlog selection links are immutable'); END;
CREATE TRIGGER IF NOT EXISTS backlog_answer_bindings_no_update
BEFORE UPDATE ON backlog_answer_bindings BEGIN SELECT RAISE(ABORT, 'backlog answer bindings are immutable'); END;
CREATE TRIGGER IF NOT EXISTS backlog_idempotency_no_update
BEFORE UPDATE ON backlog_idempotency BEGIN SELECT RAISE(ABORT, 'backlog idempotency rows are immutable'); END;

CREATE TRIGGER IF NOT EXISTS backlog_questions_no_delete
BEFORE DELETE ON backlog_questions BEGIN SELECT RAISE(ABORT, 'backlog questions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS backlog_question_versions_no_delete
BEFORE DELETE ON backlog_question_versions BEGIN SELECT RAISE(ABORT, 'backlog question versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS backlog_question_events_no_delete
BEFORE DELETE ON backlog_question_events BEGIN SELECT RAISE(ABORT, 'backlog question events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS backlog_selection_links_no_delete
BEFORE DELETE ON backlog_selection_links BEGIN SELECT RAISE(ABORT, 'backlog selection links are immutable'); END;
CREATE TRIGGER IF NOT EXISTS backlog_answer_bindings_no_delete
BEFORE DELETE ON backlog_answer_bindings BEGIN SELECT RAISE(ABORT, 'backlog answer bindings are immutable'); END;
CREATE TRIGGER IF NOT EXISTS backlog_idempotency_no_delete
BEFORE DELETE ON backlog_idempotency BEGIN SELECT RAISE(ABORT, 'backlog idempotency rows are immutable'); END;

CREATE TRIGGER IF NOT EXISTS backlog_question_pointer_authorized_delete
BEFORE DELETE ON backlog_question_pointer BEGIN SELECT RAISE(ABORT, 'backlog question pointer cannot be deleted'); END;
