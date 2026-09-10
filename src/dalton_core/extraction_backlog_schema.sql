-- P10x: what the search wire said about a document, kept.
--
-- Additive: no existing table changes and no existing hash is re-derived.  One
-- row per document, written when the document is first read and never
-- rewritten -- the bytes it describes are pinned by a content hash, so its
-- publisher and its company list cannot honestly change afterwards.
--
-- **Only what the wire said is stored.**  Which of the named companies are
-- *covered* is not: coverage is the mission's business and it changes when the
-- owner publishes a new universe, so storing it would mean a document recorded
-- before a company joined coverage disagrees with the same document read
-- after, and the append-only replay check would call that a conflict.  The
-- covered subjects are therefore derived at read time from
-- ``named_companies_json`` and the mission's own universe.
--
-- Column names are the ones P12c's ``debate_map_draft._ATTRIBUTION_COLUMNS``
-- already looks for -- ``broker``, ``authors``, ``sources``, ``title`` -- so
-- the independence ladder can read this table with no rename and no change on
-- its side.  It reads them off the discovered-document row today, which this
-- lane may not alter; ``document_attribution_rows`` below returns the same
-- shape so integration is one merge rather than a schema migration.
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
    authors TEXT,
    sources TEXT,
    named_companies_json TEXT NOT NULL,
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
