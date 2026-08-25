PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS answer_sufficiency_policy_versions (
    version_id TEXT PRIMARY KEY,
    policy_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES answer_sufficiency_policy_versions(version_id),
    mandate_ref TEXT NOT NULL,
    mandate_version_ref TEXT NOT NULL,
    mandate_version_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(policy_ref, version_number)
);

CREATE TABLE IF NOT EXISTS answer_sufficiency_policy_pointer (
    mandate_ref TEXT PRIMARY KEY,
    version_id TEXT NOT NULL UNIQUE
        REFERENCES answer_sufficiency_policy_versions(version_id),
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS answer_routing_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS answer_refresh_budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    decision_ref TEXT NOT NULL UNIQUE,
    policy_version_ref TEXT NOT NULL
        REFERENCES answer_sufficiency_policy_versions(version_id),
    loop_version_ref TEXT NOT NULL UNIQUE,
    budget_day TEXT NOT NULL,
    reserved_cost_units INTEGER NOT NULL CHECK(reserved_cost_units > 0),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS answer_refresh_dispatch_receipts (
    receipt_id TEXT PRIMARY KEY,
    reservation_ref TEXT NOT NULL UNIQUE
        REFERENCES answer_refresh_budget_reservations(reservation_id),
    decision_ref TEXT NOT NULL UNIQUE,
    loop_version_ref TEXT NOT NULL,
    round_ref TEXT NOT NULL UNIQUE,
    work_order_ref TEXT NOT NULL UNIQUE,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS answer_refresh_outcome_receipts (
    receipt_id TEXT PRIMARY KEY,
    dispatch_ref TEXT NOT NULL UNIQUE
        REFERENCES answer_refresh_dispatch_receipts(receipt_id),
    outcome_ref TEXT NOT NULL UNIQUE,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS answer_sufficiency_policy_versions_authorized_insert
BEFORE INSERT ON answer_sufficiency_policy_versions
WHEN dalton_answer_routing_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'answer policy insert requires AnswerRoutingAuthority');
END;
CREATE TRIGGER IF NOT EXISTS answer_sufficiency_policy_pointer_authorized_insert
BEFORE INSERT ON answer_sufficiency_policy_pointer
WHEN dalton_answer_routing_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'answer policy pointer insert requires AnswerRoutingAuthority');
END;
CREATE TRIGGER IF NOT EXISTS answer_sufficiency_policy_pointer_authorized_update
BEFORE UPDATE ON answer_sufficiency_policy_pointer
WHEN dalton_answer_routing_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'answer policy pointer update requires AnswerRoutingAuthority');
END;
CREATE TRIGGER IF NOT EXISTS answer_routing_idempotency_authorized_insert
BEFORE INSERT ON answer_routing_idempotency
WHEN dalton_answer_routing_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'answer routing idempotency insert requires AnswerRoutingAuthority');
END;
CREATE TRIGGER IF NOT EXISTS answer_refresh_budget_reservations_authorized_insert
BEFORE INSERT ON answer_refresh_budget_reservations
WHEN dalton_answer_routing_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'answer refresh reservation requires AnswerRoutingAuthority');
END;
CREATE TRIGGER IF NOT EXISTS answer_refresh_dispatch_receipts_authorized_insert
BEFORE INSERT ON answer_refresh_dispatch_receipts
WHEN dalton_answer_routing_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'answer refresh dispatch requires AnswerRoutingAuthority');
END;
CREATE TRIGGER IF NOT EXISTS answer_refresh_outcome_receipts_authorized_insert
BEFORE INSERT ON answer_refresh_outcome_receipts
WHEN dalton_answer_routing_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'answer refresh outcome requires AnswerRoutingAuthority');
END;

CREATE TRIGGER IF NOT EXISTS answer_sufficiency_policy_versions_no_update
BEFORE UPDATE ON answer_sufficiency_policy_versions BEGIN
    SELECT RAISE(ABORT, 'answer_sufficiency_policy_versions is immutable');
END;
CREATE TRIGGER IF NOT EXISTS answer_sufficiency_policy_versions_no_delete
BEFORE DELETE ON answer_sufficiency_policy_versions BEGIN
    SELECT RAISE(ABORT, 'answer_sufficiency_policy_versions is immutable');
END;
CREATE TRIGGER IF NOT EXISTS answer_sufficiency_policy_pointer_no_delete
BEFORE DELETE ON answer_sufficiency_policy_pointer BEGIN
    SELECT RAISE(ABORT, 'answer_sufficiency_policy_pointer cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS answer_routing_idempotency_no_update
BEFORE UPDATE ON answer_routing_idempotency BEGIN
    SELECT RAISE(ABORT, 'answer_routing_idempotency is immutable');
END;
CREATE TRIGGER IF NOT EXISTS answer_routing_idempotency_no_delete
BEFORE DELETE ON answer_routing_idempotency BEGIN
    SELECT RAISE(ABORT, 'answer_routing_idempotency is immutable');
END;
CREATE TRIGGER IF NOT EXISTS answer_refresh_budget_reservations_no_update
BEFORE UPDATE ON answer_refresh_budget_reservations BEGIN
    SELECT RAISE(ABORT, 'answer_refresh_budget_reservations is immutable');
END;
CREATE TRIGGER IF NOT EXISTS answer_refresh_budget_reservations_no_delete
BEFORE DELETE ON answer_refresh_budget_reservations BEGIN
    SELECT RAISE(ABORT, 'answer_refresh_budget_reservations is immutable');
END;
CREATE TRIGGER IF NOT EXISTS answer_refresh_dispatch_receipts_no_update
BEFORE UPDATE ON answer_refresh_dispatch_receipts BEGIN
    SELECT RAISE(ABORT, 'answer_refresh_dispatch_receipts is immutable');
END;
CREATE TRIGGER IF NOT EXISTS answer_refresh_dispatch_receipts_no_delete
BEFORE DELETE ON answer_refresh_dispatch_receipts BEGIN
    SELECT RAISE(ABORT, 'answer_refresh_dispatch_receipts is immutable');
END;
CREATE TRIGGER IF NOT EXISTS answer_refresh_outcome_receipts_no_update
BEFORE UPDATE ON answer_refresh_outcome_receipts BEGIN
    SELECT RAISE(ABORT, 'answer_refresh_outcome_receipts is immutable');
END;
CREATE TRIGGER IF NOT EXISTS answer_refresh_outcome_receipts_no_delete
BEFORE DELETE ON answer_refresh_outcome_receipts BEGIN
    SELECT RAISE(ABORT, 'answer_refresh_outcome_receipts is immutable');
END;
