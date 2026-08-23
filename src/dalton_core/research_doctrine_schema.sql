CREATE TABLE IF NOT EXISTS doctrine_pack_versions (
    version_id TEXT PRIMARY KEY,
    doctrine_pack_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES doctrine_pack_versions(version_id),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(doctrine_pack_ref, version_number)
);

CREATE TABLE IF NOT EXISTS doctrine_override_versions (
    version_id TEXT PRIMARY KEY,
    override_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES doctrine_override_versions(version_id),
    loop_version_ref TEXT NOT NULL REFERENCES bounded_planner_loop_versions(version_id),
    effective_from TEXT NOT NULL,
    effective_until TEXT NOT NULL,
    revoked INTEGER NOT NULL CHECK(revoked IN (0,1)),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(override_ref, version_number)
);

CREATE TABLE IF NOT EXISTS planner_context_pack_versions (
    context_pack_id TEXT PRIMARY KEY,
    loop_version_ref TEXT NOT NULL REFERENCES bounded_planner_loop_versions(version_id),
    round_ordinal INTEGER NOT NULL CHECK(round_ordinal >= 1),
    doctrine_pack_version_ref TEXT NOT NULL REFERENCES doctrine_pack_versions(version_id),
    selected_lens_ref TEXT NOT NULL,
    override_version_ref TEXT REFERENCES doctrine_override_versions(version_id),
    as_of TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS doctrine_overrides_by_loop
ON doctrine_override_versions(loop_version_ref, effective_from, effective_until);
CREATE INDEX IF NOT EXISTS planner_context_by_loop
ON planner_context_pack_versions(loop_version_ref, round_ordinal, as_of);

CREATE TRIGGER IF NOT EXISTS doctrine_pack_versions_insert_guard
BEFORE INSERT ON doctrine_pack_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'doctrine pack insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS doctrine_override_versions_insert_guard
BEFORE INSERT ON doctrine_override_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'doctrine override insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS planner_context_pack_versions_insert_guard
BEFORE INSERT ON planner_context_pack_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'planner context pack insert requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS doctrine_pack_versions_no_update
BEFORE UPDATE ON doctrine_pack_versions BEGIN SELECT RAISE(ABORT, 'research doctrine authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS doctrine_pack_versions_no_delete
BEFORE DELETE ON doctrine_pack_versions BEGIN SELECT RAISE(ABORT, 'research doctrine authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS doctrine_override_versions_no_update
BEFORE UPDATE ON doctrine_override_versions BEGIN SELECT RAISE(ABORT, 'research doctrine authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS doctrine_override_versions_no_delete
BEFORE DELETE ON doctrine_override_versions BEGIN SELECT RAISE(ABORT, 'research doctrine authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS planner_context_pack_versions_no_update
BEFORE UPDATE ON planner_context_pack_versions BEGIN SELECT RAISE(ABORT, 'research doctrine authority is append-only'); END;
CREATE TRIGGER IF NOT EXISTS planner_context_pack_versions_no_delete
BEFORE DELETE ON planner_context_pack_versions BEGIN SELECT RAISE(ABORT, 'research doctrine authority is append-only'); END;
