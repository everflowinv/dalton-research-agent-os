PRAGMA foreign_keys = ON;

-- Versioned per-day spend cap for the paid thesis-impact model lane.  A cap
-- policy is immutable; the caller always passes one exact version ref.
CREATE TABLE IF NOT EXISTS thesis_impact_budget_policies (
    policy_version_id TEXT PRIMARY KEY,
    day_cap_micros INTEGER NOT NULL CHECK(day_cap_micros > 0),
    currency TEXT NOT NULL CHECK(currency = 'USD'),
    prior_version_id TEXT,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- One reservation per (work order, attempt, phase), admitted before any
-- broker call.  State is derived: an admission with a settlement row counts
-- its actual cost, an admission without one keeps counting its full
-- reservation, so a crash between the paid call and settlement stays
-- conservative.  Nothing in this authority is ever updated or deleted.
CREATE TABLE IF NOT EXISTS thesis_impact_day_admissions (
    admission_id TEXT PRIMARY KEY,
    policy_version_id TEXT NOT NULL REFERENCES thesis_impact_budget_policies(policy_version_id),
    day TEXT NOT NULL CHECK(day GLOB '????-??-??'),
    work_order_ref TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
    phase TEXT NOT NULL CHECK(phase IN ('assessment','verification')),
    route_decision_ref TEXT NOT NULL,
    reserved_micros INTEGER NOT NULL CHECK(reserved_micros > 0),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(work_order_ref, attempt_number, phase)
);

CREATE INDEX IF NOT EXISTS idx_thesis_impact_admission_day
ON thesis_impact_day_admissions(policy_version_id, day);

-- Actual-cost settlement for exactly one admission.
CREATE TABLE IF NOT EXISTS thesis_impact_day_settlements (
    settlement_id TEXT PRIMARY KEY,
    admission_id TEXT NOT NULL UNIQUE REFERENCES thesis_impact_day_admissions(admission_id),
    actual_micros INTEGER NOT NULL CHECK(actual_micros >= 0),
    usage_entry_ref TEXT,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Durable fail-closed admission decision: the day cap would be exceeded.
CREATE TABLE IF NOT EXISTS thesis_impact_day_rejections (
    rejection_id TEXT PRIMARY KEY,
    policy_version_id TEXT NOT NULL REFERENCES thesis_impact_budget_policies(policy_version_id),
    day TEXT NOT NULL CHECK(day GLOB '????-??-??'),
    work_order_ref TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
    phase TEXT NOT NULL CHECK(phase IN ('assessment','verification')),
    route_decision_ref TEXT,
    reserved_micros INTEGER NOT NULL CHECK(reserved_micros > 0),
    day_committed_micros INTEGER NOT NULL CHECK(day_committed_micros >= 0),
    day_cap_micros INTEGER NOT NULL CHECK(day_cap_micros > 0),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(work_order_ref, attempt_number, phase)
);

-- Append-only owner alerts for the thesis-impact lane.
CREATE TABLE IF NOT EXISTS thesis_impact_alerts (
    alert_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('day_budget_exceeded','work_order_failed')),
    severity TEXT NOT NULL CHECK(severity IN ('high','medium')),
    work_order_ref TEXT,
    phase TEXT,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS thesis_impact_alert_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    alert_id TEXT NOT NULL REFERENCES thesis_impact_alerts(alert_id),
    state TEXT NOT NULL CHECK(state IN ('pending','claimed','delivered','failed')),
    claim_expires_at TEXT,
    endpoint_ref TEXT,
    error_code TEXT,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS thesis_impact_budget_policies_no_update
BEFORE UPDATE ON thesis_impact_budget_policies BEGIN
    SELECT RAISE(ABORT, 'thesis impact budget policies are immutable'); END;
CREATE TRIGGER IF NOT EXISTS thesis_impact_budget_policies_no_delete
BEFORE DELETE ON thesis_impact_budget_policies BEGIN
    SELECT RAISE(ABORT, 'thesis impact budget policies are immutable'); END;
CREATE TRIGGER IF NOT EXISTS thesis_impact_day_admissions_no_update
BEFORE UPDATE ON thesis_impact_day_admissions BEGIN
    SELECT RAISE(ABORT, 'thesis impact day admissions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS thesis_impact_day_admissions_no_delete
BEFORE DELETE ON thesis_impact_day_admissions BEGIN
    SELECT RAISE(ABORT, 'thesis impact day admissions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS thesis_impact_day_settlements_no_update
BEFORE UPDATE ON thesis_impact_day_settlements BEGIN
    SELECT RAISE(ABORT, 'thesis impact day settlements are append-only'); END;
CREATE TRIGGER IF NOT EXISTS thesis_impact_day_settlements_no_delete
BEFORE DELETE ON thesis_impact_day_settlements BEGIN
    SELECT RAISE(ABORT, 'thesis impact day settlements are append-only'); END;
CREATE TRIGGER IF NOT EXISTS thesis_impact_day_rejections_no_update
BEFORE UPDATE ON thesis_impact_day_rejections BEGIN
    SELECT RAISE(ABORT, 'thesis impact day rejections are append-only'); END;
CREATE TRIGGER IF NOT EXISTS thesis_impact_day_rejections_no_delete
BEFORE DELETE ON thesis_impact_day_rejections BEGIN
    SELECT RAISE(ABORT, 'thesis impact day rejections are append-only'); END;
CREATE TRIGGER IF NOT EXISTS thesis_impact_alerts_no_update
BEFORE UPDATE ON thesis_impact_alerts BEGIN
    SELECT RAISE(ABORT, 'thesis impact alerts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS thesis_impact_alerts_no_delete
BEFORE DELETE ON thesis_impact_alerts BEGIN
    SELECT RAISE(ABORT, 'thesis impact alerts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS thesis_impact_alert_events_no_update
BEFORE UPDATE ON thesis_impact_alert_events BEGIN
    SELECT RAISE(ABORT, 'thesis impact alert events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS thesis_impact_alert_events_no_delete
BEFORE DELETE ON thesis_impact_alert_events BEGIN
    SELECT RAISE(ABORT, 'thesis impact alert events are append-only'); END;
