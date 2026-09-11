PRAGMA foreign_keys = ON;

-- A mission may admit one exact, already-acquired annual-report question
-- without pretending that a person approved an Agenda ResearchPlan.  This is
-- authority only: dispatch/model execution remains a separate consumer and
-- must resolve this row again immediately before enqueue.
CREATE TABLE IF NOT EXISTS mission_annual_research_admissions (
    admission_id TEXT PRIMARY KEY,
    identity_hash TEXT NOT NULL UNIQUE,
    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
    mission_version_hash TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    repair_feedback_ref TEXT NOT NULL,
    repair_feedback_hash TEXT NOT NULL,
    repair_target_ref TEXT NOT NULL,
    repair_target_hash TEXT NOT NULL,
    source_content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL CHECK(actor_ref LIKE 'automation:%'),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS mission_annual_research_admissions_authorized_insert
BEFORE INSERT ON mission_annual_research_admissions
WHEN dalton_mission_annual_research_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission annual research insert requires MissionAnnualResearchAuthority');
END;
CREATE TRIGGER IF NOT EXISTS mission_annual_research_admissions_no_update
BEFORE UPDATE ON mission_annual_research_admissions BEGIN
    SELECT RAISE(ABORT, 'mission annual research admissions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS mission_annual_research_admissions_no_delete
BEFORE DELETE ON mission_annual_research_admissions BEGIN
    SELECT RAISE(ABORT, 'mission annual research admissions are append-only');
END;
