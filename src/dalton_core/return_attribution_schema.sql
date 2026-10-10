-- ADR-0014: holding-period return attribution and buy-side expectation sources.
--
-- Four append-only, derived_deterministic authorities.  Every row is written
-- by ReturnAttributionAuthority through a DaltonStore; nothing is updated or
-- deleted.  A new version is appended only when the body (everything except
-- timestamps and the version chain) differs from the newest version of the
-- same ref.

-- IncomeBridgeVersion: one company, one fiscal period, as filed.
CREATE TABLE IF NOT EXISTS income_bridge_versions (
    version_id TEXT PRIMARY KEY,
    bridge_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES income_bridge_versions(version_id),
    company_ref TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    period_semantics TEXT NOT NULL CHECK(period_semantics IN ('quarterly', 'year_to_date', 'annual')),
    status TEXT NOT NULL CHECK(status IN ('complete', 'residual', 'missing_rows')),
    body_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(bridge_ref, version_number)
);
CREATE INDEX IF NOT EXISTS income_bridge_versions_by_company
ON income_bridge_versions(company_ref, period_end, version_number);

-- PerShareSeriesVersion: one company, every filed period, with TTM points.
CREATE TABLE IF NOT EXISTS per_share_series_versions (
    version_id TEXT PRIMARY KEY,
    series_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES per_share_series_versions(version_id),
    company_ref TEXT NOT NULL,
    period_count INTEGER NOT NULL CHECK(period_count >= 0),
    missing_count INTEGER NOT NULL CHECK(missing_count >= 0),
    body_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(series_ref, version_number)
);
CREATE INDEX IF NOT EXISTS per_share_series_versions_by_company
ON per_share_series_versions(company_ref, version_number);

-- HoldingPeriodReturnBridge: realised and expected legs for one company.
CREATE TABLE IF NOT EXISTS holding_period_return_bridges (
    version_id TEXT PRIMARY KEY,
    bridge_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES holding_period_return_bridges(version_id),
    company_ref TEXT NOT NULL,
    as_of TEXT NOT NULL,
    available_leg_count INTEGER NOT NULL CHECK(available_leg_count >= 0),
    body_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(bridge_ref, version_number)
);
CREATE INDEX IF NOT EXISTS holding_period_return_bridges_by_company
ON holding_period_return_bridges(company_ref, version_number);

-- ExpectationSourceTag: the typed source of one expectation-bearing input
-- (a consensus version, one debate's market position, a conviction call's
-- market view).  Subjects are immutable, so a tag is written once per
-- subject, field and classifier version.
CREATE TABLE IF NOT EXISTS expectation_source_tags (
    tag_id TEXT PRIMARY KEY,
    subject_kind TEXT NOT NULL CHECK(subject_kind IN ('consensus_estimate', 'debate_map', 'conviction_call')),
    subject_ref TEXT NOT NULL,
    subject_hash TEXT NOT NULL,
    field TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    classifier_ref TEXT NOT NULL,
    primary_kind TEXT CHECK(primary_kind IS NULL OR primary_kind IN (
        'consensus', 'management_guidance', 'sell_side_note', 'owner_view', 'market_implied')),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_ref, field, classifier_ref)
);
CREATE INDEX IF NOT EXISTS expectation_source_tags_by_company
ON expectation_source_tags(company_ref, created_at);

CREATE TRIGGER IF NOT EXISTS income_bridge_versions_insert_guard
BEFORE INSERT ON income_bridge_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'income bridge insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS income_bridge_versions_no_update
BEFORE UPDATE ON income_bridge_versions BEGIN
    SELECT RAISE(ABORT, 'income bridge versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS income_bridge_versions_no_delete
BEFORE DELETE ON income_bridge_versions BEGIN
    SELECT RAISE(ABORT, 'income bridge versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS per_share_series_versions_insert_guard
BEFORE INSERT ON per_share_series_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'per-share series insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS per_share_series_versions_no_update
BEFORE UPDATE ON per_share_series_versions BEGIN
    SELECT RAISE(ABORT, 'per-share series versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS per_share_series_versions_no_delete
BEFORE DELETE ON per_share_series_versions BEGIN
    SELECT RAISE(ABORT, 'per-share series versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS holding_period_return_bridges_insert_guard
BEFORE INSERT ON holding_period_return_bridges WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'return bridge insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS holding_period_return_bridges_no_update
BEFORE UPDATE ON holding_period_return_bridges BEGIN
    SELECT RAISE(ABORT, 'return bridges are immutable');
END;
CREATE TRIGGER IF NOT EXISTS holding_period_return_bridges_no_delete
BEFORE DELETE ON holding_period_return_bridges BEGIN
    SELECT RAISE(ABORT, 'return bridges are immutable');
END;

CREATE TRIGGER IF NOT EXISTS expectation_source_tags_insert_guard
BEFORE INSERT ON expectation_source_tags WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'expectation source tag insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS expectation_source_tags_no_update
BEFORE UPDATE ON expectation_source_tags BEGIN
    SELECT RAISE(ABORT, 'expectation source tags are immutable');
END;
CREATE TRIGGER IF NOT EXISTS expectation_source_tags_no_delete
BEFORE DELETE ON expectation_source_tags BEGIN
    SELECT RAISE(ABORT, 'expectation source tags are immutable');
END;

-- Actual paid cash for one exact share class; source coverage also proves zero.
CREATE TABLE IF NOT EXISTS dividend_payment_evidence_versions (
    version_id TEXT PRIMARY KEY, company_ref TEXT NOT NULL,
    record_json TEXT NOT NULL, content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS dividend_payment_evidence_insert_guard
BEFORE INSERT ON dividend_payment_evidence_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'dividend evidence insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS dividend_payment_evidence_no_update
BEFORE UPDATE ON dividend_payment_evidence_versions BEGIN
    SELECT RAISE(ABORT, 'dividend evidence is immutable');
END;
CREATE TRIGGER IF NOT EXISTS dividend_payment_evidence_no_delete
BEFORE DELETE ON dividend_payment_evidence_versions BEGIN
    SELECT RAISE(ABORT, 'dividend evidence is immutable');
END;
