PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS mission_annual_research_starts (
    start_id TEXT PRIMARY KEY,
    admission_ref TEXT NOT NULL UNIQUE
        REFERENCES mission_annual_research_admissions(admission_id),
    admission_hash TEXT NOT NULL,
    run_id TEXT NOT NULL UNIQUE,
    root_work_order_ref TEXT NOT NULL,
    root_work_order_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mission_annual_research_outcomes (
    outcome_id TEXT PRIMARY KEY,
    start_ref TEXT NOT NULL UNIQUE
        REFERENCES mission_annual_research_starts(start_id),
    admission_ref TEXT NOT NULL UNIQUE
        REFERENCES mission_annual_research_admissions(admission_id),
    repair_target_ref TEXT NOT NULL,
    repair_target_hash TEXT NOT NULL,
    candidate_evidence_ref TEXT NOT NULL,
    candidate_evidence_hash TEXT NOT NULL,
    candidate_claim_ref TEXT NOT NULL,
    candidate_claim_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mission_annual_research_promotions (
    promotion_id TEXT PRIMARY KEY,
    outcome_ref TEXT NOT NULL UNIQUE
        REFERENCES mission_annual_research_outcomes(outcome_id),
    admission_ref TEXT NOT NULL UNIQUE
        REFERENCES mission_annual_research_admissions(admission_id),
    evidence_version_ref TEXT NOT NULL,
    evidence_version_hash TEXT NOT NULL,
    claim_version_ref TEXT NOT NULL,
    claim_version_hash TEXT NOT NULL,
    policy_authorization_ref TEXT NOT NULL,
    policy_authorization_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS mission_annual_research_starts_authorized_insert
BEFORE INSERT ON mission_annual_research_starts
WHEN dalton_mission_annual_research_executor_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission annual research start requires its executor');
END;
CREATE TRIGGER IF NOT EXISTS mission_annual_research_starts_no_update
BEFORE UPDATE ON mission_annual_research_starts BEGIN
    SELECT RAISE(ABORT, 'mission annual research starts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS mission_annual_research_starts_no_delete
BEFORE DELETE ON mission_annual_research_starts BEGIN
    SELECT RAISE(ABORT, 'mission annual research starts are append-only');
END;

CREATE TRIGGER IF NOT EXISTS mission_annual_research_outcomes_authorized_insert
BEFORE INSERT ON mission_annual_research_outcomes
WHEN dalton_mission_annual_research_executor_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission annual research outcome requires its executor');
END;
CREATE TRIGGER IF NOT EXISTS mission_annual_research_outcomes_no_update
BEFORE UPDATE ON mission_annual_research_outcomes BEGIN
    SELECT RAISE(ABORT, 'mission annual research outcomes are append-only');
END;
CREATE TRIGGER IF NOT EXISTS mission_annual_research_outcomes_no_delete
BEFORE DELETE ON mission_annual_research_outcomes BEGIN
    SELECT RAISE(ABORT, 'mission annual research outcomes are append-only');
END;

CREATE TRIGGER IF NOT EXISTS mission_annual_research_promotions_authorized_insert
BEFORE INSERT ON mission_annual_research_promotions
WHEN dalton_mission_annual_research_executor_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission annual research promotion requires its executor');
END;
CREATE TRIGGER IF NOT EXISTS mission_annual_research_promotions_no_update
BEFORE UPDATE ON mission_annual_research_promotions BEGIN
    SELECT RAISE(ABORT, 'mission annual research promotions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS mission_annual_research_promotions_no_delete
BEFORE DELETE ON mission_annual_research_promotions BEGIN
    SELECT RAISE(ABORT, 'mission annual research promotions are append-only');
END;
