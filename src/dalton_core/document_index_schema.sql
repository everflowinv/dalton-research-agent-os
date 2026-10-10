-- Disposable, read-only projection owned by DocumentIndex.  It has no
-- foreign keys or write path into Dalton authority tables on purpose.
CREATE TABLE IF NOT EXISTS document_index_snapshot (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    snapshot_id TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_index_documents (
    rowid INTEGER PRIMARY KEY,
    artifact_version_ref TEXT NOT NULL UNIQUE,
    artifact_version_hash TEXT NOT NULL,
    artifact_ref TEXT NOT NULL,
    artifact_version INTEGER NOT NULL,
    artifact_content_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    media_type TEXT NOT NULL,
    access_class TEXT NOT NULL,
    source_envelope_ref TEXT,
    source_envelope_hash TEXT,
    source_type TEXT,
    source_operation TEXT,
    source_record_refs_json TEXT NOT NULL,
    company_refs_json TEXT NOT NULL,
    company_parser_ref TEXT,
    published_at TEXT,
    updated_at TEXT,
    as_of TEXT,
    retrieved_at TEXT,
    document_date TEXT NOT NULL,
    source_metadata TEXT NOT NULL,
    -- Disposable searchable body.  It is never returned as authority and is
    -- deleted together with this projection; its hash/ref stay in record_json.
    extracted_text TEXT NOT NULL,
    extracted_text_ref TEXT NOT NULL,
    extracted_text_hash TEXT NOT NULL,
    extracted_text_size_bytes INTEGER NOT NULL,
    input_ref TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_index_companies (
    document_rowid INTEGER NOT NULL REFERENCES document_index_documents(rowid) ON DELETE CASCADE,
    company_ref TEXT NOT NULL,
    PRIMARY KEY (document_rowid, company_ref)
);

CREATE INDEX IF NOT EXISTS document_index_documents_source_type
    ON document_index_documents(source_type);
CREATE INDEX IF NOT EXISTS document_index_documents_media_type
    ON document_index_documents(media_type);
CREATE INDEX IF NOT EXISTS document_index_documents_date
    ON document_index_documents(document_date);

CREATE VIRTUAL TABLE IF NOT EXISTS document_index_fts USING fts5(
    title,
    source_metadata,
    extracted_text,
    content='document_index_documents',
    content_rowid='rowid',
    -- trigram gives useful >=3-codepoint substring recall for CJK.  It is not
    -- a general Chinese tokenizer: two-codepoint queries (for example 存储)
    -- may miss and callers must retain exact refs for any materialization.
    tokenize='trigram'
);

-- ---------------------------------------------------------------------------
-- Batch A (证据底座, 2026-10-09): the passage index over every acquired body.
-- Owned by evidence_index.py.  Still a disposable projection *except*
-- material_arrivals, which is an append-only ledger later batches read to
-- decide when enough new material has accumulated to redraft.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence_index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_documents (
    doc_id INTEGER PRIMARY KEY,
    document_ref TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT,
    house TEXT,
    document_date TEXT,
    period TEXT,
    url TEXT,
    renderer TEXT NOT NULL,
    text_chars INTEGER NOT NULL,
    passage_count INTEGER NOT NULL,
    source_key TEXT NOT NULL,
    acquired_at TEXT,
    indexed_at TEXT NOT NULL,
    -- A newer body under the same document_ref; the older one stays citable.
    superseded_by INTEGER,
    metadata_json TEXT NOT NULL,
    UNIQUE(document_ref, text_sha256)
);
CREATE INDEX IF NOT EXISTS evidence_documents_ref ON evidence_documents(document_ref);
CREATE INDEX IF NOT EXISTS evidence_documents_kind_date ON evidence_documents(kind, document_date);

CREATE TABLE IF NOT EXISTS evidence_document_companies (
    doc_id INTEGER NOT NULL REFERENCES evidence_documents(doc_id),
    company_ref TEXT NOT NULL,
    -- filed: the mission filed it under this company; listed: the document's own
    -- company list names it; named: its text names it.
    basis TEXT NOT NULL CHECK (basis IN ('filed', 'listed', 'named')),
    PRIMARY KEY (doc_id, company_ref, basis)
);
CREATE INDEX IF NOT EXISTS evidence_document_companies_company
    ON evidence_document_companies(company_ref, doc_id);

CREATE TABLE IF NOT EXISTS evidence_passages (
    passage_id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES evidence_documents(doc_id),
    ordinal INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    passage_sha256 TEXT NOT NULL,
    heading TEXT,
    speakers_json TEXT NOT NULL,
    named_company_refs_json TEXT NOT NULL,
    text TEXT NOT NULL,
    UNIQUE(doc_id, ordinal)
);
CREATE INDEX IF NOT EXISTS evidence_passages_span ON evidence_passages(doc_id, start_offset, end_offset);

CREATE TABLE IF NOT EXISTS evidence_passage_companies (
    passage_id INTEGER NOT NULL REFERENCES evidence_passages(passage_id),
    company_ref TEXT NOT NULL,
    PRIMARY KEY (passage_id, company_ref)
);
CREATE INDEX IF NOT EXISTS evidence_passage_companies_company
    ON evidence_passage_companies(company_ref, passage_id);

-- Contentless: the passage text lives once, in evidence_passages; the FTS
-- table holds only the inverted index.  CJK text is indexed one character per
-- token (spaced by the indexer) so a two-character query such as 存储 is a
-- phrase match rather than a miss.
CREATE VIRTUAL TABLE IF NOT EXISTS evidence_passages_fts USING fts5(
    title,
    heading,
    body,
    content='',
    tokenize='porter unicode61 remove_diacritics 2'
);

-- One row per manifest on disk the indexer has looked at: the resumable
-- backfill cursor and the ledger of indexed document hashes.
CREATE TABLE IF NOT EXISTS evidence_index_ledger (
    source_key TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('indexed', 'skipped', 'failed')),
    document_ref TEXT,
    text_sha256 TEXT,
    doc_id INTEGER,
    reason TEXT,
    attempts INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_index_ledger_hash ON evidence_index_ledger(text_sha256);

-- Append-only: "this company gained this document".  A later batch decides
-- when enough has accumulated (the threshold lives there, not here).
CREATE TABLE IF NOT EXISTS material_arrivals (
    arrival_id INTEGER PRIMARY KEY,
    company_ref TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    kind TEXT NOT NULL,
    period TEXT,
    document_date TEXT,
    acquired_at TEXT,
    indexed_at TEXT NOT NULL,
    digest TEXT NOT NULL,
    -- 1 when the document was acquired before the index existed: it is
    -- material the analyst already had, recorded so the ledger is complete.
    backfill INTEGER NOT NULL CHECK (backfill IN (0, 1)),
    UNIQUE(company_ref, document_ref, digest)
);
CREATE INDEX IF NOT EXISTS material_arrivals_company ON material_arrivals(company_ref, arrival_id);
CREATE TRIGGER IF NOT EXISTS material_arrivals_no_update
BEFORE UPDATE ON material_arrivals BEGIN
    SELECT RAISE(ABORT, 'material arrivals are append-only');
END;
CREATE TRIGGER IF NOT EXISTS material_arrivals_no_delete
BEFORE DELETE ON material_arrivals BEGIN
    SELECT RAISE(ABORT, 'material arrivals are append-only');
END;
