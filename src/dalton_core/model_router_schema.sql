PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS model_endpoint_profile_versions (
    profile_version_ref TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    prior_version_ref TEXT REFERENCES model_endpoint_profile_versions(profile_version_ref),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    family TEXT NOT NULL,
    adapter_ref TEXT NOT NULL,
    credential_slot_ref TEXT NOT NULL,
    profile_hash TEXT NOT NULL UNIQUE,
    profile_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(profile_id, version)
);

CREATE TABLE IF NOT EXISTS model_routing_policy_versions (
    policy_version_ref TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    prior_version_ref TEXT REFERENCES model_routing_policy_versions(policy_version_ref),
    policy_hash TEXT NOT NULL UNIQUE,
    policy_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(policy_id, version)
);

CREATE TABLE IF NOT EXISTS model_route_decisions (
    decision_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE,
    decision_kind TEXT NOT NULL CHECK (decision_kind IN ('initial', 'retry', 'switch')),
    outcome TEXT NOT NULL CHECK (outcome IN ('selected', 'rejected')),
    work_order_id TEXT NOT NULL,
    work_order_hash TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    capability TEXT NOT NULL,
    policy_version_ref TEXT NOT NULL REFERENCES model_routing_policy_versions(policy_version_ref),
    policy_hash TEXT NOT NULL,
    candidate_snapshot_hash TEXT NOT NULL,
    selected_profile_version_ref TEXT REFERENCES model_endpoint_profile_versions(profile_version_ref),
    previous_decision_ref TEXT REFERENCES model_route_decisions(decision_id),
    request_hash TEXT NOT NULL,
    decision_hash TEXT NOT NULL UNIQUE,
    decision_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_route_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    decision_id TEXT NOT NULL REFERENCES model_route_decisions(decision_id),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS model_profile_insert_authorized
BEFORE INSERT ON model_endpoint_profile_versions
WHEN dalton_model_router_authorized() != 1 BEGIN
    SELECT RAISE(ABORT, 'model profile writes require ModelRouter');
END;
CREATE TRIGGER IF NOT EXISTS model_profile_no_update
BEFORE UPDATE ON model_endpoint_profile_versions BEGIN
    SELECT RAISE(ABORT, 'model endpoint profile versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS model_profile_no_delete
BEFORE DELETE ON model_endpoint_profile_versions BEGIN
    SELECT RAISE(ABORT, 'model endpoint profile versions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS model_policy_insert_authorized
BEFORE INSERT ON model_routing_policy_versions
WHEN dalton_model_router_authorized() != 1 BEGIN
    SELECT RAISE(ABORT, 'model policy writes require ModelRouter');
END;
CREATE TRIGGER IF NOT EXISTS model_policy_no_update
BEFORE UPDATE ON model_routing_policy_versions BEGIN
    SELECT RAISE(ABORT, 'model routing policy versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS model_policy_no_delete
BEFORE DELETE ON model_routing_policy_versions BEGIN
    SELECT RAISE(ABORT, 'model routing policy versions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS model_decision_insert_authorized
BEFORE INSERT ON model_route_decisions
WHEN dalton_model_router_authorized() != 1 BEGIN
    SELECT RAISE(ABORT, 'model route decisions require ModelRouter');
END;
CREATE TRIGGER IF NOT EXISTS model_decision_no_update
BEFORE UPDATE ON model_route_decisions BEGIN
    SELECT RAISE(ABORT, 'model route decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS model_decision_no_delete
BEFORE DELETE ON model_route_decisions BEGIN
    SELECT RAISE(ABORT, 'model route decisions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS model_idempotency_insert_authorized
BEFORE INSERT ON model_route_idempotency
WHEN dalton_model_router_authorized() != 1 BEGIN
    SELECT RAISE(ABORT, 'model route idempotency writes require ModelRouter');
END;
CREATE TRIGGER IF NOT EXISTS model_idempotency_no_update
BEFORE UPDATE ON model_route_idempotency BEGIN
    SELECT RAISE(ABORT, 'model route idempotency rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS model_idempotency_no_delete
BEFORE DELETE ON model_route_idempotency BEGIN
    SELECT RAISE(ABORT, 'model route idempotency rows are append-only');
END;

-- P14-M: which link of which per-purpose-tier fallback chain served this
-- attempt, and why the links before it were skipped.  It is a separate table
-- rather than three more fields on the route decision because the decision's
-- wire shape is validated key-for-key by the broker adapter: a decision that
-- grew a field would stop being admissible, and every historical decision hash
-- would have to be recomputed.  Append-only like everything else here; a
-- replay reads the links and the decisions they point at together.
CREATE TABLE IF NOT EXISTS model_route_chain_links (
    link_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id TEXT NOT NULL UNIQUE,
    work_order_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    purpose TEXT NOT NULL,
    tier TEXT NOT NULL,
    chain_position INTEGER NOT NULL CHECK (chain_position > 0),
    profile_id TEXT NOT NULL,
    decision_id TEXT NOT NULL REFERENCES model_route_decisions(decision_id),
    served INTEGER NOT NULL CHECK (served IN (0, 1)),
    skip_reason TEXT,
    policy_version_ref TEXT NOT NULL
        REFERENCES model_routing_policy_versions(policy_version_ref),
    link_hash TEXT NOT NULL UNIQUE,
    link_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK ((served = 1 AND skip_reason IS NULL) OR (served = 0 AND skip_reason IS NOT NULL)),
    UNIQUE(work_order_id, capability, attempt_number, chain_position)
);

CREATE TRIGGER IF NOT EXISTS model_chain_link_insert_authorized
BEFORE INSERT ON model_route_chain_links
WHEN dalton_model_router_authorized() != 1 BEGIN
    SELECT RAISE(ABORT, 'model route chain links require ModelRouter');
END;
CREATE TRIGGER IF NOT EXISTS model_chain_link_no_update
BEFORE UPDATE ON model_route_chain_links BEGIN
    SELECT RAISE(ABORT, 'model route chain links are immutable');
END;
CREATE TRIGGER IF NOT EXISTS model_chain_link_no_delete
BEFORE DELETE ON model_route_chain_links BEGIN
    SELECT RAISE(ABORT, 'model route chain links are append-only');
END;

-- P14-M2: the owner let one more of the gateway's models through to Dalton.
-- Writing the broker's plugin subtree in ~/.openclaw/openclaw.json is a change
-- to a file outside this repository's authorities, so the record of it has to
-- live somewhere that cannot be quietly edited: which model, who decided, what
-- the backup was called, and exactly which keys moved.  It sits in the router's
-- own database because that is where the consequence lands -- the catalog lane
-- registers a profile for the model on its next run -- and because a new
-- database would be a new schema file for a table with a handful of rows.
CREATE TABLE IF NOT EXISTS model_openclaw_allow_decisions (
    decision_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE,
    model_ref TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    config_path TEXT NOT NULL,
    backup_path TEXT NOT NULL,
    decision_hash TEXT NOT NULL UNIQUE,
    decision_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS model_allow_decision_insert_authorized
BEFORE INSERT ON model_openclaw_allow_decisions
WHEN dalton_model_router_authorized() != 1 BEGIN
    SELECT RAISE(ABORT, 'openclaw allow decisions require ModelRouter');
END;
CREATE TRIGGER IF NOT EXISTS model_allow_decision_no_update
BEFORE UPDATE ON model_openclaw_allow_decisions BEGIN
    SELECT RAISE(ABORT, 'openclaw allow decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS model_allow_decision_no_delete
BEFORE DELETE ON model_openclaw_allow_decisions BEGIN
    SELECT RAISE(ABORT, 'openclaw allow decisions are append-only');
END;
