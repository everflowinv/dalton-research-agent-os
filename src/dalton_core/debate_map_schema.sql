-- P12c: the map of what the market disagrees about, one version chain per
-- subject (a company, or the industry).
--
-- A debate map is not a summary of the Claims underneath it. It is the one
-- place the system writes down *where we differ from the market and what would
-- settle it*, which is the only part of research that has any value: agreeing
-- with consensus in more words is not a finding. So every debate carries a
-- bull position, a bear position, where the market stands, where we stand, and
-- -- when it moves -- which side has been gaining and on what evidence.
--
-- Append-only, per ADR-0008. `open`, `shifting` and `resolved` are states of a
-- *version*, never of a debate: a debate that resolved in March and reopened
-- in June is two versions of one chain, and the chain is what the owner reads
-- to see the mind change. Nothing is edited and nothing is deleted, so a
-- superseded position stays legible beside the one that replaced it.
--
-- A new version exists only when it can say what it learned: a debate that was
-- not there before, a status that moved, or a reference the previous version
-- did not cite. Re-recording an unchanged map is a `duplicate`, not a version,
-- which is what stops a tick that runs every five minutes from growing a chain
-- made of identical rows and calling it an evolving view.
--
-- The candidates the constitution refused are stored *in* the version rather
-- than thrown away, because "we considered this question and it did not
-- qualify" is a research fact, and a gate whose rejections are invisible is a
-- gate nobody can audit.

CREATE TABLE IF NOT EXISTS debate_map_versions (
    version_id TEXT PRIMARY KEY,
    map_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES debate_map_versions(version_id),
    subject_ref TEXT NOT NULL,
    subject_kind TEXT NOT NULL CHECK(subject_kind IN ('company', 'industry')),
    change_reason TEXT NOT NULL CHECK(change_reason IN (
        'filing_actual', 'driver_event', 'assumption_review',
        'evidence_thicker', 'human_revision', 'mission_rebind')),
    evidence_fingerprint TEXT NOT NULL,
    debate_count INTEGER NOT NULL CHECK(debate_count >= 0),
    live_count INTEGER NOT NULL CHECK(live_count >= 0),
    rejected_count INTEGER NOT NULL CHECK(rejected_count >= 0),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(map_ref, version_number)
);

-- The reads this exists to serve: the current map for one subject, and the
-- weekly brief's "what moved since the version I last read".
CREATE INDEX IF NOT EXISTS debate_map_versions_by_map
ON debate_map_versions(map_ref, version_number);
CREATE INDEX IF NOT EXISTS debate_map_versions_by_subject
ON debate_map_versions(subject_ref, version_number);

CREATE TRIGGER IF NOT EXISTS debate_map_version_insert_guard
BEFORE INSERT ON debate_map_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'debate map version insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS debate_map_version_no_update
BEFORE UPDATE ON debate_map_versions BEGIN
    SELECT RAISE(ABORT, 'debate map versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS debate_map_version_no_delete
BEFORE DELETE ON debate_map_versions BEGIN
    SELECT RAISE(ABORT, 'debate map versions are immutable');
END;
