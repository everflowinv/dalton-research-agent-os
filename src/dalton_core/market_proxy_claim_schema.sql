CREATE TABLE IF NOT EXISTS market_proxy_claim_versions (
    version_id TEXT PRIMARY KEY,
    mapping_ref TEXT NOT NULL,
    target_subject_ref TEXT NOT NULL,
    source_series_company_ref TEXT NOT NULL,
    source_series_version_ref TEXT NOT NULL,
    source_series_version_hash TEXT NOT NULL,
    claim_version_ref TEXT NOT NULL,
    proxy_gap TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(mapping_ref, source_series_version_ref)
);

CREATE TRIGGER IF NOT EXISTS market_proxy_claim_insert_guard
BEFORE INSERT ON market_proxy_claim_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'market proxy claim insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS market_proxy_claim_no_update
BEFORE UPDATE ON market_proxy_claim_versions BEGIN
    SELECT RAISE(ABORT, 'market proxy claims are immutable');
END;
CREATE TRIGGER IF NOT EXISTS market_proxy_claim_no_delete
BEFORE DELETE ON market_proxy_claim_versions BEGIN
    SELECT RAISE(ABORT, 'market proxy claims are immutable');
END;
