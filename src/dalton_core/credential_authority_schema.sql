-- Credential metadata authority. Credential values and host handles never enter
-- these tables. Grants, revocations, and per-call use receipts are immutable.

CREATE TABLE IF NOT EXISTS credential_grants (
    grant_id TEXT PRIMARY KEY,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credential_revocation_events (
    event_id TEXT PRIMARY KEY,
    grant_ref TEXT NOT NULL UNIQUE REFERENCES credential_grants(grant_id),
    grant_hash TEXT NOT NULL,
    reason_code TEXT NOT NULL CHECK (reason_code IN (
        'manual','rotation','permission_change','security_incident','source_disabled'
    )),
    actor_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credential_use_receipts (
    use_id TEXT PRIMARY KEY,
    grant_ref TEXT NOT NULL REFERENCES credential_grants(grant_id),
    grant_hash TEXT NOT NULL,
    runner_request_ref TEXT NOT NULL,
    runner_request_hash TEXT NOT NULL,
    connector_invocation_ref TEXT NOT NULL,
    connector_invocation_hash TEXT NOT NULL,
    reservation_ref TEXT NOT NULL,
    reservation_hash TEXT NOT NULL,
    physical_attempt_number INTEGER NOT NULL CHECK (physical_attempt_number > 0),
    operation TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (runner_request_ref, reservation_ref, physical_attempt_number)
);

CREATE INDEX IF NOT EXISTS credential_use_by_grant
ON credential_use_receipts(grant_ref, created_at, use_id);

CREATE TABLE IF NOT EXISTS credential_idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS credential_grants_authorized_insert
BEFORE INSERT ON credential_grants WHEN dalton_authorized()=0 BEGIN
    SELECT RAISE(ABORT, 'credential grant insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS credential_revocations_authorized_insert
BEFORE INSERT ON credential_revocation_events WHEN dalton_authorized()=0 BEGIN
    SELECT RAISE(ABORT, 'credential revocation insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS credential_uses_authorized_insert
BEFORE INSERT ON credential_use_receipts WHEN dalton_authorized()=0 BEGIN
    SELECT RAISE(ABORT, 'credential use insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS credential_idempotency_authorized_insert
BEFORE INSERT ON credential_idempotency_keys WHEN dalton_authorized()=0 BEGIN
    SELECT RAISE(ABORT, 'credential idempotency insert requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS credential_grants_no_update
BEFORE UPDATE ON credential_grants BEGIN
    SELECT RAISE(ABORT, 'credential grants are immutable');
END;
CREATE TRIGGER IF NOT EXISTS credential_grants_no_delete
BEFORE DELETE ON credential_grants BEGIN
    SELECT RAISE(ABORT, 'credential grants are immutable');
END;
CREATE TRIGGER IF NOT EXISTS credential_revocations_no_update
BEFORE UPDATE ON credential_revocation_events BEGIN
    SELECT RAISE(ABORT, 'credential revocations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS credential_revocations_no_delete
BEFORE DELETE ON credential_revocation_events BEGIN
    SELECT RAISE(ABORT, 'credential revocations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS credential_uses_no_update
BEFORE UPDATE ON credential_use_receipts BEGIN
    SELECT RAISE(ABORT, 'credential use receipts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS credential_uses_no_delete
BEFORE DELETE ON credential_use_receipts BEGIN
    SELECT RAISE(ABORT, 'credential use receipts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS credential_idempotency_no_update
BEFORE UPDATE ON credential_idempotency_keys BEGIN
    SELECT RAISE(ABORT, 'credential idempotency rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS credential_idempotency_no_delete
BEFORE DELETE ON credential_idempotency_keys BEGIN
    SELECT RAISE(ABORT, 'credential idempotency rows are immutable');
END;
