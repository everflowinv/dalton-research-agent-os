-- 2026-10-09 (P0, research artifact quality review): a product version that
-- cites a Claim retired as another issuer's needs a rewrite.  Append-only:
-- the flag names the exact product version, the affected sections and the
-- retired Claims; it is open while that version is still current, and the
-- product lane's next (batched) version answers it.  Nothing is redrafted here.
CREATE TABLE IF NOT EXISTS product_rewrite_flags (
    flag_id TEXT PRIMARY KEY,
    product_kind TEXT NOT NULL,
    product_ref TEXT NOT NULL,
    product_version_ref TEXT NOT NULL,
    subject_ref TEXT,
    rule_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_rewrite_flags_version
ON product_rewrite_flags(product_version_ref);
CREATE INDEX IF NOT EXISTS idx_product_rewrite_flags_subject
ON product_rewrite_flags(subject_ref, created_at);
CREATE TRIGGER IF NOT EXISTS product_rewrite_flags_authorized_insert
BEFORE INSERT ON product_rewrite_flags WHEN dalton_product_rewrite_flag_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'product rewrite flag insert requires ProductRewriteFlagAuthority'); END;
CREATE TRIGGER IF NOT EXISTS product_rewrite_flags_no_update
BEFORE UPDATE ON product_rewrite_flags BEGIN
    SELECT RAISE(ABORT, 'product rewrite flags are append-only'); END;
CREATE TRIGGER IF NOT EXISTS product_rewrite_flags_no_delete
BEFORE DELETE ON product_rewrite_flags BEGIN
    SELECT RAISE(ABORT, 'product rewrite flags are append-only'); END;

-- Which (retirements, product versions) states a flag scan has already read:
-- a cache, so an unchanged state is not re-read every tick.
CREATE TABLE IF NOT EXISTS product_rewrite_scans (
    probe TEXT PRIMARY KEY,
    scanned_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS product_rewrite_scans_authorized_insert
BEFORE INSERT ON product_rewrite_scans WHEN dalton_product_rewrite_flag_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'product rewrite scan insert requires ProductRewriteFlagAuthority'); END;
