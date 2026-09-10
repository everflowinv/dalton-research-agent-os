-- P12a: the company dossier, one append-only chain per company.
--
-- No pointer table.  "The current one" is the highest version number of the
-- chain, derived rather than stored, because a pointer is a second answer to a
-- question the chain already answers and the two can disagree.

CREATE TABLE IF NOT EXISTS company_dossier_versions (
    version_id TEXT PRIMARY KEY,
    dossier_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES company_dossier_versions(version_id),
    company_ref TEXT NOT NULL,
    change_reason TEXT NOT NULL,
    -- Everything the version chain is about: the sections, the classification
    -- and the variant view.  Two records with the same body are the same
    -- dossier, whatever occasioned the attempt, so a tick that learned nothing
    -- is a duplicate rather than a version saying what the last one said.
    body_hash TEXT NOT NULL,
    -- The union of every ref the version cites, canonicalised, so that "does
    -- this version cite something the last one did not" is a read rather than
    -- a walk over the record.
    evidence_scope_hash TEXT NOT NULL,
    -- 0.2: exact producer input; NULL means a legacy 0.1 version whose
    -- freshness cannot be reconstructed.
    input_fingerprint TEXT,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(dossier_ref, version_number)
);

CREATE INDEX IF NOT EXISTS company_dossier_versions_by_company
ON company_dossier_versions(company_ref, version_number);

CREATE TRIGGER IF NOT EXISTS company_dossier_insert_guard
BEFORE INSERT ON company_dossier_versions
WHEN dalton_company_dossier_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'company dossier insert requires CompanyDossierAuthority');
END;

CREATE TRIGGER IF NOT EXISTS company_dossier_no_update
BEFORE UPDATE ON company_dossier_versions BEGIN
    SELECT RAISE(ABORT, 'company dossier versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS company_dossier_no_delete
BEFORE DELETE ON company_dossier_versions BEGIN
    SELECT RAISE(ABORT, 'company dossier versions are immutable');
END;
