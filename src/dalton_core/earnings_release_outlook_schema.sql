-- Batch B (2026-10-09): bookings, growth and guidance read from an earnings
-- release's Exhibit 99.1 (earnings_release_outlook).  Append-only: one row per
-- (exhibit body, parser version); a new parser appends, nothing is updated.
CREATE TABLE IF NOT EXISTS earnings_release_outlooks (
    outlook_id TEXT PRIMARY KEY,
    company_ref TEXT NOT NULL,
    accession TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    release_period TEXT,
    item_count INTEGER NOT NULL CHECK(item_count >= 0),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(body_sha256, parser_version)
);
CREATE INDEX IF NOT EXISTS earnings_release_outlooks_by_company
ON earnings_release_outlooks(company_ref, created_at);
CREATE TRIGGER IF NOT EXISTS earnings_release_outlooks_insert_guard
BEFORE INSERT ON earnings_release_outlooks WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'earnings release outlook insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS earnings_release_outlooks_no_update
BEFORE UPDATE ON earnings_release_outlooks BEGIN
    SELECT RAISE(ABORT, 'earnings release outlooks are append-only');
END;
CREATE TRIGGER IF NOT EXISTS earnings_release_outlooks_no_delete
BEFORE DELETE ON earnings_release_outlooks BEGIN
    SELECT RAISE(ABORT, 'earnings release outlooks are append-only');
END;

-- 2026-10-09 (batch B follow-up): one-off items a release names (legal and
-- litigation charges, severance/restructuring, impairments, gains on sale),
-- read deterministically from the same exhibit.  Same shape and the same
-- append-only rule: one row per (exhibit body, one-off parser version).
CREATE TABLE IF NOT EXISTS earnings_release_one_offs (
    one_off_id TEXT PRIMARY KEY,
    company_ref TEXT NOT NULL,
    accession TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    release_period TEXT,
    item_count INTEGER NOT NULL CHECK(item_count >= 0),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(body_sha256, parser_version)
);
CREATE INDEX IF NOT EXISTS earnings_release_one_offs_by_company
ON earnings_release_one_offs(company_ref, created_at);
CREATE TRIGGER IF NOT EXISTS earnings_release_one_offs_insert_guard
BEFORE INSERT ON earnings_release_one_offs WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'earnings release one-off insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS earnings_release_one_offs_no_update
BEFORE UPDATE ON earnings_release_one_offs BEGIN
    SELECT RAISE(ABORT, 'earnings release one-offs are append-only');
END;
CREATE TRIGGER IF NOT EXISTS earnings_release_one_offs_no_delete
BEFORE DELETE ON earnings_release_one_offs BEGIN
    SELECT RAISE(ABORT, 'earnings release one-offs are append-only');
END;
