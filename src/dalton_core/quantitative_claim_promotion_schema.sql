-- C2-4: what the deterministic promoter has already turned into a number.
--
-- Append-only.  One row per (company, measure, period, source row): the
-- promoter is idempotent by construction, and this table is how it proves it
-- across process restarts and new filings without re-deriving 16,903 lines.
CREATE TABLE IF NOT EXISTS quantitative_claim_promotions (
    promotion_id TEXT PRIMARY KEY,
    company_ref TEXT NOT NULL,
    metric_or_aspect TEXT NOT NULL,
    period TEXT NOT NULL,
    -- 'statement_line' | 'document_figure' | 'derived_ratio'
    origin_kind TEXT NOT NULL CHECK (origin_kind IN (
        'statement_line', 'document_figure', 'derived_ratio')),
    -- The exact row the number came from: a statement line_id, a figure_id,
    -- or the canonical identity of the two lines a ratio was computed from.
    origin_ref TEXT NOT NULL,
    origin_hash TEXT NOT NULL,
    -- The filed document behind it: an accession for a statement line, a
    -- document_ref for a figure.
    source_document_ref TEXT NOT NULL,
    value TEXT NOT NULL,
    unit TEXT NOT NULL,
    currency TEXT,
    scale TEXT NOT NULL,
    normalized_statement TEXT NOT NULL,
    -- 'staged'      : a candidate claim exists and is awaiting admission
    -- 'admitted'    : it reached claim_versions
    -- 'blocked'     : the staging chain refused it; ``reason`` says which one
    disposition TEXT NOT NULL CHECK (disposition IN ('staged', 'admitted', 'blocked')),
    candidate_claim_ref TEXT,
    claim_version_ref TEXT,
    reason TEXT,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (company_ref, metric_or_aspect, period, origin_ref)
);
CREATE INDEX IF NOT EXISTS idx_quantitative_claim_promotions_company
ON quantitative_claim_promotions (company_ref, metric_or_aspect, period);
CREATE INDEX IF NOT EXISTS idx_quantitative_claim_promotions_origin
ON quantitative_claim_promotions (origin_kind, origin_ref);
CREATE INDEX IF NOT EXISTS idx_quantitative_claim_promotions_disposition
ON quantitative_claim_promotions (disposition, created_at);
CREATE TRIGGER IF NOT EXISTS quantitative_claim_promotions_authorized_insert
BEFORE INSERT ON quantitative_claim_promotions
WHEN dalton_quantitative_claim_promotion_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'quantitative claim promotion insert requires authority');
END;
CREATE TRIGGER IF NOT EXISTS quantitative_claim_promotions_authorized_update
BEFORE UPDATE ON quantitative_claim_promotions
WHEN dalton_quantitative_claim_promotion_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'quantitative claim promotion update requires authority');
END;
CREATE TRIGGER IF NOT EXISTS quantitative_claim_promotions_no_delete
BEFORE DELETE ON quantitative_claim_promotions BEGIN
    SELECT RAISE(ABORT, 'quantitative claim promotions cannot be deleted');
END;
