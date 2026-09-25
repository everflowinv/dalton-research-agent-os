-- 2026-09-25: a retired Claim that is an industry-level finding is recorded
-- against the mission's industry, by a further append-only record.
--
-- The span-level subject-absent detector retires a Claim whose statement and
-- cited span never name the company it is filed under.  That stays right at
-- company level; but when the Claim is about the industry (a CIO survey's
-- spending read, hyperscaler capex as a group) the finding is knowledge the
-- industry framework, the evidence refresh and the debate map should keep.
-- ClaimVersion is frozen and its subject_ref is part of its hash, so the
-- Claim is never re-filed: this row names the exact claim version and the
-- exact retirement decision (and their hashes), and says which industry the
-- finding belongs to, who said so and under which rule.  The retirement row
-- is untouched and company-level read paths keep skipping the Claim; only the
-- industry-level reads (``claim_industry_reattribution.industry_reattributions``)
-- pick it up.  One row per claim version, for ever.
CREATE TABLE IF NOT EXISTS claim_industry_reattributions (
    reattribution_id TEXT PRIMARY KEY,
    claim_version_ref TEXT NOT NULL UNIQUE REFERENCES claim_versions(claim_version_id),
    claim_version_hash TEXT NOT NULL,
    decision_ref TEXT NOT NULL REFERENCES claim_retirement_decisions(decision_id),
    decision_hash TEXT NOT NULL,
    from_subject_ref TEXT NOT NULL,
    industry_ref TEXT NOT NULL CHECK(substr(industry_ref, 1, 9) = 'industry:'),
    mission_version_ref TEXT NOT NULL,
    reason_code TEXT NOT NULL CHECK(reason_code IN (
        'industry_level_under_rule',
        'human_judgment'
    )),
    rule_ref TEXT,
    actor_ref TEXT NOT NULL,
    rationale TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claim_industry_reattributions_industry
ON claim_industry_reattributions(industry_ref, created_at);

CREATE TRIGGER IF NOT EXISTS claim_industry_reattributions_authorized_insert
BEFORE INSERT ON claim_industry_reattributions WHEN dalton_claim_reattribution_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'claim industry reattribution insert requires ClaimIndustryReattributionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS claim_industry_reattributions_no_update
BEFORE UPDATE ON claim_industry_reattributions BEGIN
    SELECT RAISE(ABORT, 'claim industry reattributions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS claim_industry_reattributions_no_delete
BEFORE DELETE ON claim_industry_reattributions BEGIN
    SELECT RAISE(ABORT, 'claim industry reattributions are append-only'); END;

-- Which retired Claims the backfill has already judged, under which rule and
-- which alias table.  A cache, like ``claim_review_rereviews``: the authority
-- is the table above; this only keeps a Claim the rule refused from being
-- re-read every tick.  A new rule ref or a changed alias table
-- (``inputs_hash``) judges it once more.
CREATE TABLE IF NOT EXISTS claim_industry_reattribution_reviews (
    claim_version_ref TEXT PRIMARY KEY,
    rule_ref TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN (
        'reattributed', 'not_industry_level', 'no_industry', 'unreadable'
    )),
    refusal TEXT,
    attempts INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    reviewed_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS claim_industry_reattribution_reviews_authorized_insert
BEFORE INSERT ON claim_industry_reattribution_reviews
WHEN dalton_claim_reattribution_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'claim reattribution review insert requires authority');
END;
CREATE TRIGGER IF NOT EXISTS claim_industry_reattribution_reviews_authorized_update
BEFORE UPDATE ON claim_industry_reattribution_reviews
WHEN dalton_claim_reattribution_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'claim reattribution review update requires authority');
END;
CREATE TRIGGER IF NOT EXISTS claim_industry_reattribution_reviews_no_delete
BEFORE DELETE ON claim_industry_reattribution_reviews BEGIN
    SELECT RAISE(ABORT, 'claim reattribution reviews cannot be deleted');
END;

-- 2026-09-25b: a reattribution withdrawn, by a further append-only record.
--
-- Rule v2 of claim_industry_rule no longer counts "capex" on its own as the
-- hyperscaler industry's word (GS on capital markets "supported by AI capex
-- spend" was kept as hyperscaler evidence).  A reattribution is one row per
-- claim version for ever, so the ones v1 kept and v2 refuses are withdrawn
-- the way a reinstatement is: this row names the exact reattribution and its
-- hash; ``claim_industry_reattribution.industry_reattributions`` counts only
-- reattributions that stand, so every industry-level read drops the Claim at
-- once.  The retirement is untouched; the Claim is simply retired again, and
-- nobody's.  One row per reattribution; update/delete refused.
CREATE TABLE IF NOT EXISTS claim_industry_reattribution_withdrawals (
    withdrawal_id TEXT PRIMARY KEY,
    claim_version_ref TEXT NOT NULL,
    reattribution_ref TEXT NOT NULL UNIQUE REFERENCES claim_industry_reattributions(reattribution_id),
    reattribution_hash TEXT NOT NULL,
    reason_code TEXT NOT NULL CHECK(reason_code IN (
        'not_industry_level_under_current_rule',
        'human_judgment'
    )),
    rule_ref TEXT,
    actor_ref TEXT NOT NULL,
    rationale TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS claim_industry_reattribution_withdrawals_authorized_insert
BEFORE INSERT ON claim_industry_reattribution_withdrawals
WHEN dalton_claim_reattribution_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'claim reattribution withdrawal insert requires ClaimIndustryReattributionAuthority'); END;
CREATE TRIGGER IF NOT EXISTS claim_industry_reattribution_withdrawals_no_update
BEFORE UPDATE ON claim_industry_reattribution_withdrawals BEGIN
    SELECT RAISE(ABORT, 'claim reattribution withdrawals are append-only'); END;
CREATE TRIGGER IF NOT EXISTS claim_industry_reattribution_withdrawals_no_delete
BEFORE DELETE ON claim_industry_reattribution_withdrawals BEGIN
    SELECT RAISE(ABORT, 'claim reattribution withdrawals are append-only'); END;
