PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS agenda_control_versions (
    version_id TEXT PRIMARY KEY,
    version_number INTEGER NOT NULL CHECK(version_number > 0),
    prior_version_id TEXT REFERENCES agenda_control_versions(version_id),
    paused INTEGER NOT NULL CHECK(paused IN (0, 1)),
    reason TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agenda_control_pointer (
    pointer_id INTEGER PRIMARY KEY CHECK(pointer_id = 1),
    version_id TEXT NOT NULL REFERENCES agenda_control_versions(version_id)
);

CREATE TABLE IF NOT EXISTS agenda_policy_versions (
    version_id TEXT PRIMARY KEY,
    version_number INTEGER NOT NULL CHECK(version_number > 0),
    prior_version_id TEXT REFERENCES agenda_policy_versions(version_id),
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    effective_from TEXT NOT NULL,
    effective_until TEXT,
    policy_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agenda_policy_pointer (
    pointer_id INTEGER PRIMARY KEY CHECK(pointer_id = 1),
    version_id TEXT NOT NULL REFERENCES agenda_policy_versions(version_id)
);

CREATE TABLE IF NOT EXISTS mandate_versions (
    version_id TEXT PRIMARY KEY,
    mandate_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number > 0),
    prior_version_id TEXT REFERENCES mandate_versions(version_id),
    objective TEXT NOT NULL,
    scope_refs_json TEXT NOT NULL,
    constraints_json TEXT NOT NULL,
    success_criteria_json TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_until TEXT,
    actor_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(mandate_ref, version_number)
);

CREATE TABLE IF NOT EXISTS mandate_pointer (
    mandate_ref TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES mandate_versions(version_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS priority_override_versions (
    version_id TEXT PRIMARY KEY,
    override_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number > 0),
    prior_version_id TEXT REFERENCES priority_override_versions(version_id),
    scope_refs_json TEXT NOT NULL,
    weight_deltas_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_until TEXT NOT NULL,
    revoked INTEGER NOT NULL CHECK(revoked IN (0, 1)),
    actor_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(override_ref, version_number)
);

CREATE TABLE IF NOT EXISTS priority_override_pointer (
    override_ref TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES priority_override_versions(version_id)
);

CREATE TABLE IF NOT EXISTS research_question_versions (
    version_id TEXT PRIMARY KEY,
    question_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number > 0),
    prior_version_id TEXT REFERENCES research_question_versions(version_id),
    company_ref TEXT NOT NULL,
    question TEXT NOT NULL,
    answer_criteria TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open','selected','deferred','dormant')),
    defer_until TEXT,
    wake_condition TEXT,
    source_refs_json TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(question_ref, version_number)
);

CREATE TABLE IF NOT EXISTS research_question_pointer (
    question_ref TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES research_question_versions(version_id)
);

CREATE TABLE IF NOT EXISTS perception_snapshot_versions (
    snapshot_id TEXT PRIMARY KEY,
    company_ref TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_snapshot_hash TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agenda_cycles (
    cycle_id TEXT PRIMARY KEY,
    cycle_key TEXT NOT NULL UNIQUE,
    perception_snapshot_ref TEXT NOT NULL,
    perception_snapshot_hash TEXT NOT NULL,
    mandate_version_ref TEXT NOT NULL REFERENCES mandate_versions(version_id),
    mandate_version_hash TEXT,
    policy_version_ref TEXT NOT NULL REFERENCES agenda_policy_versions(version_id),
    policy_version_hash TEXT,
    company_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agenda_cycle_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    cycle_id TEXT NOT NULL REFERENCES agenda_cycles(cycle_id),
    state TEXT NOT NULL CHECK(state IN ('collecting','candidates_ready','decided','delivered','failed')),
    reason TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agenda_candidates (
    candidate_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL REFERENCES agenda_cycles(cycle_id),
    question_version_ref TEXT REFERENCES research_question_versions(version_id),
    proposed_question TEXT NOT NULL,
    answer_criteria TEXT NOT NULL,
    features_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    valid INTEGER NOT NULL CHECK(valid IN (0, 1)),
    rejection_reason TEXT,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(cycle_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS agenda_decisions (
    decision_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL UNIQUE REFERENCES agenda_cycles(cycle_id),
    selected_candidate_refs_json TEXT NOT NULL,
    deferred_candidate_refs_json TEXT NOT NULL,
    rejected_candidate_refs_json TEXT NOT NULL,
    score_breakdown_json TEXT NOT NULL,
    policy_version_ref TEXT NOT NULL REFERENCES agenda_policy_versions(version_id),
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agenda_feedback (
    feedback_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES agenda_decisions(decision_id),
    prior_feedback_id TEXT REFERENCES agenda_feedback(feedback_id),
    subject_ref TEXT,
    verdict TEXT NOT NULL CHECK(verdict IN ('agree','disagree','partial')),
    notes TEXT NOT NULL,
    source TEXT,
    source_event_ref TEXT,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agenda_outbox_messages (
    message_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    topic TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agenda_outbox_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    message_id TEXT NOT NULL REFERENCES agenda_outbox_messages(message_id),
    state TEXT NOT NULL CHECK(state IN ('pending','claimed','delivered','failed')),
    delivery_attempt_id TEXT,
    claim_expires_at TEXT,
    endpoint_ref TEXT,
    retry_after TEXT,
    delivery_receipt_id TEXT,
    error_code TEXT,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agenda_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agenda_domain_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    aggregate_ref TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agenda_cycle_events_cycle
ON agenda_cycle_events(cycle_id, event_seq);
CREATE INDEX IF NOT EXISTS idx_agenda_outbox_events_message
ON agenda_outbox_events(message_id, event_seq);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agenda_delivery_receipt_unique
ON agenda_outbox_events(delivery_receipt_id)
WHERE state='delivered' AND delivery_receipt_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_questions_company_state
ON research_question_versions(company_ref, state);

CREATE TRIGGER IF NOT EXISTS agenda_control_versions_authorized_insert
BEFORE INSERT ON agenda_control_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'agenda control insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS agenda_control_pointer_authorized_insert
BEFORE INSERT ON agenda_control_pointer WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'agenda control pointer insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS agenda_control_pointer_authorized_update
BEFORE UPDATE ON agenda_control_pointer WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'agenda control pointer update requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS agenda_policy_versions_authorized_insert
BEFORE INSERT ON agenda_policy_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'agenda policy insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS agenda_policy_pointer_authorized_insert
BEFORE INSERT ON agenda_policy_pointer WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'agenda policy pointer insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS agenda_policy_pointer_authorized_update
BEFORE UPDATE ON agenda_policy_pointer WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'agenda policy pointer update requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS mandate_versions_authorized_insert
BEFORE INSERT ON mandate_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mandate insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS mandate_pointer_authorized_insert
BEFORE INSERT ON mandate_pointer WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mandate pointer insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS mandate_pointer_authorized_update
BEFORE UPDATE ON mandate_pointer WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mandate pointer update requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS priority_override_versions_authorized_insert
BEFORE INSERT ON priority_override_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'priority override insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS priority_override_pointer_authorized_insert
BEFORE INSERT ON priority_override_pointer WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'priority override pointer insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS priority_override_pointer_authorized_update
BEFORE UPDATE ON priority_override_pointer WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'priority override pointer update requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS research_question_versions_authorized_insert
BEFORE INSERT ON research_question_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research question insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS research_question_pointer_authorized_insert
BEFORE INSERT ON research_question_pointer WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research question pointer insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS research_question_pointer_authorized_update
BEFORE UPDATE ON research_question_pointer WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research question pointer update requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS perception_snapshot_versions_authorized_insert
BEFORE INSERT ON perception_snapshot_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'perception snapshot insert requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS agenda_cycles_authorized_insert
BEFORE INSERT ON agenda_cycles WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'agenda cycle insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS agenda_cycle_events_authorized_insert
BEFORE INSERT ON agenda_cycle_events WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'agenda cycle event insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS agenda_candidates_authorized_insert
BEFORE INSERT ON agenda_candidates WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'agenda candidate insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS agenda_decisions_authorized_insert
BEFORE INSERT ON agenda_decisions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'agenda decision insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS agenda_feedback_authorized_insert
BEFORE INSERT ON agenda_feedback WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'agenda feedback insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS agenda_outbox_messages_authorized_insert
BEFORE INSERT ON agenda_outbox_messages WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'agenda outbox insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS agenda_outbox_events_authorized_insert
BEFORE INSERT ON agenda_outbox_events WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'agenda outbox event insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS agenda_idempotency_authorized_insert
BEFORE INSERT ON agenda_idempotency WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'agenda idempotency insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS agenda_domain_events_authorized_insert
BEFORE INSERT ON agenda_domain_events WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'agenda domain event insert requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS agenda_control_versions_no_update
BEFORE UPDATE ON agenda_control_versions BEGIN SELECT RAISE(ABORT, 'agenda control versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_policy_versions_no_update
BEFORE UPDATE ON agenda_policy_versions BEGIN SELECT RAISE(ABORT, 'agenda policy versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS mandate_versions_no_update
BEFORE UPDATE ON mandate_versions BEGIN SELECT RAISE(ABORT, 'mandate versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS priority_override_versions_no_update
BEFORE UPDATE ON priority_override_versions BEGIN SELECT RAISE(ABORT, 'priority override versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_question_versions_no_update
BEFORE UPDATE ON research_question_versions BEGIN SELECT RAISE(ABORT, 'research question versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS perception_snapshot_versions_no_update
BEFORE UPDATE ON perception_snapshot_versions BEGIN SELECT RAISE(ABORT, 'perception snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_cycles_no_update
BEFORE UPDATE ON agenda_cycles BEGIN SELECT RAISE(ABORT, 'agenda cycles are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_cycle_events_no_update
BEFORE UPDATE ON agenda_cycle_events BEGIN SELECT RAISE(ABORT, 'agenda cycle events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_candidates_no_update
BEFORE UPDATE ON agenda_candidates BEGIN SELECT RAISE(ABORT, 'agenda candidates are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_decisions_no_update
BEFORE UPDATE ON agenda_decisions BEGIN SELECT RAISE(ABORT, 'agenda decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_feedback_no_update
BEFORE UPDATE ON agenda_feedback BEGIN SELECT RAISE(ABORT, 'agenda feedback is immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_outbox_messages_no_update
BEFORE UPDATE ON agenda_outbox_messages BEGIN SELECT RAISE(ABORT, 'agenda outbox messages are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_outbox_events_no_update
BEFORE UPDATE ON agenda_outbox_events BEGIN SELECT RAISE(ABORT, 'agenda outbox events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_domain_events_no_update
BEFORE UPDATE ON agenda_domain_events BEGIN SELECT RAISE(ABORT, 'agenda domain events are immutable'); END;

CREATE TRIGGER IF NOT EXISTS agenda_control_versions_no_delete
BEFORE DELETE ON agenda_control_versions BEGIN SELECT RAISE(ABORT, 'agenda control versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_policy_versions_no_delete
BEFORE DELETE ON agenda_policy_versions BEGIN SELECT RAISE(ABORT, 'agenda policy versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS mandate_versions_no_delete
BEFORE DELETE ON mandate_versions BEGIN SELECT RAISE(ABORT, 'mandate versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS priority_override_versions_no_delete
BEFORE DELETE ON priority_override_versions BEGIN SELECT RAISE(ABORT, 'priority override versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_question_versions_no_delete
BEFORE DELETE ON research_question_versions BEGIN SELECT RAISE(ABORT, 'research question versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS perception_snapshot_versions_no_delete
BEFORE DELETE ON perception_snapshot_versions BEGIN SELECT RAISE(ABORT, 'perception snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_cycles_no_delete
BEFORE DELETE ON agenda_cycles BEGIN SELECT RAISE(ABORT, 'agenda cycles are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_cycle_events_no_delete
BEFORE DELETE ON agenda_cycle_events BEGIN SELECT RAISE(ABORT, 'agenda cycle events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_candidates_no_delete
BEFORE DELETE ON agenda_candidates BEGIN SELECT RAISE(ABORT, 'agenda candidates are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_decisions_no_delete
BEFORE DELETE ON agenda_decisions BEGIN SELECT RAISE(ABORT, 'agenda decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_feedback_no_delete
BEFORE DELETE ON agenda_feedback BEGIN SELECT RAISE(ABORT, 'agenda feedback is immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_outbox_messages_no_delete
BEFORE DELETE ON agenda_outbox_messages BEGIN SELECT RAISE(ABORT, 'agenda outbox messages are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_outbox_events_no_delete
BEFORE DELETE ON agenda_outbox_events BEGIN SELECT RAISE(ABORT, 'agenda outbox events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_domain_events_no_delete
BEFORE DELETE ON agenda_domain_events BEGIN SELECT RAISE(ABORT, 'agenda domain events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_idempotency_no_update
BEFORE UPDATE ON agenda_idempotency BEGIN SELECT RAISE(ABORT, 'agenda idempotency rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_idempotency_no_delete
BEFORE DELETE ON agenda_idempotency BEGIN SELECT RAISE(ABORT, 'agenda idempotency rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agenda_control_pointer_authorized_delete
BEFORE DELETE ON agenda_control_pointer BEGIN SELECT RAISE(ABORT, 'agenda control pointer cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS agenda_policy_pointer_authorized_delete
BEFORE DELETE ON agenda_policy_pointer BEGIN SELECT RAISE(ABORT, 'agenda policy pointer cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS mandate_pointer_authorized_delete
BEFORE DELETE ON mandate_pointer BEGIN SELECT RAISE(ABORT, 'mandate pointer cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS priority_override_pointer_authorized_delete
BEFORE DELETE ON priority_override_pointer BEGIN SELECT RAISE(ABORT, 'priority override pointer cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS research_question_pointer_authorized_delete
BEFORE DELETE ON research_question_pointer BEGIN SELECT RAISE(ABORT, 'research question pointer cannot be deleted'); END;
