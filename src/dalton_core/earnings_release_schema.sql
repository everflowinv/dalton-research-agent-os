-- ADR-0015: earnings releases (8-K Item 2.02, Exhibit 99.1) and the
-- preliminary actuals parsed out of them.
--
-- Append-only.  A read is one parse of one exhibit body by one parser
-- version; a row is one income-statement line of a parsed read.  Neither is
-- ever updated: "superseded by the 10-Q/10-K" is decided at read time against
-- coverage_mission_statement_filings (earnings_release_intake.preliminary_actuals),
-- so a preliminary number never sits in the same table as a filed one.
CREATE TABLE IF NOT EXISTS earnings_release_reads (
    read_id TEXT PRIMARY KEY,
    company_ref TEXT NOT NULL,
    accession TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('parsed', 'unparseable', 'exhibit_unavailable')),
    reason TEXT,
    period TEXT,
    exhibit_url TEXT,
    body_sha256 TEXT,
    row_count INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_earnings_release_reads_company
ON earnings_release_reads (company_ref, accession, status);
CREATE TABLE IF NOT EXISTS preliminary_actuals (
    row_id TEXT PRIMARY KEY,
    read_ref TEXT NOT NULL REFERENCES earnings_release_reads(read_id),
    company_ref TEXT NOT NULL,
    accession TEXT NOT NULL,
    period TEXT,
    metric TEXT NOT NULL,
    value TEXT NOT NULL,
    unit TEXT NOT NULL,
    scale TEXT NOT NULL,
    span_start INTEGER NOT NULL,
    span_end INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_preliminary_actuals_company
ON preliminary_actuals (company_ref, period, metric);
CREATE TRIGGER IF NOT EXISTS earnings_release_reads_insert_guard
BEFORE INSERT ON earnings_release_reads WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'earnings release read insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS earnings_release_reads_no_update
BEFORE UPDATE ON earnings_release_reads BEGIN
    SELECT RAISE(ABORT, 'earnings release reads are immutable');
END;
CREATE TRIGGER IF NOT EXISTS earnings_release_reads_no_delete
BEFORE DELETE ON earnings_release_reads BEGIN
    SELECT RAISE(ABORT, 'earnings release reads are immutable');
END;
CREATE TRIGGER IF NOT EXISTS preliminary_actuals_insert_guard
BEFORE INSERT ON preliminary_actuals WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'preliminary actual insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS preliminary_actuals_no_update
BEFORE UPDATE ON preliminary_actuals BEGIN
    SELECT RAISE(ABORT, 'preliminary actuals are immutable');
END;
CREATE TRIGGER IF NOT EXISTS preliminary_actuals_no_delete
BEFORE DELETE ON preliminary_actuals BEGIN
    SELECT RAISE(ABORT, 'preliminary actuals are immutable');
END;
