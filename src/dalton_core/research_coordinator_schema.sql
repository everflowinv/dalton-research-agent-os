PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS compiled_connector_plans (
    plan_id TEXT PRIMARY KEY,
    plan_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS context_packs (
    context_pack_id TEXT PRIMARY KEY,
    context_pack_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claim_indexes (
    claim_index_id TEXT PRIMARY KEY,
    claim_index_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Connector request and completion authority is persisted separately from the
-- rebuildable checkpoint projection.  A resolver must be able to re-read the
-- exact request/receipt pair after a coordinator restart; caller-provided
-- maps are not execution authority.
CREATE TABLE IF NOT EXISTS research_runner_requests (
    runner_request_id TEXT PRIMARY KEY,
    request_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_completion_receipts (
    receipt_id TEXT PRIMARY KEY,
    receipt_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    run_ref TEXT NOT NULL,
    sequence_number INTEGER NOT NULL,
    checkpoint_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    prior_checkpoint_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_ref, sequence_number),
    FOREIGN KEY(prior_checkpoint_id) REFERENCES research_checkpoints(checkpoint_id)
);

CREATE TABLE IF NOT EXISTS research_run_state_versions (
    run_state_id TEXT PRIMARY KEY,
    run_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    prior_version_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_ref, version_number),
    FOREIGN KEY(prior_version_id) REFERENCES research_run_state_versions(run_state_id)
);

CREATE TRIGGER IF NOT EXISTS compiled_connector_plans_no_update
BEFORE UPDATE ON compiled_connector_plans BEGIN SELECT RAISE(ABORT, 'compiled_connector_plans is immutable'); END;
CREATE TRIGGER IF NOT EXISTS compiled_connector_plans_no_delete
BEFORE DELETE ON compiled_connector_plans BEGIN SELECT RAISE(ABORT, 'compiled_connector_plans is immutable'); END;
CREATE TRIGGER IF NOT EXISTS context_packs_no_update
BEFORE UPDATE ON context_packs BEGIN SELECT RAISE(ABORT, 'context_packs is immutable'); END;
CREATE TRIGGER IF NOT EXISTS context_packs_no_delete
BEFORE DELETE ON context_packs BEGIN SELECT RAISE(ABORT, 'context_packs is immutable'); END;
CREATE TRIGGER IF NOT EXISTS claim_indexes_no_update
BEFORE UPDATE ON claim_indexes BEGIN SELECT RAISE(ABORT, 'claim_indexes is immutable'); END;
CREATE TRIGGER IF NOT EXISTS claim_indexes_no_delete
BEFORE DELETE ON claim_indexes BEGIN SELECT RAISE(ABORT, 'claim_indexes is immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_runner_requests_no_update
BEFORE UPDATE ON research_runner_requests BEGIN SELECT RAISE(ABORT, 'research_runner_requests is immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_runner_requests_no_delete
BEFORE DELETE ON research_runner_requests BEGIN SELECT RAISE(ABORT, 'research_runner_requests is immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_completion_receipts_no_update
BEFORE UPDATE ON research_completion_receipts BEGIN SELECT RAISE(ABORT, 'research_completion_receipts is immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_completion_receipts_no_delete
BEFORE DELETE ON research_completion_receipts BEGIN SELECT RAISE(ABORT, 'research_completion_receipts is immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_checkpoints_no_update
BEFORE UPDATE ON research_checkpoints BEGIN SELECT RAISE(ABORT, 'research_checkpoints is immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_checkpoints_no_delete
BEFORE DELETE ON research_checkpoints BEGIN SELECT RAISE(ABORT, 'research_checkpoints is immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_run_state_versions_no_update
BEFORE UPDATE ON research_run_state_versions BEGIN SELECT RAISE(ABORT, 'research_run_state_versions is immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_run_state_versions_no_delete
BEFORE DELETE ON research_run_state_versions BEGIN SELECT RAISE(ABORT, 'research_run_state_versions is immutable'); END;
