-- Exact domestic v5 statement table contracts captured from 4a2668cb.
CREATE TABLE IF NOT EXISTS coverage_mission_statement_dispatches (
    dispatch_id TEXT PRIMARY KEY,
    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
    mission_version_hash TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    ticker TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    form TEXT NOT NULL CHECK(form IN ('10-Q','10-K')),
    filing_limit INTEGER NOT NULL CHECK(filing_limit BETWEEN 1 AND 8),
    -- Which try this is. Without it the request is the identity, so a dispatch
    -- rejected once could never be re-queued and approving the governance
    -- record afterwards would change nothing.
    attempt INTEGER NOT NULL CHECK(attempt BETWEEN 0 AND 3),
    authorization_json TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK(status IN ('pending','launched','succeeded','failed','rejected')),
    ticket_ref TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS coverage_mission_statement_dispatches_authorized_insert
BEFORE INSERT ON coverage_mission_statement_dispatches WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'statement dispatch insert requires CoverageMissionAuthority'); END;

CREATE TRIGGER IF NOT EXISTS coverage_mission_statement_dispatches_authorized_update
BEFORE UPDATE ON coverage_mission_statement_dispatches WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'statement dispatch update requires CoverageMissionAuthority'); END;

CREATE TRIGGER IF NOT EXISTS coverage_mission_statement_dispatches_no_delete
BEFORE DELETE ON coverage_mission_statement_dispatches BEGIN
    SELECT RAISE(ABORT, 'statement dispatches cannot be deleted'); END;

CREATE INDEX IF NOT EXISTS idx_coverage_mission_statement_dispatch_pending
ON coverage_mission_statement_dispatches(status, created_at, dispatch_id);

CREATE TABLE IF NOT EXISTS coverage_mission_statement_filings (
    ingest_id TEXT PRIMARY KEY,
    dispatch_id TEXT NOT NULL
        REFERENCES coverage_mission_statement_dispatches(dispatch_id),
    company_ref TEXT NOT NULL,
    cik TEXT NOT NULL,
    entity_name TEXT NOT NULL,
    accession TEXT NOT NULL,
    form TEXT NOT NULL CHECK(form IN ('10-Q','10-K','10-Q/A','10-K/A')),
    filed TEXT NOT NULL,
    report_date TEXT NOT NULL,
    line_count INTEGER NOT NULL,
    source_record_refs_json TEXT NOT NULL,
    governance_ref TEXT NOT NULL,
    governance_hash TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    -- A filing is ingested once per company. A second run of the same 10-Q is
    -- the same filing, not a second set of 495 lines.
    UNIQUE(company_ref, accession)
);

CREATE TRIGGER IF NOT EXISTS coverage_mission_statement_filings_authorized_insert
BEFORE INSERT ON coverage_mission_statement_filings WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'statement filing insert requires CoverageMissionAuthority'); END;

CREATE TRIGGER IF NOT EXISTS coverage_mission_statement_filings_no_update
BEFORE UPDATE ON coverage_mission_statement_filings BEGIN
    SELECT RAISE(ABORT, 'statement filings are append-only'); END;

CREATE TRIGGER IF NOT EXISTS coverage_mission_statement_filings_no_delete
BEFORE DELETE ON coverage_mission_statement_filings BEGIN
    SELECT RAISE(ABORT, 'statement filings are append-only'); END;
