-- P10c: the documents a CoverageMission owes, as versioned authority records.
--
-- The Playbook already froze what an Initial Screen contains; this table holds
-- the actual documents, one append-only version chain per deliverable, each
-- version binding the mission version and the playbook version it was written
-- under.  Nothing is edited: a rewrite is a new version with a prior link.

CREATE TABLE IF NOT EXISTS mission_deliverable_versions (
    version_id TEXT PRIMARY KEY,
    deliverable_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_ref TEXT REFERENCES mission_deliverable_versions(version_id),
    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
    mission_version_hash TEXT NOT NULL,
    playbook_version_ref TEXT NOT NULL,
    playbook_version_hash TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN (
        'industry_framework','initial_screen','industry_model','company_model',
        'forecast_lines','investment_memo','weekly_brief','event_note',
        'earnings_preview','earnings_calibration'
    )),
    subject_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(deliverable_ref, version_number)
);

CREATE INDEX IF NOT EXISTS idx_mission_deliverables_by_subject
ON mission_deliverable_versions(mission_version_ref, subject_ref, kind, created_at);

CREATE TABLE IF NOT EXISTS mission_deliverable_pointer (
    deliverable_ref TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES mission_deliverable_versions(version_id),
    version_number INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS mission_deliverables_authorized_insert
BEFORE INSERT ON mission_deliverable_versions WHEN dalton_mission_deliverable_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission deliverable insert requires MissionDeliverableAuthority'); END;
CREATE TRIGGER IF NOT EXISTS mission_deliverables_no_update
BEFORE UPDATE ON mission_deliverable_versions BEGIN
    SELECT RAISE(ABORT, 'mission deliverables are append-only'); END;
CREATE TRIGGER IF NOT EXISTS mission_deliverables_no_delete
BEFORE DELETE ON mission_deliverable_versions BEGIN
    SELECT RAISE(ABORT, 'mission deliverables are append-only'); END;

CREATE TRIGGER IF NOT EXISTS mission_deliverable_pointer_authorized_insert
BEFORE INSERT ON mission_deliverable_pointer WHEN dalton_mission_deliverable_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission deliverable pointer insert requires MissionDeliverableAuthority'); END;
CREATE TRIGGER IF NOT EXISTS mission_deliverable_pointer_authorized_update
BEFORE UPDATE ON mission_deliverable_pointer WHEN dalton_mission_deliverable_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission deliverable pointer update requires MissionDeliverableAuthority'); END;
CREATE TRIGGER IF NOT EXISTS mission_deliverable_pointer_no_delete
BEFORE DELETE ON mission_deliverable_pointer BEGIN
    SELECT RAISE(ABORT, 'mission deliverable pointers cannot be deleted'); END;
