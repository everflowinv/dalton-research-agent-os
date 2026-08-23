CREATE TABLE IF NOT EXISTS bounded_probe_template_versions (
    version_id TEXT PRIMARY KEY,
    template_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES bounded_probe_template_versions(version_id),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(template_ref, version_number)
);

CREATE TABLE IF NOT EXISTS bounded_planner_loop_versions (
    version_id TEXT PRIMARY KEY,
    loop_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES bounded_planner_loop_versions(version_id),
    question_ref TEXT NOT NULL,
    question_version_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(loop_ref, version_number)
);

CREATE TABLE IF NOT EXISTS bounded_research_directive_versions (
    version_id TEXT PRIMARY KEY,
    directive_ref TEXT NOT NULL,
    loop_version_ref TEXT NOT NULL REFERENCES bounded_planner_loop_versions(version_id),
    effective_round INTEGER NOT NULL CHECK(effective_round >= 1),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bounded_research_directive_receipts (
    receipt_id TEXT PRIMARY KEY,
    directive_version_ref TEXT NOT NULL UNIQUE REFERENCES bounded_research_directive_versions(version_id),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bounded_planner_proposal_versions (
    proposal_id TEXT PRIMARY KEY,
    loop_version_ref TEXT NOT NULL REFERENCES bounded_planner_loop_versions(version_id),
    round_ordinal INTEGER NOT NULL CHECK(round_ordinal >= 1),
    action_kind TEXT NOT NULL CHECK(action_kind IN ('probe','terminate')),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(loop_version_ref, round_ordinal, content_hash)
);

CREATE TABLE IF NOT EXISTS bounded_planner_proposal_decisions (
    decision_id TEXT PRIMARY KEY,
    proposal_ref TEXT NOT NULL UNIQUE REFERENCES bounded_planner_proposal_versions(proposal_id),
    decision TEXT NOT NULL CHECK(decision IN ('accepted','rejected')),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bounded_research_plan_rounds (
    round_id TEXT PRIMARY KEY,
    loop_version_ref TEXT NOT NULL REFERENCES bounded_planner_loop_versions(version_id),
    round_ordinal INTEGER NOT NULL CHECK(round_ordinal >= 1),
    proposal_ref TEXT NOT NULL UNIQUE REFERENCES bounded_planner_proposal_versions(proposal_id),
    work_order_ref TEXT NOT NULL UNIQUE,
    workflow_version_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(loop_version_ref, round_ordinal)
);

CREATE TABLE IF NOT EXISTS bounded_coverage_manifests (
    manifest_id TEXT PRIMARY KEY,
    loop_version_ref TEXT NOT NULL REFERENCES bounded_planner_loop_versions(version_id),
    through_round INTEGER NOT NULL CHECK(through_round >= 1),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(loop_version_ref, through_round)
);

CREATE TABLE IF NOT EXISTS bounded_research_outcomes (
    outcome_id TEXT PRIMARY KEY,
    loop_version_ref TEXT NOT NULL REFERENCES bounded_planner_loop_versions(version_id),
    round_ref TEXT NOT NULL UNIQUE REFERENCES bounded_research_plan_rounds(round_id),
    round_ordinal INTEGER NOT NULL CHECK(round_ordinal >= 1),
    manifest_ref TEXT NOT NULL UNIQUE REFERENCES bounded_coverage_manifests(manifest_id),
    outcome_kind TEXT NOT NULL CHECK(outcome_kind IN ('observed','not_found_in_scope','source_unavailable')),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(loop_version_ref, round_ordinal)
);

CREATE TABLE IF NOT EXISTS bounded_planner_terminal_events (
    event_id TEXT PRIMARY KEY,
    loop_version_ref TEXT NOT NULL UNIQUE REFERENCES bounded_planner_loop_versions(version_id),
    terminal_state TEXT NOT NULL CHECK(terminal_state IN (
        'coverage_complete_unobservable_candidate',
        'evidence_observed_for_review',
        'human_replan_required',
        'human_deprioritized',
        'budget_exhausted'
    )),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS bounded_rounds_by_loop
ON bounded_research_plan_rounds(loop_version_ref, round_ordinal);
CREATE INDEX IF NOT EXISTS bounded_outcomes_by_loop
ON bounded_research_outcomes(loop_version_ref, round_ordinal);
CREATE INDEX IF NOT EXISTS bounded_directives_by_loop
ON bounded_research_directive_versions(loop_version_ref, effective_round, created_at);

CREATE TRIGGER IF NOT EXISTS bounded_probe_template_versions_insert_guard
BEFORE INSERT ON bounded_probe_template_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'bounded probe template insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS bounded_planner_loop_versions_insert_guard
BEFORE INSERT ON bounded_planner_loop_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'bounded planner loop insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS bounded_research_directive_versions_insert_guard
BEFORE INSERT ON bounded_research_directive_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'bounded research directive insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS bounded_research_directive_receipts_insert_guard
BEFORE INSERT ON bounded_research_directive_receipts WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'bounded research directive receipt insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS bounded_planner_proposal_versions_insert_guard
BEFORE INSERT ON bounded_planner_proposal_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'bounded planner proposal insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS bounded_planner_proposal_decisions_insert_guard
BEFORE INSERT ON bounded_planner_proposal_decisions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'bounded planner decision insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS bounded_research_plan_rounds_insert_guard
BEFORE INSERT ON bounded_research_plan_rounds WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'bounded research round insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS bounded_coverage_manifests_insert_guard
BEFORE INSERT ON bounded_coverage_manifests WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'bounded coverage manifest insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS bounded_research_outcomes_insert_guard
BEFORE INSERT ON bounded_research_outcomes WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'bounded research outcome insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS bounded_planner_terminal_events_insert_guard
BEFORE INSERT ON bounded_planner_terminal_events WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'bounded terminal event insert requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS bounded_probe_template_versions_no_update
BEFORE UPDATE ON bounded_probe_template_versions BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_probe_template_versions_no_delete
BEFORE DELETE ON bounded_probe_template_versions BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_planner_loop_versions_no_update
BEFORE UPDATE ON bounded_planner_loop_versions BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_planner_loop_versions_no_delete
BEFORE DELETE ON bounded_planner_loop_versions BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_research_directive_versions_no_update
BEFORE UPDATE ON bounded_research_directive_versions BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_research_directive_versions_no_delete
BEFORE DELETE ON bounded_research_directive_versions BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_research_directive_receipts_no_update
BEFORE UPDATE ON bounded_research_directive_receipts BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_research_directive_receipts_no_delete
BEFORE DELETE ON bounded_research_directive_receipts BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_planner_proposal_versions_no_update
BEFORE UPDATE ON bounded_planner_proposal_versions BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_planner_proposal_versions_no_delete
BEFORE DELETE ON bounded_planner_proposal_versions BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_planner_proposal_decisions_no_update
BEFORE UPDATE ON bounded_planner_proposal_decisions BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_planner_proposal_decisions_no_delete
BEFORE DELETE ON bounded_planner_proposal_decisions BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_research_plan_rounds_no_update
BEFORE UPDATE ON bounded_research_plan_rounds BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_research_plan_rounds_no_delete
BEFORE DELETE ON bounded_research_plan_rounds BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_coverage_manifests_no_update
BEFORE UPDATE ON bounded_coverage_manifests BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_coverage_manifests_no_delete
BEFORE DELETE ON bounded_coverage_manifests BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_research_outcomes_no_update
BEFORE UPDATE ON bounded_research_outcomes BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_research_outcomes_no_delete
BEFORE DELETE ON bounded_research_outcomes BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_planner_terminal_events_no_update
BEFORE UPDATE ON bounded_planner_terminal_events BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS bounded_planner_terminal_events_no_delete
BEFORE DELETE ON bounded_planner_terminal_events BEGIN SELECT RAISE(ABORT, 'bounded authority is append-only'); END;
