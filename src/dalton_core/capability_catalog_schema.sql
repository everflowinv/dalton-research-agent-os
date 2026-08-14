PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS capability_catalog_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    catalog_epoch INTEGER NOT NULL CHECK (catalog_epoch >= 0)
);
INSERT OR IGNORE INTO capability_catalog_meta(singleton, catalog_epoch) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS capability_descriptor_versions (
    revision_ref TEXT PRIMARY KEY,
    capability_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    catalog_epoch INTEGER NOT NULL CHECK (catalog_epoch > 0),
    descriptor_hash TEXT NOT NULL UNIQUE,
    approval_ref TEXT NOT NULL,
    approval_hash TEXT NOT NULL,
    registry_revision_ref TEXT NOT NULL,
    descriptor_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(capability_id, version)
);

CREATE TABLE IF NOT EXISTS capability_current (
    capability_id TEXT PRIMARY KEY,
    revision_ref TEXT NOT NULL UNIQUE REFERENCES capability_descriptor_versions(revision_ref),
    version INTEGER NOT NULL,
    catalog_epoch INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS capability_leases (
    lease_id TEXT PRIMARY KEY,
    capability_id TEXT NOT NULL,
    revision_ref TEXT NOT NULL REFERENCES capability_descriptor_versions(revision_ref),
    work_order_ref TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    lease_hash TEXT NOT NULL UNIQUE,
    lease_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS capability_descriptor_no_update
BEFORE UPDATE ON capability_descriptor_versions BEGIN
    SELECT RAISE(ABORT, 'capability descriptor versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS capability_descriptor_no_delete
BEFORE DELETE ON capability_descriptor_versions BEGIN
    SELECT RAISE(ABORT, 'capability descriptor versions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS capability_lease_no_update
BEFORE UPDATE ON capability_leases BEGIN
    SELECT RAISE(ABORT, 'capability leases are immutable');
END;
CREATE TRIGGER IF NOT EXISTS capability_lease_no_delete
BEFORE DELETE ON capability_leases BEGIN
    SELECT RAISE(ABORT, 'capability leases are append-only');
END;
