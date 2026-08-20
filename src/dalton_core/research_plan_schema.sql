PRAGMA foreign_keys = ON;

-- ResearchPlanVersion append-only authority ("Planner thin closure").
--
-- ``research_plan_versions`` holds one immutable plan per exact selected
-- research-question version + exact AgendaDecision.  The plan ref is derived
-- deterministically from the canonical binding (planner + question version +
-- agenda decision + frozen SEC request); callers can never supply a plan ref,
-- version id, or content hash.  Version 1 is closed to a single SEC public,
-- credential-free, read-only ``list_filings`` plan; every read re-derives the
-- closed execution scope from the packaged SEC connector template and from
-- frozen constants, so caller-injected connectors, credentials, writes,
-- broadened permissions or extra steps fail closed.
--
-- ``research_plan_events`` is the append-only plan state machine.  This slice
-- only ever writes ``created`` (plan version recorded) and ``started``
-- (approved workflow/root WorkOrder bound).  Illegal or out-of-order
-- transitions fail closed inside the same Core transaction that appends the
-- event row.
--
-- ``research_plan_approvals`` is the exact human approval authority.  Each
-- exact plan version may instead bind one separate versioned-policy
-- authorization for the closed low-risk SEC scope.  Automation/model
-- principals, Agenda approval and Discord reactions cannot write the human
-- table or impersonate a person.
--
-- ``research_plan_starts`` binds a started plan to the exact
-- WorkflowRunVersion row and root WorkOrder created by the plan control
-- plane: workflow version ref/hash, root work order ref/hash and the exact
-- accepted human-or-policy authorization ref/hash are all frozen and
-- re-validated on every read.
--
-- ``research_plan_idempotency`` mirrors the agenda/backlog idempotency
-- convention: a replayed request returns the original result; the same key
-- with a different request fails closed as a conflict.

CREATE TABLE IF NOT EXISTS research_plan_versions (
    version_id TEXT PRIMARY KEY,
    question_ref TEXT NOT NULL,
    question_version_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number > 0),
    prior_version_id TEXT REFERENCES research_plan_versions(version_id),
    decision_ref TEXT NOT NULL,
    cycle_ref TEXT NOT NULL,
    planner_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(question_ref, version_number)
);

CREATE TABLE IF NOT EXISTS research_plan_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    plan_version_ref TEXT NOT NULL REFERENCES research_plan_versions(version_id),
    state TEXT NOT NULL CHECK(state IN ('created','started')),
    reason TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_plan_approvals (
    approval_id TEXT PRIMARY KEY,
    plan_version_ref TEXT NOT NULL UNIQUE REFERENCES research_plan_versions(version_id),
    plan_version_hash TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('accepted','rejected')),
    reason TEXT NOT NULL,
    actor_ref TEXT NOT NULL CHECK(actor_ref LIKE 'human:%'),
    created_at TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

-- Low-risk plans may instead receive one deterministic authorization from
-- the active versioned governance policy.  This authority is deliberately
-- separate from human approval so automation cannot impersonate a person.
CREATE TABLE IF NOT EXISTS research_plan_policy_authorizations (
    authorization_id TEXT PRIMARY KEY,
    plan_version_ref TEXT NOT NULL UNIQUE REFERENCES research_plan_versions(version_id),
    plan_version_hash TEXT NOT NULL,
    policy_version_ref TEXT NOT NULL REFERENCES governance_policy_versions(policy_version_id),
    policy_version_hash TEXT NOT NULL,
    rule_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_plan_starts (
    start_id TEXT PRIMARY KEY,
    plan_version_ref TEXT NOT NULL UNIQUE REFERENCES research_plan_versions(version_id),
    plan_version_hash TEXT NOT NULL,
    approval_ref TEXT NOT NULL,
    approval_hash TEXT NOT NULL,
    workflow_ref TEXT NOT NULL,
    workflow_version_ref TEXT NOT NULL,
    workflow_version_hash TEXT NOT NULL,
    root_work_order_ref TEXT NOT NULL,
    root_work_order_hash TEXT NOT NULL,
    event_ref TEXT NOT NULL REFERENCES research_plan_events(event_id),
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_plan_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_plan_events_plan
ON research_plan_events(plan_version_ref, event_seq);
CREATE INDEX IF NOT EXISTS idx_research_plan_versions_question
ON research_plan_versions(question_ref, version_number);

CREATE TRIGGER IF NOT EXISTS research_plan_versions_authorized_insert
BEFORE INSERT ON research_plan_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research plan version insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS research_plan_events_authorized_insert
BEFORE INSERT ON research_plan_events WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research plan event insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS research_plan_approvals_authorized_insert
BEFORE INSERT ON research_plan_approvals WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research plan approval insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS research_plan_policy_authorizations_authorized_insert
BEFORE INSERT ON research_plan_policy_authorizations WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research plan policy authorization insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS research_plan_starts_authorized_insert
BEFORE INSERT ON research_plan_starts WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research plan start insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS research_plan_idempotency_authorized_insert
BEFORE INSERT ON research_plan_idempotency WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research plan idempotency insert requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS research_plan_versions_no_update
BEFORE UPDATE ON research_plan_versions BEGIN SELECT RAISE(ABORT, 'research plan versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_plan_events_no_update
BEFORE UPDATE ON research_plan_events BEGIN SELECT RAISE(ABORT, 'research plan events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_plan_approvals_no_update
BEFORE UPDATE ON research_plan_approvals BEGIN SELECT RAISE(ABORT, 'research plan approvals are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_plan_policy_authorizations_no_update
BEFORE UPDATE ON research_plan_policy_authorizations BEGIN SELECT RAISE(ABORT, 'research plan policy authorizations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_plan_starts_no_update
BEFORE UPDATE ON research_plan_starts BEGIN SELECT RAISE(ABORT, 'research plan starts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_plan_idempotency_no_update
BEFORE UPDATE ON research_plan_idempotency BEGIN SELECT RAISE(ABORT, 'research plan idempotency rows are immutable'); END;

CREATE TRIGGER IF NOT EXISTS research_plan_versions_no_delete
BEFORE DELETE ON research_plan_versions BEGIN SELECT RAISE(ABORT, 'research plan versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_plan_events_no_delete
BEFORE DELETE ON research_plan_events BEGIN SELECT RAISE(ABORT, 'research plan events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_plan_approvals_no_delete
BEFORE DELETE ON research_plan_approvals BEGIN SELECT RAISE(ABORT, 'research plan approvals are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_plan_policy_authorizations_no_delete
BEFORE DELETE ON research_plan_policy_authorizations BEGIN SELECT RAISE(ABORT, 'research plan policy authorizations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_plan_starts_no_delete
BEFORE DELETE ON research_plan_starts BEGIN SELECT RAISE(ABORT, 'research plan starts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_plan_idempotency_no_delete
BEFORE DELETE ON research_plan_idempotency BEGIN SELECT RAISE(ABORT, 'research plan idempotency rows are immutable'); END;
