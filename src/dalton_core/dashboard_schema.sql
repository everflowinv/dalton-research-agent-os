PRAGMA foreign_keys = ON;

-- This database is a disposable, read-only dashboard projection.  It is not
-- an authority and may be rebuilt from Dalton's append-only stores at any
-- time.  The dashboard process opens it with SQLite mode=ro and never receives
-- a Core writer token or an authority database path.
CREATE TABLE IF NOT EXISTS projection_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version TEXT NOT NULL,
    as_of TEXT NOT NULL,
    source_watermark TEXT NOT NULL,
    build_state TEXT NOT NULL CHECK (build_state IN ('ready', 'building', 'failed')),
    partial_data INTEGER NOT NULL CHECK (partial_data IN (0, 1)),
    warnings_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_summaries (
    workflow_ref TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    display_status TEXT NOT NULL,
    source_state TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    status_reason TEXT NOT NULL,
    total_tasks INTEGER NOT NULL CHECK (total_tasks >= 0),
    completed_tasks INTEGER NOT NULL CHECK (completed_tasks >= 0),
    running_tasks INTEGER NOT NULL CHECK (running_tasks >= 0),
    failed_tasks INTEGER NOT NULL CHECK (failed_tasks >= 0),
    total_tokens INTEGER,
    artifact_count INTEGER NOT NULL CHECK (artifact_count >= 0),
    recent_activity TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_items (
    work_order_ref TEXT PRIMARY KEY,
    workflow_ref TEXT NOT NULL REFERENCES workflow_summaries(workflow_ref),
    parent_work_order_ref TEXT REFERENCES work_items(work_order_ref),
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    title TEXT NOT NULL,
    question TEXT NOT NULL,
    display_status TEXT NOT NULL,
    source_state TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    status_reason TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    model_count INTEGER NOT NULL CHECK (model_count >= 0),
    total_tokens INTEGER,
    artifact_count INTEGER NOT NULL CHECK (artifact_count >= 0),
    latest_result_ref TEXT,
    latest_error_summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invocation_slices (
    invocation_ref TEXT PRIMARY KEY,
    workflow_ref TEXT REFERENCES workflow_summaries(workflow_ref),
    work_order_ref TEXT NOT NULL REFERENCES work_items(work_order_ref),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    model_family TEXT NOT NULL,
    profile_ref TEXT NOT NULL,
    runtime_ref TEXT NOT NULL,
    capability TEXT NOT NULL,
    granularity TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    total_tokens INTEGER,
    metering_source TEXT NOT NULL,
    measurement_status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_slices (
    cost_entry_ref TEXT PRIMARY KEY,
    invocation_ref TEXT NOT NULL REFERENCES invocation_slices(invocation_ref),
    workflow_ref TEXT REFERENCES workflow_summaries(workflow_ref),
    work_order_ref TEXT NOT NULL REFERENCES work_items(work_order_ref),
    amount_micros INTEGER,
    currency TEXT NOT NULL,
    cost_status TEXT NOT NULL,
    price_rate_ref TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact_index (
    artifact_ref TEXT PRIMARY KEY,
    workflow_ref TEXT REFERENCES workflow_summaries(workflow_ref),
    work_order_ref TEXT NOT NULL REFERENCES work_items(work_order_ref),
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    content_hash TEXT NOT NULL,
    access_class TEXT NOT NULL,
    preview_status TEXT NOT NULL,
    producer_invocation_ref TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capability_status (
    capability_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_type TEXT NOT NULL,
    eligibility_state TEXT NOT NULL,
    active_revision_ref TEXT,
    decision_state TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_status (
    profile_ref TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    model_family TEXT NOT NULL,
    availability TEXT NOT NULL,
    auth_state TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    context_window INTEGER,
    cost_class TEXT NOT NULL,
    last_used_at TEXT,
    total_tokens INTEGER
);

CREATE INDEX IF NOT EXISTS work_items_workflow_idx
ON work_items(workflow_ref, sequence, work_order_ref);
CREATE INDEX IF NOT EXISTS invocation_workflow_idx
ON invocation_slices(workflow_ref, started_at, invocation_ref);
CREATE INDEX IF NOT EXISTS invocation_model_idx
ON invocation_slices(provider, model, started_at);
CREATE INDEX IF NOT EXISTS cost_workflow_idx
ON cost_slices(workflow_ref, currency, created_at);
CREATE INDEX IF NOT EXISTS artifact_workflow_idx
ON artifact_index(workflow_ref, created_at);
