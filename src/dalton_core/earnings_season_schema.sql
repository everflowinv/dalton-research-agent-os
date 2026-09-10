-- P14f: what the earnings-season workflow did for one occurrence.
--
-- The deliverable carries the prose a person reads; this table carries the
-- numbers underneath it, because a forecast figure has no Claim behind it and
-- the deliverable authority -- correctly -- refuses a figure in its prose that
-- no live Claim accounts for.  Our own estimate is not a Claim and never will
-- be: it is a cell in an append-only ForecastModelVersion, named by its
-- ``cell_ref``.  So the comparison table lives here, every row naming the ref
-- its number came from, and the deliverable quotes only what a Claim covers.
--
-- ``occurrence_key`` is C1's ``calendar_event_key``: one company, one
-- occurrence, one window, one date as well as it was known.  The UNIQUE is the
-- whole of the idempotency rule -- a preview window stays open for thirty days
-- and this lane runs daily, so something other than a coordinator's memory has
-- to say "already done".  A date that genuinely moves is a different key and
-- therefore a second preview, which is the behaviour we want.
CREATE TABLE IF NOT EXISTS earnings_season_records (
    record_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('earnings_preview','earnings_calibration')),
    occurrence_key TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    entry_ref TEXT NOT NULL,
    expected_date TEXT NOT NULL,
    date_confidence TEXT NOT NULL CHECK(date_confidence IN ('confirmed','estimated')),
    -- The deliverable this record's prose was published as, when it was.  A
    -- record whose deliverable was refused still exists: the deterministic
    -- half ran and is worth keeping, and an operator needs to see that the
    -- prose is what failed rather than the arithmetic.
    deliverable_version_ref TEXT,
    mission_version_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(kind, occurrence_key)
);

CREATE INDEX IF NOT EXISTS earnings_season_by_company
ON earnings_season_records(company_ref, expected_date DESC, record_id);

CREATE TRIGGER IF NOT EXISTS earnings_season_insert_guard
BEFORE INSERT ON earnings_season_records WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'earnings season insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS earnings_season_no_update
BEFORE UPDATE ON earnings_season_records BEGIN
    SELECT RAISE(ABORT, 'earnings season records are immutable');
END;
CREATE TRIGGER IF NOT EXISTS earnings_season_no_delete
BEFORE DELETE ON earnings_season_records BEGIN
    SELECT RAISE(ABORT, 'earnings season records are immutable');
END;
