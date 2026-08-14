PRAGMA foreign_keys = ON;

-- Host-owned exporter retry state. This is not a Dalton research authority;
-- the CapabilityCatalog independently enforces the monotonic source chain.
CREATE TABLE IF NOT EXISTS openclaw_exporter_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    source_instance_ref TEXT NOT NULL,
    acknowledged_generation INTEGER NOT NULL CHECK (acknowledged_generation >= 0),
    acknowledged_snapshot_ref TEXT,
    acknowledged_snapshot_hash TEXT,
    pending_generation INTEGER,
    pending_snapshot_ref TEXT,
    pending_snapshot_hash TEXT,
    pending_snapshot_json TEXT,
    updated_at TEXT NOT NULL,
    CHECK (
        (acknowledged_snapshot_ref IS NULL) = (acknowledged_snapshot_hash IS NULL)
    ),
    CHECK (
        (acknowledged_generation = 0 AND acknowledged_snapshot_ref IS NULL)
        OR (acknowledged_generation > 0 AND acknowledged_snapshot_ref IS NOT NULL)
    ),
    CHECK (
        (pending_generation IS NULL)
        = (pending_snapshot_ref IS NULL)
    ),
    CHECK (
        (pending_snapshot_ref IS NULL)
        = (pending_snapshot_hash IS NULL)
    ),
    CHECK (
        (pending_snapshot_hash IS NULL)
        = (pending_snapshot_json IS NULL)
    ),
    CHECK (
        pending_generation IS NULL
        OR pending_generation = acknowledged_generation + 1
    )
);
