-- Conviction v2 (ADR-0013): the policy the owner publishes.
--
-- The thresholds a conviction call is held to -- the minimum expected return,
-- the higher bar a call that agrees with the market must clear, the minimum
-- evidence confidence, how many scenarios a call must weigh, which horizons
-- and directions are allowed, and the daily drafting budget -- are a
-- judgement about the fund, not about any one company.  So they are a
-- versioned object a *person* publishes, never a constant somebody edits.
--
-- Defaults live in code (``conviction_policy.DEFAULT_POLICY``) so a brand-new
-- workspace runs v2 with no row here at all.  The first published row
-- replaces the default; every later row replaces the one before it.  Nothing
-- is rewritten: comparing two versions is reading two rows.

CREATE TABLE IF NOT EXISTS conviction_policy_versions (
    version_id TEXT PRIMARY KEY,
    policy_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL UNIQUE CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES conviction_policy_versions(version_id),
    policy_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    -- Published by a person, and only by a person.
    actor_ref TEXT NOT NULL CHECK(actor_ref LIKE 'human:%'),
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS conviction_policy_insert_guard
BEFORE INSERT ON conviction_policy_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'conviction policy insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS conviction_policy_no_update
BEFORE UPDATE ON conviction_policy_versions BEGIN
    SELECT RAISE(ABORT, 'conviction policy versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS conviction_policy_no_delete
BEFORE DELETE ON conviction_policy_versions BEGIN
    SELECT RAISE(ABORT, 'conviction policy versions are immutable');
END;
