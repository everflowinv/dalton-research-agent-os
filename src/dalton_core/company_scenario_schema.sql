-- ADR-0012: structured scenario assumptions and their observations.
--
-- Four append-only tables.  A *set* is one proposal (one model call, or one
-- owner act) for one company; a *version* is one scenario inside a set and
-- chains to the previous version of the same scenario across sets; a
-- *decision* is the owner's answer to a set; an *observation* is what the
-- deterministic evaluator saw for one condition of one version.  Nothing is
-- ever updated or deleted: which scenarios are in force is folded from the
-- rows (company_scenario.CompanyScenarioAuthority.in_force).

CREATE TABLE IF NOT EXISTS company_scenario_sets (
    set_id TEXT PRIMARY KEY,
    company_ref TEXT NOT NULL,
    origin TEXT NOT NULL CHECK(origin IN ('automation', 'owner')),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS company_scenario_sets_by_company
ON company_scenario_sets(company_ref, created_at);

CREATE TABLE IF NOT EXISTS company_scenario_versions (
    version_id TEXT PRIMARY KEY,
    scenario_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES company_scenario_versions(version_id),
    set_ref TEXT NOT NULL REFERENCES company_scenario_sets(set_id),
    company_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(scenario_ref, version_number)
);

CREATE INDEX IF NOT EXISTS company_scenario_versions_by_set
ON company_scenario_versions(set_ref, scenario_ref, version_number);

CREATE TABLE IF NOT EXISTS company_scenario_decisions (
    decision_id TEXT PRIMARY KEY,
    set_ref TEXT NOT NULL REFERENCES company_scenario_sets(set_id),
    set_hash TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('accept', 'reject', 'defer')),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL CHECK(actor_ref LIKE 'human:%'),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS company_scenario_decisions_by_set
ON company_scenario_decisions(set_ref, created_at);

CREATE TABLE IF NOT EXISTS company_scenario_observations (
    observation_id TEXT PRIMARY KEY,
    scenario_version_ref TEXT NOT NULL REFERENCES company_scenario_versions(version_id),
    condition_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    company_ref TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('triggered', 'not_triggered', 'unobservable')),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(scenario_version_ref, condition_id, sequence)
);

CREATE INDEX IF NOT EXISTS company_scenario_observations_by_company
ON company_scenario_observations(company_ref, created_at);

CREATE TRIGGER IF NOT EXISTS company_scenario_sets_insert_guard
BEFORE INSERT ON company_scenario_sets WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'company scenario insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS company_scenario_sets_no_update
BEFORE UPDATE ON company_scenario_sets BEGIN
    SELECT RAISE(ABORT, 'company scenario sets are immutable');
END;
CREATE TRIGGER IF NOT EXISTS company_scenario_sets_no_delete
BEFORE DELETE ON company_scenario_sets BEGIN
    SELECT RAISE(ABORT, 'company scenario sets are immutable');
END;

CREATE TRIGGER IF NOT EXISTS company_scenario_versions_insert_guard
BEFORE INSERT ON company_scenario_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'company scenario insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS company_scenario_versions_no_update
BEFORE UPDATE ON company_scenario_versions BEGIN
    SELECT RAISE(ABORT, 'company scenario versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS company_scenario_versions_no_delete
BEFORE DELETE ON company_scenario_versions BEGIN
    SELECT RAISE(ABORT, 'company scenario versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS company_scenario_decisions_insert_guard
BEFORE INSERT ON company_scenario_decisions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'company scenario insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS company_scenario_decisions_no_update
BEFORE UPDATE ON company_scenario_decisions BEGIN
    SELECT RAISE(ABORT, 'company scenario decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS company_scenario_decisions_no_delete
BEFORE DELETE ON company_scenario_decisions BEGIN
    SELECT RAISE(ABORT, 'company scenario decisions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS company_scenario_observations_insert_guard
BEFORE INSERT ON company_scenario_observations WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'company scenario insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS company_scenario_observations_no_update
BEFORE UPDATE ON company_scenario_observations BEGIN
    SELECT RAISE(ABORT, 'company scenario observations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS company_scenario_observations_no_delete
BEFORE DELETE ON company_scenario_observations BEGIN
    SELECT RAISE(ABORT, 'company scenario observations are immutable');
END;
