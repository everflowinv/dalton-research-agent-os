-- Capability registry tables.  This schema is installed on the same
-- connection as DaltonStore, but has its own append-only authority tables.
-- The writer service, rather than an executor, owns all writes.

CREATE TABLE IF NOT EXISTS capability_proposal_versions (
    revision_id TEXT PRIMARY KEY,
    capability_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    proposal_json TEXT NOT NULL,
    proposal_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    prior_revision_id TEXT REFERENCES capability_proposal_versions(revision_id),
    builder_invocation_id TEXT,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (capability_ref, version_number)
);

CREATE TABLE IF NOT EXISTS capability_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES capability_proposal_versions(revision_id),
    capability_ref TEXT NOT NULL,
    proposal_content_hash TEXT NOT NULL,
    fixtures_json TEXT NOT NULL,
    baseline_json TEXT NOT NULL,
    results_json TEXT NOT NULL,
    environment_hash TEXT NOT NULL,
    evaluator_invocation_id TEXT NOT NULL,
    builder_invocation_id TEXT,
    policy_version_id TEXT NOT NULL,
    policy_content_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capability_decisions (
    decision_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES capability_proposal_versions(revision_id),
    evaluation_id TEXT REFERENCES capability_evaluations(evaluation_id),
    capability_ref TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject', 'rollback')),
    actor_ref TEXT NOT NULL,
    requested_permissions_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    rollback_to_revision_id TEXT REFERENCES capability_proposal_versions(revision_id),
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- A pointer is a history log, not an UPDATE-able singleton.  The current
-- pointer is the last row for a capability_ref.
CREATE TABLE IF NOT EXISTS capability_registry_pointers (
    pointer_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    capability_ref TEXT NOT NULL,
    revision_id TEXT NOT NULL REFERENCES capability_proposal_versions(revision_id),
    action TEXT NOT NULL CHECK (action IN ('active', 'rollback')),
    decision_id TEXT NOT NULL REFERENCES capability_decisions(decision_id),
    prior_pointer_seq INTEGER REFERENCES capability_registry_pointers(pointer_seq),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capability_idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- The service authorization UDF is only an integrity guardrail for ordinary
-- callers of this trusted connection; it is not a same-user security sandbox.
CREATE TRIGGER IF NOT EXISTS capability_proposal_versions_authorized_insert
BEFORE INSERT ON capability_proposal_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'capability proposal insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS capability_evaluations_authorized_insert
BEFORE INSERT ON capability_evaluations WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'capability evaluation insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS capability_decisions_authorized_insert
BEFORE INSERT ON capability_decisions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'capability decision insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS capability_registry_pointers_authorized_insert
BEFORE INSERT ON capability_registry_pointers WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'capability pointer insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS capability_idempotency_keys_authorized_insert
BEFORE INSERT ON capability_idempotency_keys WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'capability idempotency insert requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS capability_proposal_versions_no_update
BEFORE UPDATE ON capability_proposal_versions BEGIN
    SELECT RAISE(ABORT, 'capability proposal versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS capability_proposal_versions_no_delete
BEFORE DELETE ON capability_proposal_versions BEGIN
    SELECT RAISE(ABORT, 'capability proposal versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS capability_evaluations_no_update
BEFORE UPDATE ON capability_evaluations BEGIN
    SELECT RAISE(ABORT, 'capability evaluations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS capability_evaluations_no_delete
BEFORE DELETE ON capability_evaluations BEGIN
    SELECT RAISE(ABORT, 'capability evaluations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS capability_decisions_no_update
BEFORE UPDATE ON capability_decisions BEGIN
    SELECT RAISE(ABORT, 'capability decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS capability_decisions_no_delete
BEFORE DELETE ON capability_decisions BEGIN
    SELECT RAISE(ABORT, 'capability decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS capability_registry_pointers_no_update
BEFORE UPDATE ON capability_registry_pointers BEGIN
    SELECT RAISE(ABORT, 'capability registry pointers are append-only');
END;
CREATE TRIGGER IF NOT EXISTS capability_registry_pointers_no_delete
BEFORE DELETE ON capability_registry_pointers BEGIN
    SELECT RAISE(ABORT, 'capability registry pointers are append-only');
END;
CREATE TRIGGER IF NOT EXISTS capability_idempotency_keys_no_update
BEFORE UPDATE ON capability_idempotency_keys BEGIN
    SELECT RAISE(ABORT, 'capability idempotency keys are immutable');
END;
CREATE TRIGGER IF NOT EXISTS capability_idempotency_keys_no_delete
BEFORE DELETE ON capability_idempotency_keys BEGIN
    SELECT RAISE(ABORT, 'capability idempotency keys are immutable');
END;
