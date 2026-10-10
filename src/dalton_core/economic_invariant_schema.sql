PRAGMA foreign_keys = ON;

-- One chain per company per kind of output. A row is a gate verdict: the
-- refusal that stopped a publication, or the pass that cleared a standing
-- refusal. Passes on an already-clear chain are not stored -- the chain is the
-- history of when this system refused to publish and when it stopped refusing,
-- and a row every tick saying "still fine" would bury the two that matter.
CREATE TABLE IF NOT EXISTS economic_invariant_verdicts (
    verdict_id TEXT PRIMARY KEY,
    verdict_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_verdict_id TEXT REFERENCES economic_invariant_verdicts(verdict_id),
    company_ref TEXT NOT NULL,
    output_kind TEXT NOT NULL,
    output_ref TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('available','unavailable')),
    rule_ref TEXT NOT NULL,
    subject_hash TEXT NOT NULL,
    failure_count INTEGER NOT NULL CHECK(failure_count >= 0),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(verdict_ref, version_number)
);

CREATE INDEX IF NOT EXISTS idx_economic_invariant_history
ON economic_invariant_verdicts(verdict_ref, version_number);

CREATE INDEX IF NOT EXISTS idx_economic_invariant_company
ON economic_invariant_verdicts(company_ref, output_kind, version_number);

CREATE TRIGGER IF NOT EXISTS economic_invariant_authorized_insert
BEFORE INSERT ON economic_invariant_verdicts WHEN dalton_economic_invariant_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'economic invariant verdict insert requires EconomicInvariantAuthority'); END;

CREATE TRIGGER IF NOT EXISTS economic_invariant_no_update
BEFORE UPDATE ON economic_invariant_verdicts BEGIN
    SELECT RAISE(ABORT, 'economic invariant verdicts are immutable'); END;

CREATE TRIGGER IF NOT EXISTS economic_invariant_no_delete
BEFORE DELETE ON economic_invariant_verdicts BEGIN
    SELECT RAISE(ABORT, 'economic invariant verdicts are immutable'); END;

-- Batch an (2026-10-09): one verdict per *exact* output version.  The chain
-- above records refusals and their clearings only, so a version that passed
-- left no row naming it, and a verdict on an older model was read as covering
-- the newest one.  Every gate call now also appends the report for the exact
-- version it judged -- pass or refusal -- and publication readers ask for
-- that version's row: no row, no verdict ("未验证"), never "passed".
CREATE TABLE IF NOT EXISTS economic_invariant_version_verdicts (
    output_ref TEXT NOT NULL,
    rule_ref TEXT NOT NULL,
    output_kind TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('available','unavailable')),
    subject_hash TEXT,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(output_ref, rule_ref, subject_hash)
);

CREATE INDEX IF NOT EXISTS idx_economic_invariant_version_company
ON economic_invariant_version_verdicts(company_ref, output_kind, created_at);

CREATE TRIGGER IF NOT EXISTS economic_invariant_version_authorized_insert
BEFORE INSERT ON economic_invariant_version_verdicts WHEN dalton_economic_invariant_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'economic invariant version verdict insert requires EconomicInvariantAuthority'); END;

CREATE TRIGGER IF NOT EXISTS economic_invariant_version_no_update
BEFORE UPDATE ON economic_invariant_version_verdicts BEGIN
    SELECT RAISE(ABORT, 'economic invariant version verdicts are immutable'); END;

CREATE TRIGGER IF NOT EXISTS economic_invariant_version_no_delete
BEFORE DELETE ON economic_invariant_version_verdicts BEGIN
    SELECT RAISE(ABORT, 'economic invariant version verdicts are immutable'); END;
