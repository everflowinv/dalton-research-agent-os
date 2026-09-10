-- P11b: one broker's published view of one company, read out of one note.
--
-- Deliberately not a row of ``coverage_mission_document_figures``. That table
-- holds figures *about the company* -- what it filed, what management said --
-- and its ``source_grade`` CHECK names only those two grades. A price target
-- is neither: it is the broker's own number, it needs the broker, the analyst,
-- the rating and the horizon beside it to mean anything, and it must never be
-- reachable by a query that means "figures this company published". Its own
-- table says all of that structurally instead of by convention.
--
-- Append-only for the same reason every other record here is: a broker raising
-- a target is a new statement, not a correction of the old one, and the old
-- one is what makes the raise visible.
CREATE TABLE IF NOT EXISTS street_estimates (
    estimate_id TEXT PRIMARY KEY,
    company_ref TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    spec_ref TEXT NOT NULL,
    source_manifest_hash TEXT NOT NULL,
    -- Who published it. ``broker`` is the slug the whole system counts by --
    -- two TD Cowen notes are one house -- and ``broker_as_named`` is what the
    -- document called itself, kept so the mapping can be checked.
    broker TEXT NOT NULL,
    broker_as_named TEXT NOT NULL,
    broker_basis TEXT NOT NULL CHECK(
        broker_basis IN ('document_metadata','page_text')),
    analysts_json TEXT NOT NULL,
    published_on TEXT NOT NULL,
    -- The rating, normalised to three codes, and the word the broker used.
    -- Null is a real answer: a note may carry a target and no rating.
    rating TEXT CHECK(rating IS NULL OR rating IN ('buy','hold','sell')),
    rating_as_named TEXT,
    rating_scale TEXT,
    target_value TEXT,
    target_currency TEXT,
    target_horizon TEXT,
    source_grade TEXT NOT NULL CHECK(source_grade IN ('broker-research-report')),
    extraction_method TEXT NOT NULL CHECK(
        extraction_method IN ('deterministic','model')),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    observed_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- One note is one statement by one house about one company. Reading the
    -- same note twice contributes one row.
    UNIQUE(company_ref, document_ref)
);

CREATE INDEX IF NOT EXISTS street_estimates_by_company
ON street_estimates(company_ref, published_on DESC);
CREATE INDEX IF NOT EXISTS street_estimates_by_broker
ON street_estimates(company_ref, broker, published_on DESC);

CREATE TRIGGER IF NOT EXISTS street_estimates_insert_guard
BEFORE INSERT ON street_estimates WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'street estimate insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS street_estimates_no_update
BEFORE UPDATE ON street_estimates BEGIN
    SELECT RAISE(ABORT, 'street estimates are append-only');
END;
CREATE TRIGGER IF NOT EXISTS street_estimates_no_delete
BEFORE DELETE ON street_estimates BEGIN
    SELECT RAISE(ABORT, 'street estimates are append-only');
END;

-- Which notes have been read, and what came of reading them.
--
-- Without this the lane cannot tell "not scanned yet" from "scanned and had no
-- target in it", and would re-read the same 177 industry reports every day
-- forever. A refusal is recorded with its reason for the same purpose the
-- refusals are returned rather than dropped everywhere else here: a figure
-- that silently never appears is indistinguishable from one that was never
-- looked for.
CREATE TABLE IF NOT EXISTS street_estimate_document_scans (
    scan_id TEXT PRIMARY KEY,
    company_ref TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('recorded','refused')),
    reason TEXT,
    estimate_id TEXT REFERENCES street_estimates(estimate_id),
    extraction_method TEXT NOT NULL,
    -- Which extractor said so. Without it a refusal recorded by a version of
    -- the reader that has since been fixed is indistinguishable from one that
    -- would be refused again, and the scan ledger's whole purpose -- never
    -- read the same note twice -- would make the fix unreachable.
    extractor_ref TEXT NOT NULL,
    scanned_at TEXT NOT NULL,
    UNIQUE(company_ref, document_ref)
);

CREATE INDEX IF NOT EXISTS street_estimate_scans_by_company
ON street_estimate_document_scans(company_ref, scanned_at);

CREATE TRIGGER IF NOT EXISTS street_estimate_scans_insert_guard
BEFORE INSERT ON street_estimate_document_scans WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'street estimate scan insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS street_estimate_scans_no_update
BEFORE UPDATE ON street_estimate_document_scans BEGIN
    SELECT RAISE(ABORT, 'street estimate scans are append-only');
END;
CREATE TRIGGER IF NOT EXISTS street_estimate_scans_no_delete
BEFORE DELETE ON street_estimate_document_scans BEGIN
    SELECT RAISE(ABORT, 'street estimate scans are append-only');
END;
