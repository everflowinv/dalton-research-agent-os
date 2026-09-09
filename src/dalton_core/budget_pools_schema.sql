-- C2 capacity pools: the additive part of the day-budget ledger.
--
-- The pool itself lives as a nullable column on the admission and settlement
-- rows (added by budget_pools.apply_pool_migration, because SQLite has no
-- "ADD COLUMN IF NOT EXISTS"), so nothing already written moves and no
-- content hash changes.  What needs a table of its own is the refusal: a
-- lane that ran out of its pool today is a fact about a day the cockpit has
-- to be able to list, and it must not be written into
-- thesis_impact_day_rejections, whose rows are permanent verdicts on an
-- admission identity ("a rejected admission cannot later be admitted").  A
-- pool refills at midnight; a rejected identity never does.

CREATE TABLE IF NOT EXISTS model_budget_pool_rejections (
    rejection_id       TEXT PRIMARY KEY,
    day                TEXT NOT NULL,
    mission_ref        TEXT NOT NULL,
    pool               TEXT NOT NULL,
    pool_lane          TEXT,
    work_order_ref     TEXT NOT NULL,
    attempt_number     INTEGER NOT NULL,
    phase              TEXT NOT NULL,
    reserved_micros    INTEGER NOT NULL,
    pool_spent_micros  INTEGER NOT NULL,
    pool_cap_micros    INTEGER NOT NULL,
    borrowable_micros  INTEGER NOT NULL,
    record_json        TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_model_budget_pool_rejection_day
    ON model_budget_pool_rejections(day, mission_ref, pool);
