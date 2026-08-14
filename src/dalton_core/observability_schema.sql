-- Append-only observability authorities. These tables are source facts for a
-- later read-only dashboard projection; they are not materialized UI state.

CREATE TABLE IF NOT EXISTS observability_workflow_versions (
    version_id TEXT PRIMARY KEY,
    workflow_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    prior_version_id TEXT REFERENCES observability_workflow_versions(version_id),
    root_work_order_refs_json TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (workflow_ref, version_number)
);

CREATE TABLE IF NOT EXISTS observability_work_order_links (
    link_id TEXT PRIMARY KEY,
    workflow_ref TEXT NOT NULL,
    parent_work_order_ref TEXT NOT NULL,
    child_work_order_ref TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('decomposed_from', 'verifies', 'follows_up')),
    sequence_number INTEGER NOT NULL CHECK (sequence_number >= 0),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (parent_work_order_ref <> child_work_order_ref),
    UNIQUE (workflow_ref, child_work_order_ref)
);

CREATE TABLE IF NOT EXISTS observability_usage_entries (
    usage_entry_id TEXT PRIMARY KEY,
    invocation_ref TEXT NOT NULL REFERENCES model_invocations(invocation_id),
    work_order_ref TEXT NOT NULL,
    workflow_ref TEXT,
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    correction_of_ref TEXT REFERENCES observability_usage_entries(usage_entry_id),
    occurred_at TEXT NOT NULL,
    metering_source TEXT NOT NULL CHECK (metering_source IN ('provider_reported', 'launcher_measured', 'worker_reported', 'estimated')),
    measurement_status TEXT NOT NULL CHECK (measurement_status IN ('final', 'partial', 'estimated', 'unavailable')),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (invocation_ref, revision_number)
);

CREATE TABLE IF NOT EXISTS observability_price_rate_versions (
    version_id TEXT PRIMARY KEY,
    price_rate_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    prior_version_id TEXT REFERENCES observability_price_rate_versions(version_id),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    charge_type TEXT NOT NULL,
    unit_quantity INTEGER NOT NULL CHECK (unit_quantity > 0),
    unit_price_micros INTEGER NOT NULL CHECK (unit_price_micros >= 0),
    currency TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_until TEXT,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (price_rate_ref, version_number)
);

