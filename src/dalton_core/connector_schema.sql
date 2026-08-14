-- Connector Fabric authority.  All rows are immutable; current quota,
-- incident, and source-health state is projected from append-only facts.

CREATE TABLE IF NOT EXISTS connector_profile_versions (
    profile_version_id TEXT PRIMARY KEY,
    connector_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    prior_version_ref TEXT REFERENCES connector_profile_versions(profile_version_id),
    capability_id TEXT NOT NULL,
    descriptor_revision_ref TEXT NOT NULL,
    descriptor_hash TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    schema_hash TEXT NOT NULL,
    catalog_epoch INTEGER NOT NULL CHECK (catalog_epoch > 0),
    adapter_ref TEXT NOT NULL,
    adapter_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (connector_ref, version_number)
);

CREATE TABLE IF NOT EXISTS connector_call_specs (
    call_spec_id TEXT PRIMARY KEY,
    work_order_ref TEXT NOT NULL,
    work_order_hash TEXT NOT NULL,
    connector_profile_ref TEXT NOT NULL REFERENCES connector_profile_versions(profile_version_id),
    operation TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connector_invocations (
    connector_invocation_id TEXT PRIMARY KEY,
    execution_ref TEXT NOT NULL UNIQUE REFERENCES execution_invocations(execution_id),
    execution_hash TEXT NOT NULL,
    work_order_ref TEXT NOT NULL,
    work_order_hash TEXT NOT NULL,
    connector_profile_ref TEXT NOT NULL REFERENCES connector_profile_versions(profile_version_id),
    connector_profile_hash TEXT NOT NULL,
    call_spec_ref TEXT NOT NULL REFERENCES connector_call_specs(call_spec_id),
    call_spec_hash TEXT NOT NULL,
    capability_lease_ref TEXT NOT NULL,
    capability_lease_hash TEXT NOT NULL,
    descriptor_revision_ref TEXT NOT NULL,
    catalog_epoch INTEGER NOT NULL CHECK (catalog_epoch > 0),
    logical_invocation_key TEXT NOT NULL UNIQUE,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connector_rate_policy_versions (
    policy_version_id TEXT PRIMARY KEY,
    policy_ref TEXT NOT NULL,
    quota_scope_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    prior_version_ref TEXT REFERENCES connector_rate_policy_versions(policy_version_id),
    connector_profile_ref TEXT NOT NULL REFERENCES connector_profile_versions(profile_version_id),
    window_seconds INTEGER NOT NULL CHECK (window_seconds > 0),
    max_concurrency INTEGER NOT NULL CHECK (max_concurrency > 0),
    quota_currency TEXT NOT NULL,
    price_book_hash TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_until TEXT,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (policy_ref, version_number)
);

CREATE TABLE IF NOT EXISTS connector_rate_policy_activation_events (
    event_id TEXT PRIMARY KEY,
    policy_ref TEXT NOT NULL,
    quota_scope_ref TEXT NOT NULL,
    policy_version_ref TEXT NOT NULL REFERENCES connector_rate_policy_versions(policy_version_id),
    policy_version_hash TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    prior_event_ref TEXT REFERENCES connector_rate_policy_activation_events(event_id),
    actor_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connector_quota_reservations (
    reservation_id TEXT PRIMARY KEY,
    connector_invocation_ref TEXT NOT NULL REFERENCES connector_invocations(connector_invocation_id),
    policy_version_ref TEXT NOT NULL REFERENCES connector_rate_policy_versions(policy_version_id),
    policy_version_hash TEXT NOT NULL,
    quota_scope_ref TEXT NOT NULL,
    physical_attempt_number INTEGER NOT NULL CHECK (physical_attempt_number > 0),
    window_started_at TEXT NOT NULL,
    window_ends_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    reserved_json TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (connector_invocation_ref, physical_attempt_number)
);

CREATE TABLE IF NOT EXISTS connector_physical_attempts (
    physical_attempt_id TEXT PRIMARY KEY,
    connector_invocation_ref TEXT NOT NULL REFERENCES connector_invocations(connector_invocation_id),
    physical_attempt_number INTEGER NOT NULL CHECK (physical_attempt_number > 0),
    reservation_ref TEXT NOT NULL UNIQUE REFERENCES connector_quota_reservations(reservation_id),
    outcome TEXT NOT NULL CHECK (outcome IN ('succeeded','rate_limited','timeout','failed','indeterminate')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    provider_request_id TEXT,
    retry_at TEXT,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (connector_invocation_ref, physical_attempt_number)
);

CREATE TABLE IF NOT EXISTS connector_usage_entries (
    usage_entry_id TEXT PRIMARY KEY,
    physical_attempt_ref TEXT NOT NULL REFERENCES connector_physical_attempts(physical_attempt_id),
    connector_invocation_ref TEXT NOT NULL REFERENCES connector_invocations(connector_invocation_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    correction_of_ref TEXT REFERENCES connector_usage_entries(usage_entry_id),
    measurement_status TEXT NOT NULL CHECK (measurement_status IN ('final','partial','estimated','unavailable')),
    metering_source TEXT NOT NULL CHECK (metering_source IN ('provider_reported','runner_measured','estimated')),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (physical_attempt_ref, revision_number)
);

CREATE TABLE IF NOT EXISTS connector_price_rate_versions (
    price_rate_version_id TEXT PRIMARY KEY,
    price_rate_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    prior_version_ref TEXT REFERENCES connector_price_rate_versions(price_rate_version_id),
    connector_profile_ref TEXT NOT NULL REFERENCES connector_profile_versions(profile_version_id),
    meter TEXT NOT NULL,
    unit_quantity INTEGER NOT NULL CHECK (unit_quantity > 0),
    unit_price_micros INTEGER NOT NULL CHECK (unit_price_micros >= 0),
    rounding_mode TEXT NOT NULL CHECK (rounding_mode IN ('ceiling')),
    currency TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_until TEXT,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (price_rate_ref, version_number)
);

CREATE TABLE IF NOT EXISTS connector_cost_entries (
    cost_entry_id TEXT PRIMARY KEY,
    usage_entry_ref TEXT NOT NULL REFERENCES connector_usage_entries(usage_entry_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    correction_of_ref TEXT REFERENCES connector_cost_entries(cost_entry_id),
    amount_micros INTEGER CHECK (amount_micros IS NULL OR amount_micros >= 0),
    currency TEXT NOT NULL,
    cost_status TEXT NOT NULL CHECK (cost_status IN ('actual','estimated','unpriced','waived')),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (usage_entry_ref, revision_number)
);

CREATE TABLE IF NOT EXISTS connector_quota_settlements (
    settlement_id TEXT PRIMARY KEY,
    reservation_ref TEXT NOT NULL REFERENCES connector_quota_reservations(reservation_id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    correction_of_ref TEXT REFERENCES connector_quota_settlements(settlement_id),
    state TEXT NOT NULL CHECK (state IN ('consumed','released','indeterminate')),
    usage_entry_ref TEXT REFERENCES connector_usage_entries(usage_entry_id),
    cost_entry_ref TEXT REFERENCES connector_cost_entries(cost_entry_id),
    actual_json TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (reservation_ref, revision_number)
);

CREATE TABLE IF NOT EXISTS connector_source_envelopes (
    source_envelope_id TEXT PRIMARY KEY,
    connector_invocation_ref TEXT NOT NULL REFERENCES connector_invocations(connector_invocation_id),
    connector_profile_ref TEXT NOT NULL REFERENCES connector_profile_versions(profile_version_id),
    raw_artifact_version_ref TEXT NOT NULL,
    raw_response_hash TEXT NOT NULL,
    completeness TEXT NOT NULL CHECK (completeness IN ('enumerated','ranked','partial','unknown')),
    status TEXT NOT NULL CHECK (status IN ('complete','partial','empty','error')),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connector_incidents (
    incident_id TEXT PRIMARY KEY,
    connector_profile_ref TEXT NOT NULL REFERENCES connector_profile_versions(profile_version_id),
    connector_invocation_ref TEXT REFERENCES connector_invocations(connector_invocation_id),
    reservation_ref TEXT REFERENCES connector_quota_reservations(reservation_id),
    incident_type TEXT NOT NULL CHECK (incident_type IN ('quota_drift','schema_drift','credential_auth','source_outage','policy_violation')),
    severity TEXT NOT NULL CHECK (severity IN ('warning','blocking')),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connector_incident_events (
    event_id TEXT PRIMARY KEY,
    incident_ref TEXT NOT NULL REFERENCES connector_incidents(incident_id),
    state TEXT NOT NULL CHECK (state IN ('opened','resolved')),
    prior_event_ref TEXT REFERENCES connector_incident_events(event_id),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connector_source_health_events (
    event_id TEXT PRIMARY KEY,
    connector_profile_ref TEXT NOT NULL REFERENCES connector_profile_versions(profile_version_id),
    connector_invocation_ref TEXT REFERENCES connector_invocations(connector_invocation_id),
    state TEXT NOT NULL CHECK (state IN ('healthy','degraded','open_circuit','recovered')),
    prior_event_ref TEXT REFERENCES connector_source_health_events(event_id),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connector_idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_connector_reservation_window
ON connector_quota_reservations(quota_scope_ref,window_started_at);
CREATE INDEX IF NOT EXISTS idx_connector_policy_activation
ON connector_rate_policy_activation_events(policy_ref,effective_at,event_id);
CREATE INDEX IF NOT EXISTS idx_connector_settlement_latest
ON connector_quota_settlements(reservation_ref,revision_number);
CREATE INDEX IF NOT EXISTS idx_connector_incident_latest
ON connector_incident_events(incident_ref,created_at,event_id);
CREATE INDEX IF NOT EXISTS idx_connector_health_latest
ON connector_source_health_events(connector_profile_ref,created_at,event_id);

-- The UDF is an integrity boundary for the trusted single writer, not a
-- hostile same-UID sandbox.
CREATE TRIGGER IF NOT EXISTS connector_profile_versions_authorized_insert BEFORE INSERT ON connector_profile_versions WHEN dalton_authorized()=0 BEGIN SELECT RAISE(ABORT,'connector profile insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS connector_call_specs_authorized_insert BEFORE INSERT ON connector_call_specs WHEN dalton_authorized()=0 BEGIN SELECT RAISE(ABORT,'connector call spec insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS connector_invocations_authorized_insert BEFORE INSERT ON connector_invocations WHEN dalton_authorized()=0 BEGIN SELECT RAISE(ABORT,'connector invocation insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS connector_rate_policy_versions_authorized_insert BEFORE INSERT ON connector_rate_policy_versions WHEN dalton_authorized()=0 BEGIN SELECT RAISE(ABORT,'connector rate policy insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS connector_rate_policy_activation_events_authorized_insert BEFORE INSERT ON connector_rate_policy_activation_events WHEN dalton_authorized()=0 BEGIN SELECT RAISE(ABORT,'connector rate policy activation insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS connector_quota_reservations_authorized_insert BEFORE INSERT ON connector_quota_reservations WHEN dalton_authorized()=0 BEGIN SELECT RAISE(ABORT,'connector reservation insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS connector_physical_attempts_authorized_insert BEFORE INSERT ON connector_physical_attempts WHEN dalton_authorized()=0 BEGIN SELECT RAISE(ABORT,'connector attempt insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS connector_usage_entries_authorized_insert BEFORE INSERT ON connector_usage_entries WHEN dalton_authorized()=0 BEGIN SELECT RAISE(ABORT,'connector usage insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS connector_price_rate_versions_authorized_insert BEFORE INSERT ON connector_price_rate_versions WHEN dalton_authorized()=0 BEGIN SELECT RAISE(ABORT,'connector price rate insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS connector_cost_entries_authorized_insert BEFORE INSERT ON connector_cost_entries WHEN dalton_authorized()=0 BEGIN SELECT RAISE(ABORT,'connector cost insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS connector_quota_settlements_authorized_insert BEFORE INSERT ON connector_quota_settlements WHEN dalton_authorized()=0 BEGIN SELECT RAISE(ABORT,'connector settlement insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS connector_source_envelopes_authorized_insert BEFORE INSERT ON connector_source_envelopes WHEN dalton_authorized()=0 BEGIN SELECT RAISE(ABORT,'connector source envelope insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS connector_incidents_authorized_insert BEFORE INSERT ON connector_incidents WHEN dalton_authorized()=0 BEGIN SELECT RAISE(ABORT,'connector incident insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS connector_incident_events_authorized_insert BEFORE INSERT ON connector_incident_events WHEN dalton_authorized()=0 BEGIN SELECT RAISE(ABORT,'connector incident event insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS connector_source_health_events_authorized_insert BEFORE INSERT ON connector_source_health_events WHEN dalton_authorized()=0 BEGIN SELECT RAISE(ABORT,'connector health event insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS connector_idempotency_keys_authorized_insert BEFORE INSERT ON connector_idempotency_keys WHEN dalton_authorized()=0 BEGIN SELECT RAISE(ABORT,'connector idempotency insert requires DaltonStore'); END;

CREATE TRIGGER IF NOT EXISTS connector_profile_versions_no_update BEFORE UPDATE ON connector_profile_versions BEGIN SELECT RAISE(ABORT,'connector profiles are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_profile_versions_no_delete BEFORE DELETE ON connector_profile_versions BEGIN SELECT RAISE(ABORT,'connector profiles are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_call_specs_no_update BEFORE UPDATE ON connector_call_specs BEGIN SELECT RAISE(ABORT,'connector call specs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_call_specs_no_delete BEFORE DELETE ON connector_call_specs BEGIN SELECT RAISE(ABORT,'connector call specs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_invocations_no_update BEFORE UPDATE ON connector_invocations BEGIN SELECT RAISE(ABORT,'connector invocations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_invocations_no_delete BEFORE DELETE ON connector_invocations BEGIN SELECT RAISE(ABORT,'connector invocations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_rate_policy_versions_no_update BEFORE UPDATE ON connector_rate_policy_versions BEGIN SELECT RAISE(ABORT,'connector rate policies are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_rate_policy_versions_no_delete BEFORE DELETE ON connector_rate_policy_versions BEGIN SELECT RAISE(ABORT,'connector rate policies are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_rate_policy_activation_events_no_update BEFORE UPDATE ON connector_rate_policy_activation_events BEGIN SELECT RAISE(ABORT,'connector rate policy activations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_rate_policy_activation_events_no_delete BEFORE DELETE ON connector_rate_policy_activation_events BEGIN SELECT RAISE(ABORT,'connector rate policy activations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_quota_reservations_no_update BEFORE UPDATE ON connector_quota_reservations BEGIN SELECT RAISE(ABORT,'connector reservations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_quota_reservations_no_delete BEFORE DELETE ON connector_quota_reservations BEGIN SELECT RAISE(ABORT,'connector reservations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_physical_attempts_no_update BEFORE UPDATE ON connector_physical_attempts BEGIN SELECT RAISE(ABORT,'connector physical attempts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_physical_attempts_no_delete BEFORE DELETE ON connector_physical_attempts BEGIN SELECT RAISE(ABORT,'connector physical attempts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_usage_entries_no_update BEFORE UPDATE ON connector_usage_entries BEGIN SELECT RAISE(ABORT,'connector usage is immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_usage_entries_no_delete BEFORE DELETE ON connector_usage_entries BEGIN SELECT RAISE(ABORT,'connector usage is immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_price_rate_versions_no_update BEFORE UPDATE ON connector_price_rate_versions BEGIN SELECT RAISE(ABORT,'connector price rates are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_price_rate_versions_no_delete BEFORE DELETE ON connector_price_rate_versions BEGIN SELECT RAISE(ABORT,'connector price rates are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_cost_entries_no_update BEFORE UPDATE ON connector_cost_entries BEGIN SELECT RAISE(ABORT,'connector costs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_cost_entries_no_delete BEFORE DELETE ON connector_cost_entries BEGIN SELECT RAISE(ABORT,'connector costs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_quota_settlements_no_update BEFORE UPDATE ON connector_quota_settlements BEGIN SELECT RAISE(ABORT,'connector settlements are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_quota_settlements_no_delete BEFORE DELETE ON connector_quota_settlements BEGIN SELECT RAISE(ABORT,'connector settlements are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_source_envelopes_no_update BEFORE UPDATE ON connector_source_envelopes BEGIN SELECT RAISE(ABORT,'connector source envelopes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_source_envelopes_no_delete BEFORE DELETE ON connector_source_envelopes BEGIN SELECT RAISE(ABORT,'connector source envelopes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_incidents_no_update BEFORE UPDATE ON connector_incidents BEGIN SELECT RAISE(ABORT,'connector incidents are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_incidents_no_delete BEFORE DELETE ON connector_incidents BEGIN SELECT RAISE(ABORT,'connector incidents are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_incident_events_no_update BEFORE UPDATE ON connector_incident_events BEGIN SELECT RAISE(ABORT,'connector incident events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_incident_events_no_delete BEFORE DELETE ON connector_incident_events BEGIN SELECT RAISE(ABORT,'connector incident events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_source_health_events_no_update BEFORE UPDATE ON connector_source_health_events BEGIN SELECT RAISE(ABORT,'connector health events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_source_health_events_no_delete BEFORE DELETE ON connector_source_health_events BEGIN SELECT RAISE(ABORT,'connector health events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_idempotency_keys_no_update BEFORE UPDATE ON connector_idempotency_keys BEGIN SELECT RAISE(ABORT,'connector idempotency keys are immutable'); END;
CREATE TRIGGER IF NOT EXISTS connector_idempotency_keys_no_delete BEFORE DELETE ON connector_idempotency_keys BEGIN SELECT RAISE(ABORT,'connector idempotency keys are immutable'); END;
