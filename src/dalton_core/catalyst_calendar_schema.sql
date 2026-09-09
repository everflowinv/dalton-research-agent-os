CREATE TABLE IF NOT EXISTS catalyst_calendar_versions (
    version_id TEXT PRIMARY KEY,
    calendar_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES catalyst_calendar_versions(version_id),
    company_ref TEXT NOT NULL,
    entry_count INTEGER NOT NULL CHECK(entry_count >= 0),
    change_reason TEXT NOT NULL,
    -- The soonest entry that has not happened yet, as of the moment this
    -- version was published.  A column rather than a computed read because
    -- ``upcoming`` scans every company and a JSON scan per company is the
    -- kind of thing that only becomes a problem once the universe grows.
    -- Null when this version holds nothing forthcoming.
    next_catalyst_date TEXT,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(calendar_ref, version_number)
);

CREATE INDEX IF NOT EXISTS catalyst_calendar_by_company
ON catalyst_calendar_versions(company_ref, version_number DESC);

CREATE INDEX IF NOT EXISTS catalyst_calendar_by_next_date
ON catalyst_calendar_versions(next_catalyst_date);

CREATE TRIGGER IF NOT EXISTS catalyst_calendar_insert_guard
BEFORE INSERT ON catalyst_calendar_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'catalyst calendar insert requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS catalyst_calendar_no_update
BEFORE UPDATE ON catalyst_calendar_versions BEGIN
    SELECT RAISE(ABORT, 'catalyst calendar versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS catalyst_calendar_no_delete
BEFORE DELETE ON catalyst_calendar_versions BEGIN
    SELECT RAISE(ABORT, 'catalyst calendar versions are immutable');
END;