CREATE TABLE IF NOT EXISTS observability_cost_entries (
    cost_entry_id TEXT PRIMARY KEY,
    usage_entry_ref TEXT NOT NULL REFERENCES observability_usage_entries(usage_entry_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    correction_of_ref TEXT REFERENCES observability_cost_entries(cost_entry_id),
    amount_micros INTEGER CHECK (amount_micros IS NULL OR amount_micros >= 0),
    currency TEXT NOT NULL,
    cost_status TEXT NOT NULL CHECK (cost_status IN ('actual', 'estimated', 'unpriced', 'waived')),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (usage_entry_ref, revision_number)
);

CREATE TABLE IF NOT EXISTS observability_artifact_versions (
    version_id TEXT PRIMARY KEY,
    artifact_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    prior_version_id TEXT REFERENCES observability_artifact_versions(version_id),
    artifact_content_hash TEXT NOT NULL,
    producer_invocation_ref TEXT NOT NULL REFERENCES model_invocations(invocation_id),
    work_order_ref TEXT NOT NULL,
    result_envelope_ref TEXT NOT NULL,
    result_envelope_hash TEXT NOT NULL,
    storage_locator TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (artifact_ref, version_number)
);

-- ArtifactVersion v0.2 uses the runtime-neutral execution supertype.  The
-- v0.1 table stays immutable so historical hashes and foreign keys do not
-- need to be rewritten during the additive migration.
CREATE TABLE IF NOT EXISTS observability_artifact_versions_v2 (
    version_id TEXT PRIMARY KEY,
    artifact_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    prior_version_ref TEXT,
    artifact_content_hash TEXT NOT NULL,
    producer_execution_ref TEXT NOT NULL REFERENCES execution_invocations(execution_id),
    work_order_ref TEXT NOT NULL,
    result_envelope_ref TEXT NOT NULL,
    result_envelope_hash TEXT NOT NULL,
    storage_locator TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (artifact_ref, version_number)
);

-- Cross-generation identity authority.  Both v0.1 and v0.2 must claim this
-- index in the same transaction, so an artifact cannot fork or reuse a
-- version number across physical subtype tables.
CREATE TABLE IF NOT EXISTS observability_artifact_version_index (
    version_id TEXT PRIMARY KEY,
    artifact_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    schema_version TEXT NOT NULL CHECK (schema_version IN ('0.1', '0.2')),
    prior_version_ref TEXT REFERENCES observability_artifact_version_index(version_id),
    producer_execution_ref TEXT NOT NULL REFERENCES execution_invocations(execution_id),
    record_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (artifact_ref, version_number)
);

CREATE TABLE IF NOT EXISTS observability_idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- dalton_authorized() is a trusted-process integrity guardrail. It is not a
-- same-UID hostile-process security boundary and must not be presented as one.
CREATE TRIGGER IF NOT EXISTS observability_workflow_versions_authorized_insert
BEFORE INSERT ON observability_workflow_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'workflow version insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS observability_work_order_links_authorized_insert
BEFORE INSERT ON observability_work_order_links WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'work-order link insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS observability_usage_entries_authorized_insert
BEFORE INSERT ON observability_usage_entries WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'usage entry insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS observability_price_rate_versions_authorized_insert
BEFORE INSERT ON observability_price_rate_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'price-rate version insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS observability_cost_entries_authorized_insert
BEFORE INSERT ON observability_cost_entries WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'cost entry insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS observability_artifact_versions_authorized_insert
BEFORE INSERT ON observability_artifact_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'artifact version insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS observability_artifact_versions_v2_authorized_insert
BEFORE INSERT ON observability_artifact_versions_v2 WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'artifact version v0.2 insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS observability_artifact_version_index_authorized_insert
BEFORE INSERT ON observability_artifact_version_index WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'artifact version index insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS observability_idempotency_keys_authorized_insert
BEFORE INSERT ON observability_idempotency_keys WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'observability idempotency insert requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS observability_workflow_versions_no_update
BEFORE UPDATE ON observability_workflow_versions BEGIN
    SELECT RAISE(ABORT, 'workflow versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS observability_workflow_versions_no_delete
BEFORE DELETE ON observability_workflow_versions BEGIN
    SELECT RAISE(ABORT, 'workflow versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS observability_work_order_links_no_update
BEFORE UPDATE ON observability_work_order_links BEGIN
    SELECT RAISE(ABORT, 'work-order links are immutable');
END;
CREATE TRIGGER IF NOT EXISTS observability_work_order_links_no_delete
BEFORE DELETE ON observability_work_order_links BEGIN
    SELECT RAISE(ABORT, 'work-order links are immutable');
END;
CREATE TRIGGER IF NOT EXISTS observability_usage_entries_no_update
BEFORE UPDATE ON observability_usage_entries BEGIN
    SELECT RAISE(ABORT, 'usage entries are immutable');
END;
CREATE TRIGGER IF NOT EXISTS observability_usage_entries_no_delete
BEFORE DELETE ON observability_usage_entries BEGIN
    SELECT RAISE(ABORT, 'usage entries are immutable');
END;
CREATE TRIGGER IF NOT EXISTS observability_price_rate_versions_no_update
BEFORE UPDATE ON observability_price_rate_versions BEGIN
    SELECT RAISE(ABORT, 'price-rate versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS observability_price_rate_versions_no_delete
BEFORE DELETE ON observability_price_rate_versions BEGIN
    SELECT RAISE(ABORT, 'price-rate versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS observability_cost_entries_no_update
BEFORE UPDATE ON observability_cost_entries BEGIN
    SELECT RAISE(ABORT, 'cost entries are immutable');
END;
CREATE TRIGGER IF NOT EXISTS observability_cost_entries_no_delete
BEFORE DELETE ON observability_cost_entries BEGIN
    SELECT RAISE(ABORT, 'cost entries are immutable');
END;
CREATE TRIGGER IF NOT EXISTS observability_artifact_versions_no_update
BEFORE UPDATE ON observability_artifact_versions BEGIN
    SELECT RAISE(ABORT, 'artifact versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS observability_artifact_versions_no_delete
BEFORE DELETE ON observability_artifact_versions BEGIN
    SELECT RAISE(ABORT, 'artifact versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS observability_artifact_versions_v2_no_update
BEFORE UPDATE ON observability_artifact_versions_v2 BEGIN
    SELECT RAISE(ABORT, 'artifact versions v0.2 are immutable');
END;
CREATE TRIGGER IF NOT EXISTS observability_artifact_versions_v2_no_delete
BEFORE DELETE ON observability_artifact_versions_v2 BEGIN
    SELECT RAISE(ABORT, 'artifact versions v0.2 are immutable');
END;
CREATE TRIGGER IF NOT EXISTS observability_artifact_version_index_no_update
BEFORE UPDATE ON observability_artifact_version_index BEGIN
    SELECT RAISE(ABORT, 'artifact version index is immutable');
END;
CREATE TRIGGER IF NOT EXISTS observability_artifact_version_index_no_delete
BEFORE DELETE ON observability_artifact_version_index BEGIN
    SELECT RAISE(ABORT, 'artifact version index is immutable');
END;
CREATE TRIGGER IF NOT EXISTS observability_idempotency_keys_no_update
BEFORE UPDATE ON observability_idempotency_keys BEGIN
    SELECT RAISE(ABORT, 'observability idempotency keys are immutable');
END;
CREATE TRIGGER IF NOT EXISTS observability_idempotency_keys_no_delete
BEFORE DELETE ON observability_idempotency_keys BEGIN
    SELECT RAISE(ABORT, 'observability idempotency keys are immutable');
END;
