-- Dalton scheduler authority tables.
--
-- Every fact is append-only.  Current work/attempt/lease state is a query-time
-- projection over the latest event/revision; no mutable queue row exists.

CREATE TABLE IF NOT EXISTS scheduler_policy_versions (
    policy_version_id TEXT PRIMARY KEY,
    policy_json TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduler_work_orders (
    work_order_id TEXT PRIMARY KEY,
    work_order_json TEXT NOT NULL,
    work_order_hash TEXT NOT NULL,
    policy_version_id TEXT NOT NULL REFERENCES scheduler_policy_versions(policy_version_id),
    max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduler_enqueue_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    work_order_id TEXT NOT NULL REFERENCES scheduler_work_orders(work_order_id),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduler_leases (
    lease_revision_id TEXT PRIMARY KEY,
    lease_id TEXT NOT NULL,
    lease_version INTEGER NOT NULL CHECK (lease_version > 0),
    work_order_id TEXT NOT NULL REFERENCES scheduler_work_orders(work_order_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    owner_ref TEXT NOT NULL,
    lease_token_hash TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    renewed_at TEXT,
    expires_at TEXT NOT NULL,
    prior_lease_revision_id TEXT REFERENCES scheduler_leases(lease_revision_id),
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (lease_id, lease_version)
);

CREATE TABLE IF NOT EXISTS scheduler_attempt_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    work_order_id TEXT NOT NULL REFERENCES scheduler_work_orders(work_order_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    state TEXT NOT NULL CHECK (state IN ('ready', 'leased', 'succeeded', 'retryable', 'failed', 'expired')),
    lease_revision_id TEXT REFERENCES scheduler_leases(lease_revision_id),
    result_envelope_id TEXT,
    result_envelope_hash TEXT,
    reason TEXT,
    not_before TEXT,
    wire_version TEXT,
    prior_event_id TEXT,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (prior_event_id) REFERENCES scheduler_attempt_events(event_id)
);

CREATE INDEX IF NOT EXISTS scheduler_attempt_work_seq
ON scheduler_attempt_events(work_order_id, event_seq);

CREATE TABLE IF NOT EXISTS scheduler_formal_results (
    result_record_id TEXT PRIMARY KEY,
    work_order_id TEXT NOT NULL UNIQUE REFERENCES scheduler_work_orders(work_order_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    result_envelope_id TEXT NOT NULL,
    result_envelope_hash TEXT NOT NULL,
    result_envelope_json TEXT NOT NULL,
    terminal_state TEXT NOT NULL CHECK (terminal_state IN ('succeeded', 'failed')),
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Every accepted worker completion, including retryable outcomes, gets one
-- immutable receipt.  Envelope ids therefore cannot be reused with different
-- payloads or for a different attempt.
CREATE TABLE IF NOT EXISTS scheduler_result_envelopes (
    result_envelope_id TEXT PRIMARY KEY,
    work_order_id TEXT NOT NULL REFERENCES scheduler_work_orders(work_order_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    result_envelope_hash TEXT NOT NULL,
    result_envelope_json TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('succeeded', 'retryable', 'failed')),
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduler_completion_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- dalton_scheduler_authorized() is an integrity guardrail for a trusted
-- Scheduler connection, not a hostile same-UID process sandbox.
CREATE TRIGGER IF NOT EXISTS scheduler_policy_authorized_insert
BEFORE INSERT ON scheduler_policy_versions WHEN dalton_scheduler_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'scheduler policy insert requires Scheduler');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_work_authorized_insert
BEFORE INSERT ON scheduler_work_orders WHEN dalton_scheduler_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'scheduler work insert requires Scheduler');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_enqueue_idempotency_authorized_insert
BEFORE INSERT ON scheduler_enqueue_idempotency WHEN dalton_scheduler_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'scheduler enqueue idempotency insert requires Scheduler');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_lease_authorized_insert
BEFORE INSERT ON scheduler_leases WHEN dalton_scheduler_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'scheduler lease insert requires Scheduler');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_attempt_authorized_insert
BEFORE INSERT ON scheduler_attempt_events WHEN dalton_scheduler_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'scheduler attempt insert requires Scheduler');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_result_authorized_insert
BEFORE INSERT ON scheduler_formal_results WHEN dalton_scheduler_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'scheduler result insert requires Scheduler');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_result_envelope_authorized_insert
BEFORE INSERT ON scheduler_result_envelopes WHEN dalton_scheduler_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'scheduler result envelope insert requires Scheduler');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_completion_idempotency_authorized_insert
BEFORE INSERT ON scheduler_completion_idempotency WHEN dalton_scheduler_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'scheduler idempotency insert requires Scheduler');
END;

CREATE TRIGGER IF NOT EXISTS scheduler_policy_no_update
BEFORE UPDATE ON scheduler_policy_versions BEGIN
    SELECT RAISE(ABORT, 'scheduler policies are immutable');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_policy_no_delete
BEFORE DELETE ON scheduler_policy_versions BEGIN
    SELECT RAISE(ABORT, 'scheduler policies are immutable');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_work_no_update
BEFORE UPDATE ON scheduler_work_orders BEGIN
    SELECT RAISE(ABORT, 'scheduler work orders are immutable');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_work_no_delete
BEFORE DELETE ON scheduler_work_orders BEGIN
    SELECT RAISE(ABORT, 'scheduler work orders are immutable');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_enqueue_idempotency_no_update
BEFORE UPDATE ON scheduler_enqueue_idempotency BEGIN
    SELECT RAISE(ABORT, 'scheduler enqueue idempotency is immutable');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_enqueue_idempotency_no_delete
BEFORE DELETE ON scheduler_enqueue_idempotency BEGIN
    SELECT RAISE(ABORT, 'scheduler enqueue idempotency is immutable');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_lease_no_update
BEFORE UPDATE ON scheduler_leases BEGIN
    SELECT RAISE(ABORT, 'scheduler leases are append-only');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_lease_no_delete
BEFORE DELETE ON scheduler_leases BEGIN
    SELECT RAISE(ABORT, 'scheduler leases are append-only');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_attempt_no_update
BEFORE UPDATE ON scheduler_attempt_events BEGIN
    SELECT RAISE(ABORT, 'scheduler attempts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_attempt_no_delete
BEFORE DELETE ON scheduler_attempt_events BEGIN
    SELECT RAISE(ABORT, 'scheduler attempts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_result_no_update
BEFORE UPDATE ON scheduler_formal_results BEGIN
    SELECT RAISE(ABORT, 'scheduler results are immutable');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_result_no_delete
BEFORE DELETE ON scheduler_formal_results BEGIN
    SELECT RAISE(ABORT, 'scheduler results are immutable');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_result_envelope_no_update
BEFORE UPDATE ON scheduler_result_envelopes BEGIN
    SELECT RAISE(ABORT, 'scheduler result envelopes are immutable');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_result_envelope_no_delete
BEFORE DELETE ON scheduler_result_envelopes BEGIN
    SELECT RAISE(ABORT, 'scheduler result envelopes are immutable');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_completion_idempotency_no_update
BEFORE UPDATE ON scheduler_completion_idempotency BEGIN
    SELECT RAISE(ABORT, 'scheduler idempotency keys are immutable');
END;
CREATE TRIGGER IF NOT EXISTS scheduler_completion_idempotency_no_delete
BEFORE DELETE ON scheduler_completion_idempotency BEGIN
    SELECT RAISE(ABORT, 'scheduler idempotency keys are immutable');
END;
