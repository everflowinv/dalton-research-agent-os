CREATE TABLE IF NOT EXISTS market_price_series_versions (
    version_id TEXT PRIMARY KEY,
    series_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES market_price_series_versions(version_id),
    company_ref TEXT NOT NULL,
    ticker TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    currency TEXT NOT NULL,
    first_bar_date TEXT NOT NULL,
    last_bar_date TEXT NOT NULL,
    bar_count INTEGER NOT NULL CHECK(bar_count >= 1),
    added_bar_count INTEGER NOT NULL CHECK(added_bar_count >= 0),
    restated_bar_count INTEGER NOT NULL CHECK(restated_bar_count >= 0),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(series_ref, version_number)
);

CREATE INDEX IF NOT EXISTS market_price_series_by_company
ON market_price_series_versions(company_ref, version_number DESC);

CREATE TRIGGER IF NOT EXISTS market_price_series_insert_guard
BEFORE INSERT ON market_price_series_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'market price series insert requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS market_price_series_no_update
BEFORE UPDATE ON market_price_series_versions BEGIN
    SELECT RAISE(ABORT, 'market price series versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS market_price_series_no_delete
BEFORE DELETE ON market_price_series_versions BEGIN
    SELECT RAISE(ABORT, 'market price series versions are immutable');
END;
