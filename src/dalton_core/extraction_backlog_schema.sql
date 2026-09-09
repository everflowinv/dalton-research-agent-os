-- P10x: what the search wire said about a document, kept.
--
-- Additive: no existing table changes and no existing hash is re-derived.  One
-- row per document, written when the document is first read and never
-- rewritten -- the bytes it describes are pinned by a content hash, so its
-- publisher and its company list cannot honestly change afterwards.
--
-- ``covered_subjects_json`` is the mission's own subjects the document names,
-- and it is the field the multi-company admission reads: a broker note naming
-- Accenture, Cognizant and EPAM produces a Claim for each of them instead of
-- for whichever query happened to return it.  An industry report naming no
-- covered company keeps an empty list, which is the owner's rule stored rather
-- than re-derived: it stays industry-level.
CREATE TABLE IF NOT EXISTS document_provenance_records (
    document_ref TEXT PRIMARY KEY,
    source_ref TEXT NOT NULL,
    spec_ref TEXT,
    provenance_tier TEXT NOT NULL,
    -- The publishing house verbatim as the wire named it, and the equality key
    -- with the legal dressing removed.  Two notes with one key are one source.
    broker TEXT,
    broker_key TEXT,
    title TEXT,
    named_companies_json TEXT NOT NULL,
    covered_subjects_json TEXT NOT NULL,
    published_at TEXT,
    metadata_seen INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_document_provenance_tier
ON document_provenance_records(provenance_tier, broker_key, document_ref);
CREATE INDEX IF NOT EXISTS idx_document_provenance_broker
ON document_provenance_records(broker_key, document_ref);

CREATE TRIGGER IF NOT EXISTS document_provenance_records_no_update
BEFORE UPDATE ON document_provenance_records BEGIN
    SELECT RAISE(ABORT, 'document provenance records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS document_provenance_records_no_delete
BEFORE DELETE ON document_provenance_records BEGIN
    SELECT RAISE(ABORT, 'document provenance records are append-only'); END;
