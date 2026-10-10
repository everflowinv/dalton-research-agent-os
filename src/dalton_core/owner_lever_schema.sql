-- 2026-10-06: the owner's levers (vision-gap #11/#12).
--
-- One append-only receipt per thing the owner did to the running system:
-- pause / resume a company or a lane, stop one research line, emergency stop /
-- resume everything, prioritise a company, change the coverage list.  The
-- current state is *folded* from these rows by ``owner_levers.lever_state``
-- (latest receipt per scope wins; a pause whose ``until`` has passed reads as
-- resumed), so nothing here is ever updated: expiring is a fact about the
-- clock, not a write, and the history of who paused what, when, why and until
-- when stays readable by the inspection forever.

CREATE TABLE IF NOT EXISTS owner_lever_receipts (
    receipt_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL UNIQUE CHECK(sequence >= 1),
    lever TEXT NOT NULL CHECK(lever IN (
        'pause', 'resume', 'stop_line', 'emergency_stop', 'resume_all',
        'prioritize', 'coverage_change')),
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('company', 'lane', 'line', 'global', 'mission')),
    scope_ref TEXT NOT NULL CHECK(length(trim(scope_ref)) > 0),
    prior_receipt_id TEXT REFERENCES owner_lever_receipts(receipt_id),
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    until_at TEXT,
    outcome TEXT NOT NULL,
    actor_ref TEXT NOT NULL CHECK(actor_ref LIKE 'human:%'),
    idempotency_key TEXT UNIQUE,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS owner_lever_receipts_by_scope
ON owner_lever_receipts(scope_kind, scope_ref, sequence DESC);

CREATE TRIGGER IF NOT EXISTS owner_lever_receipts_insert_guard
BEFORE INSERT ON owner_lever_receipts WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'owner lever receipt insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS owner_lever_receipts_no_update
BEFORE UPDATE ON owner_lever_receipts BEGIN
    SELECT RAISE(ABORT, 'owner lever receipts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS owner_lever_receipts_no_delete
BEFORE DELETE ON owner_lever_receipts BEGIN
    SELECT RAISE(ABORT, 'owner lever receipts are append-only');
END;
