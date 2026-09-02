PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS coverage_mission_versions (
    mission_version_id TEXT PRIMARY KEY,
    mission_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES coverage_mission_versions(mission_version_id),
    industry_ref TEXT NOT NULL,
    playbook_version_ref TEXT NOT NULL,
    constitution_version_ref TEXT NOT NULL,
    mandate_version_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(mission_ref, version_number)
);

CREATE TABLE IF NOT EXISTS coverage_mission_pointer (
    mission_ref TEXT PRIMARY KEY,
    mission_version_id TEXT NOT NULL UNIQUE REFERENCES coverage_mission_versions(mission_version_id),
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coverage_mission_stage_records (
    record_id TEXT PRIMARY KEY,
    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
    company_ref TEXT NOT NULL,
    stage_ref TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('entered','gate_passed','gate_failed')),
    actor_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coverage_mission_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_coverage_mission_history
ON coverage_mission_versions(mission_ref, version_number);
CREATE INDEX IF NOT EXISTS idx_coverage_mission_stage_by_company
ON coverage_mission_stage_records(mission_version_ref, company_ref, stage_ref, created_at);

CREATE TRIGGER IF NOT EXISTS coverage_mission_versions_authorized_insert
BEFORE INSERT ON coverage_mission_versions WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'coverage mission insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_pointer_authorized_insert
BEFORE INSERT ON coverage_mission_pointer WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'coverage mission pointer insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_pointer_authorized_update
BEFORE UPDATE ON coverage_mission_pointer WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'coverage mission pointer update requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_stage_records_authorized_insert
BEFORE INSERT ON coverage_mission_stage_records WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'coverage mission stage record insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_idempotency_authorized_insert
BEFORE INSERT ON coverage_mission_idempotency WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'coverage mission idempotency insert requires CoverageMissionAuthority'); END;

CREATE TRIGGER IF NOT EXISTS coverage_mission_versions_no_update
BEFORE UPDATE ON coverage_mission_versions BEGIN SELECT RAISE(ABORT, 'coverage mission versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_versions_no_delete
BEFORE DELETE ON coverage_mission_versions BEGIN SELECT RAISE(ABORT, 'coverage mission versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_pointer_no_delete
BEFORE DELETE ON coverage_mission_pointer BEGIN SELECT RAISE(ABORT, 'coverage mission pointer cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_stage_records_no_update
BEFORE UPDATE ON coverage_mission_stage_records BEGIN SELECT RAISE(ABORT, 'coverage mission stage records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_stage_records_no_delete
BEFORE DELETE ON coverage_mission_stage_records BEGIN SELECT RAISE(ABORT, 'coverage mission stage records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_idempotency_no_update
BEFORE UPDATE ON coverage_mission_idempotency BEGIN SELECT RAISE(ABORT, 'coverage mission idempotency is immutable'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_idempotency_no_delete
BEFORE DELETE ON coverage_mission_idempotency BEGIN SELECT RAISE(ABORT, 'coverage mission idempotency is immutable'); END;
