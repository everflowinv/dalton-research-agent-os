-- P12e: the industry framework, one append-only chain per industry.
--
-- Same shape as the company dossier's chain and for the same reasons: no
-- pointer table, because "the current one" is the highest version number and a
-- pointer is a second answer that can disagree with the first; a body hash, so
-- a tick that learned nothing is a duplicate rather than a version restating
-- the last one; and an evidence-scope hash, so ADR-0008's "does this version
-- cite anything new" is a read rather than a walk over the record.

CREATE TABLE IF NOT EXISTS industry_framework_versions (
    version_id TEXT PRIMARY KEY,
    framework_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES industry_framework_versions(version_id),
    industry_ref TEXT NOT NULL,
    change_reason TEXT NOT NULL,
    -- What this version says about the industry: the causal-chain sections,
    -- the characteristics, the driver blocks, the comparison table, the debate
    -- summary and the gap list.  Not who asked for it and not when.
    body_hash TEXT NOT NULL,
    -- The union of every ref the version cites, canonicalised.
    evidence_scope_hash TEXT NOT NULL,
    -- The deterministic cross-company table's own hash, so a version whose
    -- prose is unchanged but whose arithmetic moved is visible without
    -- decoding the record.  A filing landing changes this and nothing else.
    comparison_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(framework_ref, version_number)
);

CREATE INDEX IF NOT EXISTS industry_framework_versions_by_industry
ON industry_framework_versions(industry_ref, version_number);

CREATE TRIGGER IF NOT EXISTS industry_framework_insert_guard
BEFORE INSERT ON industry_framework_versions
WHEN dalton_industry_framework_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'industry framework insert requires IndustryFrameworkAuthority');
END;

CREATE TRIGGER IF NOT EXISTS industry_framework_no_update
BEFORE UPDATE ON industry_framework_versions BEGIN
    SELECT RAISE(ABORT, 'industry framework versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS industry_framework_no_delete
BEFORE DELETE ON industry_framework_versions BEGIN
    SELECT RAISE(ABORT, 'industry framework versions are immutable');
END;
