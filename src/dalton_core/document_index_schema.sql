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
