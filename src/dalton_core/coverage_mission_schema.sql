PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS coverage_mission_versions (
    mission_version_id TEXT PRIMARY KEY,
    mission_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES coverage_mission_versions(mission_version_id),
    industry_ref TEXT NOT NULL,
    playbook_version_ref TEXT NOT NULL,
    constitution_version_ref TEXT NOT NULL,
    mandate_version_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(mission_ref, version_number)
);

CREATE TABLE IF NOT EXISTS coverage_mission_pointer (
    mission_ref TEXT PRIMARY KEY,
    mission_version_id TEXT NOT NULL UNIQUE REFERENCES coverage_mission_versions(mission_version_id),
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coverage_mission_stage_records (
    record_id TEXT PRIMARY KEY,
    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
    company_ref TEXT NOT NULL,
    stage_ref TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('entered','gate_passed','gate_failed')),
    actor_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coverage_mission_stage_claims (
    record_id TEXT PRIMARY KEY,
    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
    company_ref TEXT NOT NULL,
    stage_ref TEXT NOT NULL,
    claim_version_ref TEXT NOT NULL,
    claim_version_hash TEXT NOT NULL,
    evidence_version_ref TEXT NOT NULL,
    evidence_version_hash TEXT NOT NULL,
    source_location TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(mission_version_ref, claim_version_ref)
);

CREATE TABLE IF NOT EXISTS coverage_mission_sec_dispatches (
    dispatch_id TEXT PRIMARY KEY,
    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
    mission_version_hash TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    ticker TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    form TEXT NOT NULL CHECK(form IN ('10-Q','10-K')),
    filed_from TEXT NOT NULL,
    filed_to TEXT NOT NULL,
    expected_accession TEXT NOT NULL,
    observation_ref TEXT NOT NULL,
    authorization_json TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','launched','rejected')),
    ticket_ref TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- P9d-1: one launched discovery child per row; settled by the controller
-- tick from the child's ticket.  Rows are never deleted.
CREATE TABLE IF NOT EXISTS coverage_mission_discovery_dispatches (
    dispatch_id TEXT PRIMARY KEY,
    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
    mission_version_hash TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    discovery_plan_ref TEXT NOT NULL,
    discovery_plan_hash TEXT NOT NULL,
    spec_ref TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    authorization_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('launched','succeeded','failed','rejected')),
    ticket_ref TEXT NOT NULL,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- P9d-1: append-only record of one completed library search bound to the
-- exact Core connector invocation and source envelope it produced.
CREATE TABLE IF NOT EXISTS coverage_mission_source_discoveries (
    record_id TEXT PRIMARY KEY,
    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
    mission_version_hash TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    discovery_plan_ref TEXT NOT NULL,
    discovery_plan_hash TEXT NOT NULL,
    spec_ref TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    connector_invocation_ref TEXT NOT NULL,
    source_envelope_ref TEXT NOT NULL,
    source_envelope_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(mission_version_ref, source_envelope_ref)
);

-- P9d-1: one row per (mission version, discovered document); status moves
-- discovered -> acquisition_launched -> acquired | acquisition_failed.
CREATE TABLE IF NOT EXISTS coverage_mission_discovered_documents (
    record_id TEXT PRIMARY KEY,
    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
    company_ref TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    discovery_ref TEXT NOT NULL REFERENCES coverage_mission_source_discoveries(record_id),
    status TEXT NOT NULL CHECK(status IN ('discovered','already_in_authority','acquisition_launched','acquired','acquisition_failed')),
    ticket_ref TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- P9d-13: the URL host for web documents, so the queue can be ordered and
    -- read by a human without re-opening the raw search bytes.  NULL for
    -- sources whose refs are not URLs and for rows recorded before P9d-13
    -- (backfilled by the coordinator from the exact discovery envelope).
    host TEXT,
    UNIQUE(mission_version_ref, document_ref)
);

CREATE TABLE IF NOT EXISTS coverage_mission_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_coverage_mission_history
ON coverage_mission_versions(mission_ref, version_number);
CREATE INDEX IF NOT EXISTS idx_coverage_mission_stage_by_company
ON coverage_mission_stage_records(mission_version_ref, company_ref, stage_ref, created_at);
CREATE INDEX IF NOT EXISTS idx_coverage_mission_claims_by_company
ON coverage_mission_stage_claims(mission_version_ref, company_ref, stage_ref, created_at);
CREATE INDEX IF NOT EXISTS idx_coverage_mission_sec_dispatch_pending
ON coverage_mission_sec_dispatches(status, created_at, dispatch_id);
CREATE INDEX IF NOT EXISTS idx_coverage_mission_discovery_dispatch_status
ON coverage_mission_discovery_dispatches(status, created_at, dispatch_id);
CREATE INDEX IF NOT EXISTS idx_coverage_mission_discovery_dispatch_by_spec
ON coverage_mission_discovery_dispatches(mission_version_ref, company_ref, spec_ref, created_at);
CREATE INDEX IF NOT EXISTS idx_coverage_mission_discoveries_by_company
ON coverage_mission_source_discoveries(mission_version_ref, company_ref, spec_ref, created_at);
CREATE INDEX IF NOT EXISTS idx_coverage_mission_discovered_documents_status
ON coverage_mission_discovered_documents(status, created_at, record_id);

CREATE TRIGGER IF NOT EXISTS coverage_mission_discovery_dispatches_authorized_insert
BEFORE INSERT ON coverage_mission_discovery_dispatches WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission discovery dispatch insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_discovery_dispatches_authorized_update
BEFORE UPDATE ON coverage_mission_discovery_dispatches WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission discovery dispatch update requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_discovery_dispatches_no_delete
BEFORE DELETE ON coverage_mission_discovery_dispatches BEGIN SELECT RAISE(ABORT, 'mission discovery dispatches cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_source_discoveries_authorized_insert
BEFORE INSERT ON coverage_mission_source_discoveries WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission source discovery insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_source_discoveries_no_update
BEFORE UPDATE ON coverage_mission_source_discoveries BEGIN SELECT RAISE(ABORT, 'mission source discoveries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_source_discoveries_no_delete
BEFORE DELETE ON coverage_mission_source_discoveries BEGIN SELECT RAISE(ABORT, 'mission source discoveries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_discovered_documents_authorized_insert
BEFORE INSERT ON coverage_mission_discovered_documents WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission discovered document insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_discovered_documents_authorized_update
BEFORE UPDATE ON coverage_mission_discovered_documents WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission discovered document update requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_discovered_documents_no_delete
BEFORE DELETE ON coverage_mission_discovered_documents BEGIN SELECT RAISE(ABORT, 'mission discovered documents cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS coverage_mission_versions_authorized_insert
BEFORE INSERT ON coverage_mission_versions WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'coverage mission insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_pointer_authorized_insert
BEFORE INSERT ON coverage_mission_pointer WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'coverage mission pointer insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_pointer_authorized_update
BEFORE UPDATE ON coverage_mission_pointer WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'coverage mission pointer update requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_stage_records_authorized_insert
BEFORE INSERT ON coverage_mission_stage_records WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'coverage mission stage record insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_stage_claims_authorized_insert
BEFORE INSERT ON coverage_mission_stage_claims WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'coverage mission stage claim insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_sec_dispatches_authorized_insert
BEFORE INSERT ON coverage_mission_sec_dispatches WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission SEC dispatch insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_sec_dispatches_authorized_update
BEFORE UPDATE ON coverage_mission_sec_dispatches WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission SEC dispatch update requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_idempotency_authorized_insert
BEFORE INSERT ON coverage_mission_idempotency WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'coverage mission idempotency insert requires CoverageMissionAuthority'); END;

CREATE TRIGGER IF NOT EXISTS coverage_mission_versions_no_update
BEFORE UPDATE ON coverage_mission_versions BEGIN SELECT RAISE(ABORT, 'coverage mission versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_versions_no_delete
BEFORE DELETE ON coverage_mission_versions BEGIN SELECT RAISE(ABORT, 'coverage mission versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_pointer_no_delete
BEFORE DELETE ON coverage_mission_pointer BEGIN SELECT RAISE(ABORT, 'coverage mission pointer cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_stage_records_no_update
BEFORE UPDATE ON coverage_mission_stage_records BEGIN SELECT RAISE(ABORT, 'coverage mission stage records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_stage_records_no_delete
BEFORE DELETE ON coverage_mission_stage_records BEGIN SELECT RAISE(ABORT, 'coverage mission stage records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_stage_claims_no_update
BEFORE UPDATE ON coverage_mission_stage_claims BEGIN SELECT RAISE(ABORT, 'coverage mission stage claims are append-only'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_stage_claims_no_delete
BEFORE DELETE ON coverage_mission_stage_claims BEGIN SELECT RAISE(ABORT, 'coverage mission stage claims are append-only'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_sec_dispatches_no_delete
BEFORE DELETE ON coverage_mission_sec_dispatches BEGIN SELECT RAISE(ABORT, 'mission SEC dispatches cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_idempotency_no_update
BEFORE UPDATE ON coverage_mission_idempotency BEGIN SELECT RAISE(ABORT, 'coverage mission idempotency is immutable'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_idempotency_no_delete
BEFORE DELETE ON coverage_mission_idempotency BEGIN SELECT RAISE(ABORT, 'coverage mission idempotency is immutable'); END;

-- P9d-2: the human extraction queue for documents the discovery loop
-- acquired.  Automation registers one review per acquired document (under
-- the source_discovery scope); only a human resolves it by staging a
-- transcript candidate or dismissing it.  Rows are never deleted; state
-- transitions are authority UPDATEs validated in coverage_mission.py.
CREATE TABLE IF NOT EXISTS coverage_mission_document_reviews (
    review_id TEXT PRIMARY KEY,
    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
    company_ref TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    discovered_document_ref TEXT NOT NULL REFERENCES coverage_mission_discovered_documents(record_id),
    state TEXT NOT NULL CHECK(state IN ('awaiting_human_extraction','extraction_staged','dismissed')),
    candidate_claim_version_ref TEXT,
    rationale TEXT,
    registered_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(mission_version_ref, document_ref)
);

CREATE INDEX IF NOT EXISTS idx_coverage_mission_document_reviews_state
ON coverage_mission_document_reviews(state, created_at, review_id);

CREATE TRIGGER IF NOT EXISTS coverage_mission_document_reviews_authorized_insert
BEFORE INSERT ON coverage_mission_document_reviews WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission document review insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_document_reviews_authorized_update
BEFORE UPDATE ON coverage_mission_document_reviews WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'mission document review update requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_document_reviews_no_delete
BEFORE DELETE ON coverage_mission_document_reviews BEGIN SELECT RAISE(ABORT, 'mission document reviews cannot be deleted'); END;

-- Human confirmation journal for the multi-store correction -> citation ->
-- CandidateStaging workflow. Not a second candidate or evidence authority.
CREATE TABLE IF NOT EXISTS coverage_mission_document_staging_requests (
    request_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    request_hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS mission_document_staging_request_no_update
BEFORE UPDATE ON coverage_mission_document_staging_requests BEGIN SELECT RAISE(ABORT, 'human staging requests are immutable'); END;
CREATE TRIGGER IF NOT EXISTS mission_document_staging_request_no_delete
BEFORE DELETE ON coverage_mission_document_staging_requests BEGIN SELECT RAISE(ABORT, 'human staging requests are immutable'); END;

-- P11q: what the market was seen calling a company's figures.
--
-- One row is one document naming one measure, verified against the quote that
-- proposed it.  A requirement is not stored: it is derived from these rows by
-- ``establish_requirements``, so the corroboration rule stays a single piece of
-- logic and raising or lowering it does not need a migration.
--
-- The UNIQUE key is (company, metric, document) rather than a row id, so a
-- document read twice contributes once.  That is the corroboration rule
-- enforced by the storage itself: counting mentions would let one verbose note
-- create a requirement on its own.
CREATE TABLE IF NOT EXISTS coverage_mission_metric_observations (
    observation_id TEXT PRIMARY KEY,
    company_ref TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    metric_ref TEXT NOT NULL,
    label TEXT NOT NULL,
    unit TEXT NOT NULL,
    evidence_phrase TEXT NOT NULL,
    quote_id TEXT NOT NULL,
    citation_text TEXT NOT NULL,
    observed_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(company_ref, metric_ref, document_ref)
);

CREATE INDEX IF NOT EXISTS idx_coverage_mission_metric_observations_company
ON coverage_mission_metric_observations(company_ref, metric_ref);

CREATE TRIGGER IF NOT EXISTS coverage_mission_metric_observations_authorized_insert
BEFORE INSERT ON coverage_mission_metric_observations WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'metric observation insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_metric_observations_no_update
BEFORE UPDATE ON coverage_mission_metric_observations BEGIN SELECT RAISE(ABORT, 'metric observations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_metric_observations_no_delete
BEFORE DELETE ON coverage_mission_metric_observations BEGIN SELECT RAISE(ABORT, 'metric observations are append-only'); END;

-- P11w: figures read out of a document, each verified against the bytes it
-- cited and graded by the kind of document it came from.
--
-- Two things make a row here different from a number a model produced:
--
--   * the digits and the as-reported label were both found in the exact quote
--     the figure cites, checked deterministically before the row was written,
--     and the quote and the manifest hash are stored so the check can be run
--     again by anyone;
--   * ``source_grade`` says what the citation is a citation *of*. A figure in
--     a 10-K is the company's published number; the same figure in a call
--     transcript is a record that someone said it. Both are kept. Storing them
--     identically would throw the difference away at the one moment it was
--     free to record.
--
-- The UNIQUE key is the citation, so the same document read twice contributes
-- one row, and the same figure filed and spoken is two rows -- which is the
-- point.
CREATE TABLE IF NOT EXISTS coverage_mission_document_figures (
    figure_id TEXT PRIMARY KEY,
    company_ref TEXT NOT NULL,
    review_ref TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    source_manifest_hash TEXT NOT NULL,
    quote_id TEXT NOT NULL,
    citation_text TEXT NOT NULL,
    metric_ref TEXT NOT NULL,
    as_reported_label TEXT NOT NULL,
    period TEXT NOT NULL,
    value TEXT NOT NULL,
    unit TEXT NOT NULL,
    currency TEXT,
    scale TEXT,
    basis TEXT NOT NULL,
    source_grade TEXT NOT NULL CHECK(
        source_grade IN ('company-filed-document','earnings-call-transcript')),
    verified_by TEXT NOT NULL,
    observed_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(company_ref, metric_ref, period, document_ref, quote_id)
);

CREATE INDEX IF NOT EXISTS idx_coverage_mission_document_figures_company
ON coverage_mission_document_figures(company_ref, metric_ref, period);
CREATE INDEX IF NOT EXISTS idx_coverage_mission_document_figures_grade
ON coverage_mission_document_figures(source_grade, company_ref);

CREATE TRIGGER IF NOT EXISTS coverage_mission_document_figures_authorized_insert
BEFORE INSERT ON coverage_mission_document_figures WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'document figure insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_document_figures_no_update
BEFORE UPDATE ON coverage_mission_document_figures BEGIN SELECT RAISE(ABORT, 'document figures are append-only'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_document_figures_no_delete
BEFORE DELETE ON coverage_mission_document_figures BEGIN SELECT RAISE(ABORT, 'document figures are append-only'); END;

-- P12b: a SEC dispatch that has finished running.
--
-- ``coverage_mission_sec_dispatches.status`` has no terminal success state --
-- its CHECK is ('pending','launched','rejected') -- so a dispatch that ran
-- perfectly stayed 'launched' forever. ``_open_dispatches`` counts launched
-- rows to avoid queueing faster than the lane can run, which meant that after
-- the first batch every company looked permanently busy and the quarterly
-- financials froze: five companies, thirty-five dispatches, none settled, no
-- new quarter dispatched for a day.
--
-- Settlement is journalled rather than written back onto the row, because
-- widening the CHECK would mean rebuilding a live table for a fact that is
-- append-only anyway: this dispatch's run is over, and here is how it ended.
CREATE TABLE IF NOT EXISTS coverage_mission_sec_dispatch_settlements (
    dispatch_id TEXT PRIMARY KEY
        REFERENCES coverage_mission_sec_dispatches(dispatch_id),
    ticket_ref TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('finished','orphaned')),
    detail TEXT,
    settled_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS coverage_mission_sec_dispatch_settlements_authorized_insert
BEFORE INSERT ON coverage_mission_sec_dispatch_settlements WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'SEC dispatch settlement insert requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_sec_dispatch_settlements_no_update
BEFORE UPDATE ON coverage_mission_sec_dispatch_settlements BEGIN SELECT RAISE(ABORT, 'SEC dispatch settlements are append-only'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_sec_dispatch_settlements_no_delete
BEFORE DELETE ON coverage_mission_sec_dispatch_settlements BEGIN SELECT RAISE(ABORT, 'SEC dispatch settlements are append-only'); END;

-- P12i: a figure that should never have been recorded.
--
-- Retraction rather than deletion, and the reason is the point. Two of the
-- first figures this system stored were "EPAM revenue = 14.3 billion RMB"
-- (a Haier call) and "revenue of AUD 169 million" (an EOS call): the digits
-- were verified against the bytes they cited, and the bytes were about another
-- company. Deleting the rows would leave nothing to say that happened, and the
-- next person to trust a figure deserves to know which ones were wrong and
-- why. A retracted figure is excluded from every read; it is not data any more,
-- it is a record of a mistake.
CREATE TABLE IF NOT EXISTS coverage_mission_document_figure_retractions (
    figure_id TEXT PRIMARY KEY
        REFERENCES coverage_mission_document_figures(figure_id),
    reason TEXT NOT NULL,
    retracted_by TEXT NOT NULL,
    retracted_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS coverage_mission_document_figure_retractions_authorized_insert
BEFORE INSERT ON coverage_mission_document_figure_retractions WHEN dalton_coverage_mission_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'figure retraction requires CoverageMissionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_document_figure_retractions_no_update
BEFORE UPDATE ON coverage_mission_document_figure_retractions BEGIN SELECT RAISE(ABORT, 'figure retractions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS coverage_mission_document_figure_retractions_no_delete
BEFORE DELETE ON coverage_mission_document_figure_retractions BEGIN SELECT RAISE(ABORT, 'figure retractions are append-only'); END;
