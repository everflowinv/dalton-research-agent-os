PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS model_input_candidates (
    candidate_id TEXT PRIMARY KEY,
    input_kind TEXT NOT NULL CHECK(input_kind IN ('actual','assumption','forecast_line','scenario')),
    model_input_ref TEXT NOT NULL,
    prior_version_id TEXT REFERENCES model_input_versions(version_id),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_input_decisions (
    decision_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE REFERENCES model_input_candidates(candidate_id),
    candidate_hash TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('admit','reject')),
    rationale TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    reviewer_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_input_versions (
    version_id TEXT PRIMARY KEY,
    model_input_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES model_input_versions(version_id),
    input_kind TEXT NOT NULL CHECK(input_kind IN ('actual','assumption','forecast_line','scenario')),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    admission_decision_id TEXT NOT NULL UNIQUE REFERENCES model_input_decisions(decision_id),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(model_input_ref, version_number)
);

CREATE TABLE IF NOT EXISTS model_input_pointer (
    model_input_ref TEXT PRIMARY KEY,
    version_id TEXT NOT NULL UNIQUE REFERENCES model_input_versions(version_id),
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_run_versions (
    version_id TEXT PRIMARY KEY,
    model_run_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES model_run_versions(version_id),
    scenario_version_ref TEXT NOT NULL REFERENCES model_input_versions(version_id),
    scenario_version_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('completed','failed')),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(model_run_ref, version_number)
);

CREATE TABLE IF NOT EXISTS model_run_pointer (
    model_run_ref TEXT PRIMARY KEY,
    version_id TEXT NOT NULL UNIQUE REFERENCES model_run_versions(version_id),
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_reconciliations (
    reconciliation_id TEXT PRIMARY KEY,
    model_run_version_ref TEXT NOT NULL REFERENCES model_run_versions(version_id),
    model_run_version_hash TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('pass','fail')),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_model_input_candidate_identity
ON model_input_candidates(model_input_ref, candidate_id);

CREATE INDEX IF NOT EXISTS idx_model_input_version_history
ON model_input_versions(model_input_ref, version_number);

CREATE INDEX IF NOT EXISTS idx_model_run_version_history
ON model_run_versions(model_run_ref, version_number);

CREATE INDEX IF NOT EXISTS idx_model_reconciliation_run
ON model_reconciliations(model_run_version_ref, created_at, reconciliation_id);

CREATE TABLE IF NOT EXISTS model_input_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'candidate_staged','candidate_decided','input_committed',
        'model_run_committed','reconciliation_committed'
    )),
    aggregate_ref TEXT NOT NULL,
    aggregate_version_ref TEXT NOT NULL,
    aggregate_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_input_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS model_input_candidates_authorized_insert
BEFORE INSERT ON model_input_candidates WHEN dalton_model_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'model input candidate insert requires ModelInputLedger'); END;
CREATE TRIGGER IF NOT EXISTS model_input_decisions_authorized_insert
BEFORE INSERT ON model_input_decisions WHEN dalton_model_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'model input decision insert requires ModelInputLedger'); END;
CREATE TRIGGER IF NOT EXISTS model_input_versions_authorized_insert
BEFORE INSERT ON model_input_versions WHEN dalton_model_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'model input version insert requires ModelInputLedger'); END;
CREATE TRIGGER IF NOT EXISTS model_input_pointer_authorized_insert
BEFORE INSERT ON model_input_pointer WHEN dalton_model_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'model input pointer insert requires ModelInputLedger'); END;
CREATE TRIGGER IF NOT EXISTS model_input_pointer_authorized_update
BEFORE UPDATE ON model_input_pointer WHEN dalton_model_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'model input pointer update requires ModelInputLedger'); END;
CREATE TRIGGER IF NOT EXISTS model_run_versions_authorized_insert
BEFORE INSERT ON model_run_versions WHEN dalton_model_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'model run version insert requires ModelInputLedger'); END;
CREATE TRIGGER IF NOT EXISTS model_run_pointer_authorized_insert
BEFORE INSERT ON model_run_pointer WHEN dalton_model_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'model run pointer insert requires ModelInputLedger'); END;
CREATE TRIGGER IF NOT EXISTS model_run_pointer_authorized_update
BEFORE UPDATE ON model_run_pointer WHEN dalton_model_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'model run pointer update requires ModelInputLedger'); END;
CREATE TRIGGER IF NOT EXISTS model_reconciliations_authorized_insert
BEFORE INSERT ON model_reconciliations WHEN dalton_model_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'model reconciliation insert requires ModelInputLedger'); END;
CREATE TRIGGER IF NOT EXISTS model_input_events_authorized_insert
BEFORE INSERT ON model_input_events WHEN dalton_model_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'model input event insert requires ModelInputLedger'); END;
CREATE TRIGGER IF NOT EXISTS model_input_idempotency_authorized_insert
BEFORE INSERT ON model_input_idempotency WHEN dalton_model_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'model input idempotency insert requires ModelInputLedger'); END;

CREATE TRIGGER IF NOT EXISTS model_input_candidates_no_update
BEFORE UPDATE ON model_input_candidates BEGIN SELECT RAISE(ABORT, 'model input candidates are immutable'); END;
CREATE TRIGGER IF NOT EXISTS model_input_candidates_no_delete
BEFORE DELETE ON model_input_candidates BEGIN SELECT RAISE(ABORT, 'model input candidates are immutable'); END;
CREATE TRIGGER IF NOT EXISTS model_input_decisions_no_update
BEFORE UPDATE ON model_input_decisions BEGIN SELECT RAISE(ABORT, 'model input decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS model_input_decisions_no_delete
BEFORE DELETE ON model_input_decisions BEGIN SELECT RAISE(ABORT, 'model input decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS model_input_versions_no_update
BEFORE UPDATE ON model_input_versions BEGIN SELECT RAISE(ABORT, 'model input versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS model_input_versions_no_delete
BEFORE DELETE ON model_input_versions BEGIN SELECT RAISE(ABORT, 'model input versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS model_input_pointer_no_delete
BEFORE DELETE ON model_input_pointer BEGIN SELECT RAISE(ABORT, 'model input pointer cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS model_run_versions_no_update
BEFORE UPDATE ON model_run_versions BEGIN SELECT RAISE(ABORT, 'model run versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS model_run_versions_no_delete
BEFORE DELETE ON model_run_versions BEGIN SELECT RAISE(ABORT, 'model run versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS model_run_pointer_no_delete
BEFORE DELETE ON model_run_pointer BEGIN SELECT RAISE(ABORT, 'model run pointer cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS model_reconciliations_no_update
BEFORE UPDATE ON model_reconciliations BEGIN SELECT RAISE(ABORT, 'model reconciliations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS model_reconciliations_no_delete
BEFORE DELETE ON model_reconciliations BEGIN SELECT RAISE(ABORT, 'model reconciliations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS model_input_events_no_update
BEFORE UPDATE ON model_input_events BEGIN SELECT RAISE(ABORT, 'model input events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS model_input_events_no_delete
BEFORE DELETE ON model_input_events BEGIN SELECT RAISE(ABORT, 'model input events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS model_input_idempotency_no_update
BEFORE UPDATE ON model_input_idempotency BEGIN SELECT RAISE(ABORT, 'model input idempotency is immutable'); END;
CREATE TRIGGER IF NOT EXISTS model_input_idempotency_no_delete
BEFORE DELETE ON model_input_idempotency BEGIN SELECT RAISE(ABORT, 'model input idempotency is immutable'); END;
