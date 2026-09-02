PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS forecast_reconciliations (
    reconciliation_id TEXT PRIMARY KEY,
    subject_ref TEXT NOT NULL,
    metric_ref TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    forecast_line_ref TEXT NOT NULL,
    forecast_line_version_ref TEXT NOT NULL,
    forecast_line_version_hash TEXT NOT NULL,
    claim_version_ref TEXT NOT NULL,
    claim_version_hash TEXT NOT NULL,
    band TEXT NOT NULL CHECK(band IN ('within_tolerance','notable','overturn_candidate')),
    human_checkpoint TEXT CHECK(human_checkpoint IS NULL OR human_checkpoint = 'forecast_overturn'),
    mission_version_ref TEXT,
    requested_by TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(forecast_line_version_ref, claim_version_ref)
);

CREATE TABLE IF NOT EXISTS forecast_overturn_decisions (
    decision_id TEXT PRIMARY KEY,
    reconciliation_ref TEXT NOT NULL UNIQUE REFERENCES forecast_reconciliations(reconciliation_id),
    reconciliation_hash TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('keep_forecast','revise_forecast')),
    actor_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forecast_reconciliation_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_forecast_reconciliation_subject
ON forecast_reconciliations(subject_ref, created_at, reconciliation_id);
CREATE INDEX IF NOT EXISTS idx_forecast_reconciliation_claim
ON forecast_reconciliations(claim_version_ref, created_at);

CREATE TRIGGER IF NOT EXISTS forecast_reconciliations_authorized_insert
BEFORE INSERT ON forecast_reconciliations WHEN dalton_forecast_reconciliation_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'forecast reconciliation insert requires ForecastReconciliationAuthority'); END;
CREATE TRIGGER IF NOT EXISTS forecast_overturn_decisions_authorized_insert
BEFORE INSERT ON forecast_overturn_decisions WHEN dalton_forecast_reconciliation_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'forecast overturn decision insert requires ForecastReconciliationAuthority'); END;
CREATE TRIGGER IF NOT EXISTS forecast_reconciliation_idempotency_authorized_insert
BEFORE INSERT ON forecast_reconciliation_idempotency WHEN dalton_forecast_reconciliation_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'forecast reconciliation idempotency insert requires ForecastReconciliationAuthority'); END;

CREATE TRIGGER IF NOT EXISTS forecast_reconciliations_no_update
BEFORE UPDATE ON forecast_reconciliations BEGIN SELECT RAISE(ABORT, 'forecast reconciliations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS forecast_reconciliations_no_delete
BEFORE DELETE ON forecast_reconciliations BEGIN SELECT RAISE(ABORT, 'forecast reconciliations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS forecast_overturn_decisions_no_update
BEFORE UPDATE ON forecast_overturn_decisions BEGIN SELECT RAISE(ABORT, 'forecast overturn decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS forecast_overturn_decisions_no_delete
BEFORE DELETE ON forecast_overturn_decisions BEGIN SELECT RAISE(ABORT, 'forecast overturn decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS forecast_reconciliation_idempotency_no_update
BEFORE UPDATE ON forecast_reconciliation_idempotency BEGIN SELECT RAISE(ABORT, 'forecast reconciliation idempotency is immutable'); END;
CREATE TRIGGER IF NOT EXISTS forecast_reconciliation_idempotency_no_delete
BEFORE DELETE ON forecast_reconciliation_idempotency BEGIN SELECT RAISE(ABORT, 'forecast reconciliation idempotency is immutable'); END;
