-- Batch B (2026-10-09): broker estimate-table figures (street_estimate_tables).
-- Append-only.  One figure: one house, one note, one fiscal period, one metric,
-- one kind (house / house_prior / street_per_broker).  One scan per note per
-- extractor version; a newer extractor scans again and appends.
CREATE TABLE IF NOT EXISTS street_estimate_figures (
    figure_id TEXT PRIMARY KEY,
    company_ref TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    house TEXT,
    published_on TEXT,
    period TEXT NOT NULL,
    period_kind TEXT NOT NULL CHECK(period_kind IN ('quarter','fiscal_year')),
    metric TEXT NOT NULL CHECK(metric IN ('eps','revenue')),
    estimate_kind TEXT NOT NULL CHECK(estimate_kind IN ('house','house_prior','street_per_broker')),
    value TEXT NOT NULL,
    extraction_method TEXT NOT NULL CHECK(extraction_method IN ('deterministic','model')),
    parser_version TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS street_estimate_figures_by_company
ON street_estimate_figures(company_ref, period, metric, published_on);
CREATE TRIGGER IF NOT EXISTS street_estimate_figures_insert_guard
BEFORE INSERT ON street_estimate_figures WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'street estimate figure insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS street_estimate_figures_no_update
BEFORE UPDATE ON street_estimate_figures BEGIN
    SELECT RAISE(ABORT, 'street estimate figures are append-only');
END;
CREATE TRIGGER IF NOT EXISTS street_estimate_figures_no_delete
BEFORE DELETE ON street_estimate_figures BEGIN
    SELECT RAISE(ABORT, 'street estimate figures are append-only');
END;

CREATE TABLE IF NOT EXISTS street_estimate_figure_scans (
    scan_id TEXT PRIMARY KEY,
    company_ref TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('recorded','none_found','needs_model','model_recorded','refused')),
    reason TEXT,
    figure_count INTEGER NOT NULL CHECK(figure_count >= 0),
    extractor_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    scanned_at TEXT NOT NULL,
    UNIQUE(company_ref, document_ref, extractor_ref)
);
CREATE TRIGGER IF NOT EXISTS street_estimate_figure_scans_insert_guard
BEFORE INSERT ON street_estimate_figure_scans WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'street estimate figure scan insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS street_estimate_figure_scans_no_update
BEFORE UPDATE ON street_estimate_figure_scans BEGIN
    SELECT RAISE(ABORT, 'street estimate figure scans are append-only');
END;
CREATE TRIGGER IF NOT EXISTS street_estimate_figure_scans_no_delete
BEFORE DELETE ON street_estimate_figure_scans BEGIN
    SELECT RAISE(ABORT, 'street estimate figure scans are append-only');
END;
