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

CREATE TABLE IF NOT EXISTS external_capability_import_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    import_generation INTEGER NOT NULL CHECK (import_generation >= 0)
);
INSERT OR IGNORE INTO external_capability_import_state(singleton, import_generation)
VALUES(1, 0);

CREATE TABLE IF NOT EXISTS external_capability_snapshots (
    snapshot_ref TEXT PRIMARY KEY,
    snapshot_hash TEXT NOT NULL UNIQUE,
    producer_version TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS external_capability_metadata_versions (
    metadata_ref TEXT PRIMARY KEY,
    capability_id TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('skill', 'mcp')),
    source_scope TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_version TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    schema_hash TEXT NOT NULL,
    metadata_hash TEXT NOT NULL UNIQUE,
    metadata_json TEXT NOT NULL,
    snapshot_ref TEXT NOT NULL REFERENCES external_capability_snapshots(snapshot_ref),
    created_at TEXT NOT NULL,
    UNIQUE(capability_id, metadata_hash)
);

CREATE TABLE IF NOT EXISTS external_capability_metadata_current (
    capability_id TEXT PRIMARY KEY,
    metadata_ref TEXT NOT NULL UNIQUE
        REFERENCES external_capability_metadata_versions(metadata_ref),
    source_scope TEXT NOT NULL,
    source_key TEXT NOT NULL,
    metadata_hash TEXT NOT NULL,
    snapshot_ref TEXT NOT NULL,
    UNIQUE(source_scope, source_key)
);

CREATE TABLE IF NOT EXISTS external_schema_artifacts (
    schema_ref TEXT NOT NULL,
    schema_hash TEXT NOT NULL,
    schema_json TEXT NOT NULL,
    snapshot_ref TEXT NOT NULL REFERENCES external_capability_snapshots(snapshot_ref),
    created_at TEXT NOT NULL,
    PRIMARY KEY(schema_ref, schema_hash)
);

CREATE TABLE IF NOT EXISTS external_capability_sync_events (
    event_ref TEXT PRIMARY KEY,
    snapshot_ref TEXT NOT NULL REFERENCES external_capability_snapshots(snapshot_ref),
    capability_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('added', 'unchanged', 'changed', 'removed')),
    prior_metadata_ref TEXT,
    metadata_ref TEXT,
    catalog_epoch INTEGER NOT NULL CHECK (catalog_epoch >= 0),
    event_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(snapshot_ref, capability_id)
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
CREATE TRIGGER IF NOT EXISTS external_snapshot_no_update
BEFORE UPDATE ON external_capability_snapshots BEGIN
    SELECT RAISE(ABORT, 'external capability snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS external_snapshot_no_delete
BEFORE DELETE ON external_capability_snapshots BEGIN
    SELECT RAISE(ABORT, 'external capability snapshots are append-only');
END;
CREATE TRIGGER IF NOT EXISTS external_metadata_no_update
BEFORE UPDATE ON external_capability_metadata_versions BEGIN
    SELECT RAISE(ABORT, 'external capability metadata versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS external_metadata_no_delete
BEFORE DELETE ON external_capability_metadata_versions BEGIN
    SELECT RAISE(ABORT, 'external capability metadata versions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS external_schema_no_update
BEFORE UPDATE ON external_schema_artifacts BEGIN
    SELECT RAISE(ABORT, 'external schema artifacts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS external_schema_no_delete
BEFORE DELETE ON external_schema_artifacts BEGIN
    SELECT RAISE(ABORT, 'external schema artifacts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS external_sync_event_no_update
BEFORE UPDATE ON external_capability_sync_events BEGIN
    SELECT RAISE(ABORT, 'external capability sync events are immutable');
END;
CREATE TRIGGER IF NOT EXISTS external_sync_event_no_delete
BEFORE DELETE ON external_capability_sync_events BEGIN
    SELECT RAISE(ABORT, 'external capability sync events are append-only');
END;
