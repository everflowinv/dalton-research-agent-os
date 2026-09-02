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

CREATE TABLE IF NOT EXISTS coverage_mission_stage_claims (
    record_id TEXT PRIMARY KEY,
    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
    company_ref TEXT NOT NULL,
    stage_ref TEXT NOT NULL,
    claim_version_ref TEXT NOT NULL,
    claim_version_hash TEXT NOT NULL,
    evidence_version_ref TEXT NOT NULL,
    evidence_version_hash TEXT NOT NULL,
    source_location TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(mission_version_ref, claim_version_ref)
);

CREATE TABLE IF NOT EXISTS coverage_mission_sec_dispatches (
    dispatch_id TEXT PRIMARY KEY,
    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
    mission_version_hash TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    ticker TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    form TEXT NOT NULL CHECK(form IN ('10-Q','10-K')),
    filed_from TEXT NOT NULL,
    filed_to TEXT NOT NULL,
    expected_accession TEXT NOT NULL,
    observation_ref TEXT NOT NULL,
    authorization_json TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','launched','rejected')),
    ticket_ref TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- P9d-1: one launched discovery child per row; settled by the controller
-- tick from the child's ticket.  Rows are never deleted.
CREATE TABLE IF NOT EXISTS coverage_mission_discovery_dispatches (
    dispatch_id TEXT PRIMARY KEY,
    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
    mission_version_hash TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    discovery_plan_ref TEXT NOT NULL,
    discovery_plan_hash TEXT NOT NULL,
    spec_ref TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    authorization_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('launched','succeeded','failed','rejected')),
    ticket_ref TEXT NOT NULL,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- P9d-1: append-only record of one completed library search bound to the
-- exact Core connector invocation and source envelope it produced.
CREATE TABLE IF NOT EXISTS coverage_mission_source_discoveries (
    record_id TEXT PRIMARY KEY,
    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
    mission_version_hash TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    discovery_plan_ref TEXT NOT NULL,
    discovery_plan_hash TEXT NOT NULL,
    spec_ref TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    connector_invocation_ref TEXT NOT NULL,
    source_envelope_ref TEXT NOT NULL,
    source_envelope_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(mission_version_ref, source_envelope_ref)
);

-- P9d-1: one row per (mission version, discovered document); status moves
-- discovered -> acquisition_launched -> acquired | acquisition_failed.
CREATE TABLE IF NOT EXISTS coverage_mission_discovered_documents (
    record_id TEXT PRIMARY KEY,
    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
    company_ref TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    discovery_ref TEXT NOT NULL REFERENCES coverage_mission_source_discoveries(record_id),
    status TEXT NOT NULL CHECK(status IN ('discovered','already_in_authority','acquisition_launched','acquired','acquisition_failed')),
    ticket_ref TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(mission_version_ref, document_ref)
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
CREATE INDEX IF NOT EXISTS idx_coverage_mission_claims_by_company
ON coverage_mission_stage_claims(mission_version_ref, company_ref, stage_ref, created_at);
CREATE INDEX IF NOT EXISTS idx_coverage_mission_sec_dispatch_pending
ON coverage_mission_sec_dispatches(status, created_at, dispatch_id);
CREATE INDEX IF NOT EXISTS idx_coverage_mission_discovery_dispatch_status
ON coverage_mission_discovery_dispatches(status, created_at, dispatch_id);
CREATE INDEX IF NOT EXISTS idx_coverage_mission_discovery_dispatch_by_spec
ON coverage_mission_discovery_dispatches(mission_version_ref, company_ref, spec_ref, created_at);
CREATE INDEX IF NOT EXISTS idx_coverage_mission_discoveries_by_company
ON coverage_mission_source_discoveries(mission_version_ref, company_ref, spec_ref, created_at);
CREATE INDEX IF NOT EXISTS idx_coverage_mission_discovered_documents_status
ON coverage_mission_discovered_documents(status, created_at, record_id);

CREATE TRIGGER IF NOT EXISTS coverage_mission_discovery_dispatches_authorized_insert
BEFORE INSERT ON coverage_mission_discovery_dispatches WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission discovery dispatch insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_discovery_dispatches_authorized_update
BEFORE UPDATE ON coverage_mission_discovery_dispatches WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission discovery dispatch update requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_discovery_dispatches_no_delete
BEFORE DELETE ON coverage_mission_discovery_dispatches BEGIN SELECT RAISE(ABORT, 'mission discovery dispatches cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_source_discoveries_authorized_insert
BEFORE INSERT ON coverage_mission_source_discoveries WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission source discovery insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_source_discoveries_no_update
BEFORE UPDATE ON coverage_mission_source_discoveries BEGIN SELECT RAISE(ABORT, 'mission source discoveries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_source_discoveries_no_delete
BEFORE DELETE ON coverage_mission_source_discoveries BEGIN SELECT RAISE(ABORT, 'mission source discoveries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_discovered_documents_authorized_insert
BEFORE INSERT ON coverage_mission_discovered_documents WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission discovered document insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_discovered_documents_authorized_update
BEFORE UPDATE ON coverage_mission_discovered_documents WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission discovered document update requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_discovered_documents_no_delete
BEFORE DELETE ON coverage_mission_discovered_documents BEGIN SELECT RAISE(ABORT, 'mission discovered documents cannot be deleted'); END;

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
CREATE TRIGGER IF NOT EXISTS coverage_mission_stage_claims_authorized_insert
BEFORE INSERT ON coverage_mission_stage_claims WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'coverage mission stage claim insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_sec_dispatches_authorized_insert
BEFORE INSERT ON coverage_mission_sec_dispatches WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission SEC dispatch insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_sec_dispatches_authorized_update
BEFORE UPDATE ON coverage_mission_sec_dispatches WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission SEC dispatch update requires CoverageMissionAuthority'); END;
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
CREATE TRIGGER IF NOT EXISTS coverage_mission_stage_claims_no_update
BEFORE UPDATE ON coverage_mission_stage_claims BEGIN SELECT RAISE(ABORT, 'coverage mission stage claims are append-only'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_stage_claims_no_delete
BEFORE DELETE ON coverage_mission_stage_claims BEGIN SELECT RAISE(ABORT, 'coverage mission stage claims are append-only'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_sec_dispatches_no_delete
BEFORE DELETE ON coverage_mission_sec_dispatches BEGIN SELECT RAISE(ABORT, 'mission SEC dispatches cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_idempotency_no_update
BEFORE UPDATE ON coverage_mission_idempotency BEGIN SELECT RAISE(ABORT, 'coverage mission idempotency is immutable'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_idempotency_no_delete
BEFORE DELETE ON coverage_mission_idempotency BEGIN SELECT RAISE(ABORT, 'coverage mission idempotency is immutable'); END;
