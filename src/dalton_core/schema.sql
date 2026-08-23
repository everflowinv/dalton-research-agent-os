PRAGMA foreign_keys = ON;

-- Staging is deliberately mutable.  Every table below it is an append-only
-- authority or a projection guarded by the store authorization UDF.
CREATE TABLE IF NOT EXISTS staging_changes (
    change_id TEXT PRIMARY KEY,
    thesis_id TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    producer_invocation_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'staged'
        CHECK (status IN ('staged', 'verified', 'committed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Staging may be revised by the service, but direct callers cannot mutate it.
-- These triggers are integrity guardrails, not a hostile-process sandbox; an
-- untrusted runtime must never receive the database path.
CREATE TRIGGER IF NOT EXISTS staging_changes_authorized_insert
BEFORE INSERT ON staging_changes
WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'staging_changes insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS staging_changes_authorized_update
BEFORE UPDATE ON staging_changes
WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'staging_changes update requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS staging_changes_no_delete
BEFORE DELETE ON staging_changes BEGIN
    SELECT RAISE(ABORT, 'staging_changes cannot be deleted');
END;

CREATE TABLE IF NOT EXISTS execution_invocations (
    execution_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('model', 'connector')),
    work_order_ref TEXT NOT NULL,
    profile_ref TEXT NOT NULL,
    capability TEXT NOT NULL,
    runtime_ref TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    parent_ref TEXT,
    environment_hash TEXT,
    execution_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_invocations (
    invocation_id TEXT PRIMARY KEY,
    profile_ref TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    capability TEXT NOT NULL,
    runtime_ref TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    environment_hash TEXT,
    granularity TEXT NOT NULL,
    work_order_ref TEXT NOT NULL,
    model_family TEXT NOT NULL,
    invocation_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_invocation_model_links (
    execution_ref TEXT PRIMARY KEY REFERENCES execution_invocations(execution_id),
    model_invocation_ref TEXT NOT NULL UNIQUE REFERENCES model_invocations(invocation_id),
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verification_records (
    verification_id TEXT PRIMARY KEY,
    change_id TEXT REFERENCES staging_changes(change_id),
    producer_invocation_id TEXT NOT NULL,
    verifier_invocation_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    verification_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    policy_version_id TEXT NOT NULL REFERENCES governance_policy_versions(policy_version_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS governance_policy_versions (
    policy_version_id TEXT PRIMARY KEY,
    version_number INTEGER NOT NULL,
    policy_ref TEXT NOT NULL DEFAULT 'commit-gate',
    effective_from TEXT NOT NULL,
    effective_until TEXT,
    actor_ref TEXT NOT NULL DEFAULT 'system:dalton-core',
    prior_version_ref TEXT REFERENCES governance_policy_versions(policy_version_id),
    change_reason TEXT NOT NULL DEFAULT 'initial policy',
    version_json TEXT NOT NULL DEFAULT '{}',
    policy_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (policy_ref, version_number)
);

CREATE TABLE IF NOT EXISTS governance_policy_pointer (
    pointer_id INTEGER PRIMARY KEY CHECK (pointer_id = 1),
    policy_version_id TEXT NOT NULL REFERENCES governance_policy_versions(policy_version_id),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS driver_pack_versions (
    version_id TEXT PRIMARY KEY,
    driver_pack_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number > 0),
    prior_version_id TEXT REFERENCES driver_pack_versions(version_id),
    industry_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(driver_pack_ref, version_number)
);

CREATE TABLE IF NOT EXISTS driver_pack_pointer (
    driver_pack_ref TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES driver_pack_versions(version_id)
);

CREATE TABLE IF NOT EXISTS thesis_admission_candidates (
    candidate_id TEXT PRIMARY KEY,
    thesis_ref TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    industry_ref TEXT NOT NULL,
    mandate_version_ref TEXT NOT NULL,
    mandate_version_hash TEXT NOT NULL,
    driver_pack_version_ref TEXT NOT NULL REFERENCES driver_pack_versions(version_id),
    driver_pack_version_hash TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS thesis_admission_decisions (
    decision_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE REFERENCES thesis_admission_candidates(candidate_id),
    candidate_hash TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('admit','reject')),
    rationale TEXT NOT NULL,
    reviewer_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coverage_governance_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS thesis_versions (
    version_id TEXT PRIMARY KEY,
    thesis_id TEXT NOT NULL,
    version_number INTEGER NOT NULL,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    prior_version_id TEXT REFERENCES thesis_versions(version_id),
    change_id TEXT REFERENCES staging_changes(change_id),
    verification_id TEXT REFERENCES verification_records(verification_id),
    admission_decision_id TEXT REFERENCES thesis_admission_decisions(decision_id),
    authority_kind TEXT NOT NULL CHECK(authority_kind IN ('verification','human_admission')),
    authority_ref TEXT NOT NULL,
    committed_by TEXT,
    created_at TEXT NOT NULL,
    CHECK(
        (authority_kind='verification' AND change_id IS NOT NULL AND verification_id IS NOT NULL AND admission_decision_id IS NULL AND authority_ref=verification_id)
        OR
        (authority_kind='human_admission' AND change_id IS NULL AND verification_id IS NULL AND admission_decision_id IS NOT NULL AND authority_ref=admission_decision_id)
    ),
    UNIQUE (thesis_id, version_number)
);

CREATE TABLE IF NOT EXISTS domain_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    change_id TEXT REFERENCES staging_changes(change_id),
    verification_id TEXT REFERENCES verification_records(verification_id),
    version_id TEXT UNIQUE REFERENCES thesis_versions(version_id),
    aggregate_version INTEGER NOT NULL,
    version_ref TEXT,
    content_hash TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    event_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS current_pointers (
    thesis_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL UNIQUE REFERENCES thesis_versions(version_id),
    version_number INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    version_id TEXT,
    created_at TEXT NOT NULL
);

-- Research ledger authorities.  Stable refs identify a logical evidence or
-- claim; version rows are immutable content snapshots.  Status is deliberately
-- absent: it is derived by store projections from relations/conflicts and the
-- latest adjudication.
CREATE TABLE IF NOT EXISTS evidence_versions (
    evidence_version_id TEXT PRIMARY KEY,
    evidence_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number >= 1),
    evidence_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    prior_version_id TEXT REFERENCES evidence_versions(evidence_version_id),
    created_at TEXT NOT NULL,
    UNIQUE (evidence_ref, version_number)
);
CREATE TABLE IF NOT EXISTS claim_versions (
    claim_version_id TEXT PRIMARY KEY,
    claim_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number >= 1),
    claim_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    prior_version_id TEXT REFERENCES claim_versions(claim_version_id),
    created_at TEXT NOT NULL,
    UNIQUE (claim_ref, version_number)
);
CREATE TABLE IF NOT EXISTS evidence_relations (
    relation_id TEXT PRIMARY KEY,
    evidence_ref TEXT NOT NULL,
    evidence_version_id TEXT NOT NULL REFERENCES evidence_versions(evidence_version_id),
    claim_ref TEXT NOT NULL,
    claim_version_id TEXT NOT NULL REFERENCES claim_versions(claim_version_id),
    relation TEXT NOT NULL CHECK (relation IN ('supports', 'contradicts', 'qualifies')),
    relation_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relation_idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    relation_id TEXT NOT NULL REFERENCES evidence_relations(relation_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS adjudication_versions (
    adjudication_version_id TEXT PRIMARY KEY,
    claim_ref TEXT NOT NULL,
    claim_version_id TEXT NOT NULL REFERENCES claim_versions(claim_version_id),
    version_number INTEGER NOT NULL CHECK (version_number >= 1),
    adjudicated_status TEXT NOT NULL CHECK (adjudicated_status IN ('corroborated', 'contested', 'superseded', 'retracted')),
    adjudication_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    prior_version_id TEXT REFERENCES adjudication_versions(adjudication_version_id),
    adjudicator_invocation_id TEXT NOT NULL REFERENCES model_invocations(invocation_id),
    independence_policy_id TEXT NOT NULL REFERENCES governance_policy_versions(policy_version_id),
    created_at TEXT NOT NULL,
    UNIQUE (claim_ref, version_number)
);
CREATE TABLE IF NOT EXISTS claim_challenges (
    challenge_id TEXT PRIMARY KEY,
    conflict_key TEXT NOT NULL UNIQUE,
    claim_version_id TEXT NOT NULL REFERENCES claim_versions(claim_version_id),
    conflicting_claim_version_id TEXT NOT NULL REFERENCES claim_versions(claim_version_id),
    challenge_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Exact accepted human-or-policy authorization + formal
-- Evidence/Claim/Relation result.  The writer
-- inserts this row in the same transaction as all three Ledger records.  It
-- is both the cross-boundary idempotency receipt and the durable copy of the
-- authorization.  The policy path is derived inside DaltonStore from the
-- active version and exact Core connector authority; callers cannot submit
-- an automation identity as a human review.
CREATE TABLE IF NOT EXISTS reviewed_candidate_commits (
    idempotency_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    review_decision_ref TEXT NOT NULL UNIQUE,
    candidate_evidence_ref TEXT NOT NULL UNIQUE,
    candidate_claim_ref TEXT NOT NULL UNIQUE,
    decision_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Keep a conventional view name available to callers that do not need to
-- know the singleton implementation detail.
CREATE VIEW IF NOT EXISTS active_policy_pointer AS
SELECT pointer_id, policy_version_id, updated_at
FROM governance_policy_pointer WHERE pointer_id = 1;

CREATE TRIGGER IF NOT EXISTS reviewed_candidate_commits_authorized_insert
BEFORE INSERT ON reviewed_candidate_commits
WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'reviewed candidate commit requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS reviewed_candidate_commits_no_update
BEFORE UPDATE ON reviewed_candidate_commits BEGIN
    SELECT RAISE(ABORT, 'reviewed candidate commits are immutable');
END;
CREATE TRIGGER IF NOT EXISTS reviewed_candidate_commits_no_delete
BEFORE DELETE ON reviewed_candidate_commits BEGIN
    SELECT RAISE(ABORT, 'reviewed candidate commits are immutable');
END;

-- No update or delete can rewrite authoritative history, even from the store.
CREATE TRIGGER IF NOT EXISTS execution_invocations_no_update
BEFORE UPDATE ON execution_invocations BEGIN
    SELECT RAISE(ABORT, 'execution_invocations is immutable');
END;
CREATE TRIGGER IF NOT EXISTS execution_invocations_no_delete
BEFORE DELETE ON execution_invocations BEGIN
    SELECT RAISE(ABORT, 'execution_invocations is immutable');
END;
CREATE TRIGGER IF NOT EXISTS execution_invocation_model_links_no_update
BEFORE UPDATE ON execution_invocation_model_links BEGIN
    SELECT RAISE(ABORT, 'execution_invocation_model_links is immutable');
END;
CREATE TRIGGER IF NOT EXISTS execution_invocation_model_links_no_delete
BEFORE DELETE ON execution_invocation_model_links BEGIN
    SELECT RAISE(ABORT, 'execution_invocation_model_links is immutable');
END;
CREATE TRIGGER IF NOT EXISTS model_invocations_no_update
BEFORE UPDATE ON model_invocations BEGIN
    SELECT RAISE(ABORT, 'model_invocations is immutable');
END;
CREATE TRIGGER IF NOT EXISTS model_invocations_no_delete
BEFORE DELETE ON model_invocations BEGIN
    SELECT RAISE(ABORT, 'model_invocations is immutable');
END;
CREATE TRIGGER IF NOT EXISTS verification_records_no_update
BEFORE UPDATE ON verification_records BEGIN
    SELECT RAISE(ABORT, 'verification_records is immutable');
END;
CREATE TRIGGER IF NOT EXISTS verification_records_no_delete
BEFORE DELETE ON verification_records BEGIN
    SELECT RAISE(ABORT, 'verification_records is immutable');
END;
CREATE TRIGGER IF NOT EXISTS governance_policy_versions_no_update
BEFORE UPDATE ON governance_policy_versions BEGIN
    SELECT RAISE(ABORT, 'governance_policy_versions is immutable');
END;
CREATE TRIGGER IF NOT EXISTS governance_policy_versions_no_delete
BEFORE DELETE ON governance_policy_versions BEGIN
    SELECT RAISE(ABORT, 'governance_policy_versions is immutable');
END;
CREATE TRIGGER IF NOT EXISTS driver_pack_versions_no_update
BEFORE UPDATE ON driver_pack_versions BEGIN SELECT RAISE(ABORT, 'driver pack versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS driver_pack_versions_no_delete
BEFORE DELETE ON driver_pack_versions BEGIN SELECT RAISE(ABORT, 'driver pack versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS thesis_admission_candidates_no_update
BEFORE UPDATE ON thesis_admission_candidates BEGIN SELECT RAISE(ABORT, 'thesis admission candidates are immutable'); END;
CREATE TRIGGER IF NOT EXISTS thesis_admission_candidates_no_delete
BEFORE DELETE ON thesis_admission_candidates BEGIN SELECT RAISE(ABORT, 'thesis admission candidates are immutable'); END;
CREATE TRIGGER IF NOT EXISTS thesis_admission_decisions_no_update
BEFORE UPDATE ON thesis_admission_decisions BEGIN SELECT RAISE(ABORT, 'thesis admission decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS thesis_admission_decisions_no_delete
BEFORE DELETE ON thesis_admission_decisions BEGIN SELECT RAISE(ABORT, 'thesis admission decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coverage_governance_idempotency_no_update
BEFORE UPDATE ON coverage_governance_idempotency BEGIN SELECT RAISE(ABORT, 'coverage governance idempotency is immutable'); END;
CREATE TRIGGER IF NOT EXISTS coverage_governance_idempotency_no_delete
BEFORE DELETE ON coverage_governance_idempotency BEGIN SELECT RAISE(ABORT, 'coverage governance idempotency is immutable'); END;
CREATE TRIGGER IF NOT EXISTS thesis_versions_no_update
BEFORE UPDATE ON thesis_versions BEGIN
    SELECT RAISE(ABORT, 'thesis_versions is immutable');
END;
CREATE TRIGGER IF NOT EXISTS thesis_versions_no_delete
BEFORE DELETE ON thesis_versions BEGIN
    SELECT RAISE(ABORT, 'thesis_versions is immutable');
END;
CREATE TRIGGER IF NOT EXISTS domain_events_no_update
BEFORE UPDATE ON domain_events BEGIN
    SELECT RAISE(ABORT, 'domain_events is immutable');
END;
CREATE TRIGGER IF NOT EXISTS domain_events_no_delete
BEFORE DELETE ON domain_events BEGIN
    SELECT RAISE(ABORT, 'domain_events is immutable');
END;

-- Inserts and pointer changes are allowed only while DaltonStore has entered
-- its short-lived transaction write context.  SQLite has no per-table grants,
-- so this trigger/UDF pair is the database-level boundary.
CREATE TRIGGER IF NOT EXISTS execution_invocations_authorized_insert
BEFORE INSERT ON execution_invocations
WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'execution_invocations insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS execution_invocation_model_links_authorized_insert
BEFORE INSERT ON execution_invocation_model_links
WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'execution_invocation_model_links insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS model_invocations_authorized_insert
BEFORE INSERT ON model_invocations
WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'model_invocations insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS verification_records_authorized_insert
BEFORE INSERT ON verification_records
WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'verification_records insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS governance_policy_versions_authorized_insert
BEFORE INSERT ON governance_policy_versions
WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'governance_policy_versions insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS driver_pack_versions_authorized_insert
BEFORE INSERT ON driver_pack_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'driver pack version insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS driver_pack_pointer_authorized_insert
BEFORE INSERT ON driver_pack_pointer WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'driver pack pointer insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS driver_pack_pointer_authorized_update
BEFORE UPDATE ON driver_pack_pointer WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'driver pack pointer update requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS driver_pack_pointer_no_delete
BEFORE DELETE ON driver_pack_pointer BEGIN SELECT RAISE(ABORT, 'driver pack pointer cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS thesis_admission_candidates_authorized_insert
BEFORE INSERT ON thesis_admission_candidates WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'thesis admission candidate insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS thesis_admission_decisions_authorized_insert
BEFORE INSERT ON thesis_admission_decisions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'thesis admission decision insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS coverage_governance_idempotency_authorized_insert
BEFORE INSERT ON coverage_governance_idempotency WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'coverage governance idempotency insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS thesis_versions_authorized_insert
BEFORE INSERT ON thesis_versions
WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'thesis_versions insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS domain_events_authorized_insert
BEFORE INSERT ON domain_events
WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'domain_events insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS current_pointers_authorized_insert
BEFORE INSERT ON current_pointers
WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'current_pointers insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS current_pointers_authorized_update
BEFORE UPDATE ON current_pointers
WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'current_pointers update requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS current_pointers_no_delete
BEFORE DELETE ON current_pointers BEGIN
    SELECT RAISE(ABORT, 'current_pointers cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS governance_policy_pointer_authorized_insert
BEFORE INSERT ON governance_policy_pointer
WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'governance_policy_pointer insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS governance_policy_pointer_authorized_update
BEFORE UPDATE ON governance_policy_pointer
WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'governance_policy_pointer update requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS governance_policy_pointer_no_delete
BEFORE DELETE ON governance_policy_pointer BEGIN
    SELECT RAISE(ABORT, 'governance_policy_pointer cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS idempotency_keys_authorized_insert
BEFORE INSERT ON idempotency_keys
WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'idempotency_keys insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS idempotency_keys_no_update
BEFORE UPDATE ON idempotency_keys BEGIN
    SELECT RAISE(ABORT, 'idempotency_keys is immutable');
END;
CREATE TRIGGER IF NOT EXISTS idempotency_keys_no_delete
BEFORE DELETE ON idempotency_keys BEGIN
    SELECT RAISE(ABORT, 'idempotency_keys is immutable');
END;

CREATE TRIGGER IF NOT EXISTS evidence_versions_no_update
BEFORE UPDATE ON evidence_versions BEGIN SELECT RAISE(ABORT, 'evidence_versions is immutable'); END;
CREATE TRIGGER IF NOT EXISTS evidence_versions_no_delete
BEFORE DELETE ON evidence_versions BEGIN SELECT RAISE(ABORT, 'evidence_versions is immutable'); END;
CREATE TRIGGER IF NOT EXISTS claim_versions_no_update
BEFORE UPDATE ON claim_versions BEGIN SELECT RAISE(ABORT, 'claim_versions is immutable'); END;
CREATE TRIGGER IF NOT EXISTS claim_versions_no_delete
BEFORE DELETE ON claim_versions BEGIN SELECT RAISE(ABORT, 'claim_versions is immutable'); END;
CREATE TRIGGER IF NOT EXISTS evidence_relations_no_update
BEFORE UPDATE ON evidence_relations BEGIN SELECT RAISE(ABORT, 'evidence_relations is immutable'); END;
CREATE TRIGGER IF NOT EXISTS evidence_relations_no_delete
BEFORE DELETE ON evidence_relations BEGIN SELECT RAISE(ABORT, 'evidence_relations is immutable'); END;
CREATE TRIGGER IF NOT EXISTS adjudication_versions_no_update
BEFORE UPDATE ON adjudication_versions BEGIN SELECT RAISE(ABORT, 'adjudication_versions is immutable'); END;
CREATE TRIGGER IF NOT EXISTS adjudication_versions_no_delete
BEFORE DELETE ON adjudication_versions BEGIN SELECT RAISE(ABORT, 'adjudication_versions is immutable'); END;
CREATE TRIGGER IF NOT EXISTS claim_challenges_no_update
BEFORE UPDATE ON claim_challenges BEGIN SELECT RAISE(ABORT, 'claim_challenges is immutable'); END;
CREATE TRIGGER IF NOT EXISTS claim_challenges_no_delete
BEFORE DELETE ON claim_challenges BEGIN SELECT RAISE(ABORT, 'claim_challenges is immutable'); END;

CREATE TRIGGER IF NOT EXISTS evidence_versions_authorized_insert
BEFORE INSERT ON evidence_versions WHEN dalton_authorized() = 0 BEGIN SELECT RAISE(ABORT, 'evidence_versions insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS claim_versions_authorized_insert
BEFORE INSERT ON claim_versions WHEN dalton_authorized() = 0 BEGIN SELECT RAISE(ABORT, 'claim_versions insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS evidence_relations_authorized_insert
BEFORE INSERT ON evidence_relations WHEN dalton_authorized() = 0 BEGIN SELECT RAISE(ABORT, 'evidence_relations insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS relation_idempotency_keys_authorized_insert
BEFORE INSERT ON relation_idempotency_keys WHEN dalton_authorized() = 0 BEGIN SELECT RAISE(ABORT, 'relation_idempotency_keys insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS relation_idempotency_keys_no_update
BEFORE UPDATE ON relation_idempotency_keys BEGIN SELECT RAISE(ABORT, 'relation_idempotency_keys is immutable'); END;
CREATE TRIGGER IF NOT EXISTS relation_idempotency_keys_no_delete
BEFORE DELETE ON relation_idempotency_keys BEGIN SELECT RAISE(ABORT, 'relation_idempotency_keys is immutable'); END;
CREATE TRIGGER IF NOT EXISTS adjudication_versions_authorized_insert
BEFORE INSERT ON adjudication_versions WHEN dalton_authorized() = 0 BEGIN SELECT RAISE(ABORT, 'adjudication_versions insert requires DaltonStore'); END;
CREATE TRIGGER IF NOT EXISTS claim_challenges_authorized_insert
BEFORE INSERT ON claim_challenges WHEN dalton_authorized() = 0 BEGIN SELECT RAISE(ABORT, 'claim_challenges insert requires DaltonStore'); END;
