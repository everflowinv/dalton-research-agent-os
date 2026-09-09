-- Q2 / C4: one record a week about where the week went.
--
-- The v0.4 freeze is kept and it is the reason this table stands alone: the
-- reflection **never writes the Ledger, never changes policy and never admits
-- a question**.  It reads eight authorities and writes one row here.  What it
-- produces instead of decisions is `backlog_candidates` -- question text, a
-- because, and refs -- which the planner or a human may pick up, and
-- `policy_suggestions`, which are sentences.  A subsystem that measured the
-- research cycle and could also change it would be marking its own homework.
--
-- Weekly, per mission, append-only, content-hashed, with a version chain.
-- `iso_week` is the ISO-8601 week the reflection is *about* -- the one that
-- closed at the Monday boundary the lane fired after -- not the week it ran
-- in.  A reflection recomputed later in the same week is a new version of the
-- same record only if its inputs moved: `inputs_hash` covers every number the
-- reflection read, so re-running an unchanged week is a `duplicate` and a week
-- whose backlog grew after Monday can be reflected on again.

CREATE TABLE IF NOT EXISTS research_cycle_reflection_versions (
    version_id TEXT PRIMARY KEY,
    reflection_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_ref TEXT REFERENCES research_cycle_reflection_versions(version_id),
    mission_ref TEXT NOT NULL,
    mission_version_ref TEXT NOT NULL,
    iso_week TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    -- Every number the reflection read, hashed.  This is the duplicate rule.
    inputs_hash TEXT NOT NULL,
    backlog_candidate_count INTEGER NOT NULL CHECK(backlog_candidate_count >= 0),
    policy_suggestion_count INTEGER NOT NULL CHECK(policy_suggestion_count >= 0),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(reflection_ref, version_number),
    -- One reflection per mission per week, and within it one version per set
    -- of inputs.  A lane that fires twice on the same Monday writes once.
    UNIQUE(reflection_ref, inputs_hash)
);

CREATE INDEX IF NOT EXISTS research_cycle_reflections_by_week
ON research_cycle_reflection_versions(mission_ref, iso_week);

CREATE TABLE IF NOT EXISTS research_cycle_reflection_pointer (
    reflection_ref TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES research_cycle_reflection_versions(version_id),
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS research_cycle_reflection_authorized_insert
BEFORE INSERT ON research_cycle_reflection_versions
WHEN dalton_research_cycle_reflection_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research cycle reflection insert requires ResearchCycleReflectionAuthority');
END;
CREATE TRIGGER IF NOT EXISTS research_cycle_reflection_no_update
BEFORE UPDATE ON research_cycle_reflection_versions BEGIN
    SELECT RAISE(ABORT, 'research cycle reflections are append-only');
END;
CREATE TRIGGER IF NOT EXISTS research_cycle_reflection_no_delete
BEFORE DELETE ON research_cycle_reflection_versions BEGIN
    SELECT RAISE(ABORT, 'research cycle reflections are append-only');
END;

CREATE TRIGGER IF NOT EXISTS research_cycle_reflection_pointer_authorized_insert
BEFORE INSERT ON research_cycle_reflection_pointer
WHEN dalton_research_cycle_reflection_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research cycle reflection pointer requires the authority');
END;
CREATE TRIGGER IF NOT EXISTS research_cycle_reflection_pointer_authorized_update
BEFORE UPDATE ON research_cycle_reflection_pointer
WHEN dalton_research_cycle_reflection_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research cycle reflection pointer requires the authority');
END;
CREATE TRIGGER IF NOT EXISTS research_cycle_reflection_pointer_no_delete
BEFORE DELETE ON research_cycle_reflection_pointer BEGIN
    SELECT RAISE(ABORT, 'research cycle reflection pointer rows are never deleted');
END;
