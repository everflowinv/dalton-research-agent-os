-- 2026-10-07: the fund Excel template registry (vision-gap #4).
--
-- Two append-only books.  ``fund_xlsx_template_versions`` holds every
-- registered workbook template: its redacted style contract (presentation
-- only -- never a cell value, formula or name from the source workbook), its
-- mapping into the export's metric bases and valuation snapshot, and where it
-- came from.  The built-in AMZN-source template is version 1 of
-- ``amzn-source`` and is seeded by code on first use, so every environment
-- starts from the same row.  ``fund_xlsx_template_selections`` holds the
-- owner's choices -- which template a company, an industry classification, or
-- the whole workspace exports with -- one receipt per choice; the choice in
-- force is folded from the receipts (newest per scope wins), never updated.

CREATE TABLE IF NOT EXISTS fund_xlsx_template_versions (
    version_id TEXT PRIMARY KEY,
    template_slug TEXT NOT NULL CHECK(length(trim(template_slug)) > 0),
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES fund_xlsx_template_versions(version_id),
    style_ref TEXT NOT NULL,
    style_hash TEXT NOT NULL,
    origin TEXT NOT NULL CHECK(origin IN ('builtin', 'owner_upload')),
    actor_ref TEXT NOT NULL CHECK(actor_ref LIKE 'human:%' OR actor_ref = 'system:builtin'),
    idempotency_key TEXT UNIQUE,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE(template_slug, version_number)
);

CREATE TABLE IF NOT EXISTS fund_xlsx_template_selections (
    receipt_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL UNIQUE CHECK(sequence >= 1),
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('company', 'industry', 'default')),
    scope_ref TEXT NOT NULL CHECK(length(trim(scope_ref)) > 0),
    action TEXT NOT NULL CHECK(action IN ('select', 'clear')),
    template_version_id TEXT REFERENCES fund_xlsx_template_versions(version_id),
    prior_receipt_id TEXT REFERENCES fund_xlsx_template_selections(receipt_id),
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    actor_ref TEXT NOT NULL CHECK(actor_ref LIKE 'human:%'),
    idempotency_key TEXT UNIQUE,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    CHECK((action = 'select') = (template_version_id IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS fund_xlsx_template_selections_by_scope
ON fund_xlsx_template_selections(scope_kind, scope_ref, sequence DESC);

CREATE TRIGGER IF NOT EXISTS fund_xlsx_template_versions_insert_guard
BEFORE INSERT ON fund_xlsx_template_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'fund template version insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS fund_xlsx_template_versions_no_update
BEFORE UPDATE ON fund_xlsx_template_versions BEGIN
    SELECT RAISE(ABORT, 'fund template versions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS fund_xlsx_template_versions_no_delete
BEFORE DELETE ON fund_xlsx_template_versions BEGIN
    SELECT RAISE(ABORT, 'fund template versions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS fund_xlsx_template_selections_insert_guard
BEFORE INSERT ON fund_xlsx_template_selections WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'fund template selection insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS fund_xlsx_template_selections_no_update
BEFORE UPDATE ON fund_xlsx_template_selections BEGIN
    SELECT RAISE(ABORT, 'fund template selections are append-only');
END;
CREATE TRIGGER IF NOT EXISTS fund_xlsx_template_selections_no_delete
BEFORE DELETE ON fund_xlsx_template_selections BEGIN
    SELECT RAISE(ABORT, 'fund template selections are append-only');
END;
