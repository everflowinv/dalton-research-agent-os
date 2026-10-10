-- Batch an (2026-10-09): cross-product consistency.
--
-- A conflict is two current products saying incompatible things about one
-- company (a gate classification against a dossier's, a confidence field
-- against its own prose, a model's operating loss against a return bridge's
-- positive earnings ...).  Detection is deterministic and read-only
-- (product_consistency.detect_conflicts); this table is where a detected
-- conflict is written down once, append-only, naming the exact product
-- versions involved.  A conflict is never edited: what happens to it later is
-- an appended resolution event (the products moved on, a person explained or
-- dismissed it).
CREATE TABLE IF NOT EXISTS product_consistency_conflicts (
    conflict_id TEXT PRIMARY KEY,
    conflict_kind TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('high','medium','low')),
    rule_ref TEXT NOT NULL,
    version_refs_json TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_consistency_conflicts_company
ON product_consistency_conflicts(company_ref, conflict_kind, created_at);
CREATE TRIGGER IF NOT EXISTS product_consistency_conflicts_authorized_insert
BEFORE INSERT ON product_consistency_conflicts WHEN dalton_product_consistency_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'conflict insert requires ProductConsistencyAuthority'); END;
CREATE TRIGGER IF NOT EXISTS product_consistency_conflicts_no_update
BEFORE UPDATE ON product_consistency_conflicts BEGIN
    SELECT RAISE(ABORT, 'conflicts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS product_consistency_conflicts_no_delete
BEFORE DELETE ON product_consistency_conflicts BEGIN
    SELECT RAISE(ABORT, 'conflicts are append-only'); END;

-- What happened to a conflict: ``superseded`` (a version it names is no
-- longer current -- appended by the scan), ``explained`` (a person or lane
-- recorded the evidence that resolves it), ``dismissed`` (a person decided it
-- is not a conflict).  Append-only; the latest event is the state.
CREATE TABLE IF NOT EXISTS product_consistency_resolutions (
    resolution_id TEXT PRIMARY KEY,
    conflict_id TEXT NOT NULL REFERENCES product_consistency_conflicts(conflict_id),
    resolution TEXT NOT NULL CHECK(resolution IN ('superseded','explained','dismissed','reopened')),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_consistency_resolutions_conflict
ON product_consistency_resolutions(conflict_id, created_at);
CREATE TRIGGER IF NOT EXISTS product_consistency_resolutions_authorized_insert
BEFORE INSERT ON product_consistency_resolutions WHEN dalton_product_consistency_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'resolution insert requires ProductConsistencyAuthority'); END;
CREATE TRIGGER IF NOT EXISTS product_consistency_resolutions_no_update
BEFORE UPDATE ON product_consistency_resolutions BEGIN
    SELECT RAISE(ABORT, 'conflict resolutions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS product_consistency_resolutions_no_delete
BEFORE DELETE ON product_consistency_resolutions BEGIN
    SELECT RAISE(ABORT, 'conflict resolutions are append-only'); END;

-- Migration-safe company identity: a non-canonical spelling already in
-- append-only history (legacy DXC ``company:sec-cik:001688568``) maps to the
-- ten-digit canonical ref.  History is never rewritten; readers resolve
-- through this table (product_consistency.company_ref_aliases).
CREATE TABLE IF NOT EXISTS company_ref_aliases (
    alias_ref TEXT PRIMARY KEY,
    canonical_ref TEXT NOT NULL,
    reason TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_company_ref_aliases_canonical
ON company_ref_aliases(canonical_ref);
CREATE TRIGGER IF NOT EXISTS company_ref_aliases_authorized_insert
BEFORE INSERT ON company_ref_aliases WHEN dalton_product_consistency_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'alias insert requires ProductConsistencyAuthority'); END;
CREATE TRIGGER IF NOT EXISTS company_ref_aliases_no_update
BEFORE UPDATE ON company_ref_aliases BEGIN
    SELECT RAISE(ABORT, 'company ref aliases are append-only'); END;
CREATE TRIGGER IF NOT EXISTS company_ref_aliases_no_delete
BEFORE DELETE ON company_ref_aliases BEGIN
    SELECT RAISE(ABORT, 'company ref aliases are append-only'); END;
