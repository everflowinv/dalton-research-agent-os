-- P11b: what the street expects of one company, as an append-only chain.
--
-- Shaped after ``market_price_series_versions`` on purpose. A consensus number
-- is the same kind of thing as a price: it moves under you, nobody restates it
-- in public, and a valuation that cited "the street expects $14.66" last week
-- has to stay reproducible after the street changes its mind. So the estimate
-- is versioned rather than maintained, a version is published only when some
-- value it carries is different, and no row is ever updated or deleted.
--
-- ``source_kind`` says which of the two routes produced this version: the
-- vendor observation (yfinance) or the corroborated sell-side reports already
-- in the ledger. Both live on one chain because they are answers to the same
-- question, and a reader that wants only one of them can filter.
CREATE TABLE IF NOT EXISTS consensus_estimate_versions (
    version_id TEXT PRIMARY KEY,
    consensus_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES consensus_estimate_versions(version_id),
    company_ref TEXT NOT NULL,
    ticker TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK(
        source_kind IN ('vendor_observation','report_consensus')),
    change_reason TEXT NOT NULL,
    currency TEXT,
    as_of TEXT NOT NULL,
    target_price_mean TEXT,
    analyst_count INTEGER,
    eps_period_count INTEGER NOT NULL CHECK(eps_period_count >= 0),
    revenue_period_count INTEGER NOT NULL CHECK(revenue_period_count >= 0),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(consensus_ref, version_number)
);

CREATE INDEX IF NOT EXISTS consensus_estimate_by_company
ON consensus_estimate_versions(company_ref, version_number DESC);

CREATE TRIGGER IF NOT EXISTS consensus_estimate_insert_guard
BEFORE INSERT ON consensus_estimate_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'consensus estimate insert requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS consensus_estimate_no_update
BEFORE UPDATE ON consensus_estimate_versions BEGIN
    SELECT RAISE(ABORT, 'consensus estimate versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS consensus_estimate_no_delete
BEFORE DELETE ON consensus_estimate_versions BEGIN
    SELECT RAISE(ABORT, 'consensus estimate versions are immutable');
END;
