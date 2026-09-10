-- C2: an append-only record of what each controller tick did.
--
-- Before this table the tick summary was a return value: ``service`` wrote it
-- into ``run/heartbeat.json`` and the next tick overwrote it.  So "what share
-- of ticks were idle", "which lane has been stuck since Thursday" and "where
-- did the day's money go" were not hard questions, they were unanswerable
-- ones -- Q2's reflection reports them as ``available: false`` for exactly
-- this reason.  One row per tick, one row per lane per tick, written in one
-- transaction at the end of the tick.
--
-- Append-only: nothing here is ever updated or deleted.  Retention is a
-- reader-side window (90 days), so an archiver can be added later without
-- having to trust that no one deleted the evidence in the meantime.

CREATE TABLE IF NOT EXISTS tick_ledger_ticks (
    tick_id             TEXT PRIMARY KEY,
    day                 TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    ended_at            TEXT NOT NULL,
    status              TEXT NOT NULL,
    active_loop_count   INTEGER NOT NULL,
    probes_executed     INTEGER NOT NULL,
    executed_count      INTEGER NOT NULL,
    skipped_count       INTEGER NOT NULL,
    lane_count          INTEGER NOT NULL,
    idle                INTEGER NOT NULL,
    pool_spend_json     TEXT NOT NULL,
    pool_cumulative_json TEXT NOT NULL,
    -- C2b: what the mission day ledger did with this tick's Tier-1 planner
    -- calls -- how many loops its pools held, and whether those calls reached
    -- the ledger at all.  Nullable: every row written before C2b has no
    -- answer, and a zero would be a claim rather than a gap.
    planner_budget_json TEXT,
    content_hash        TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tick_ledger_ticks_day
    ON tick_ledger_ticks(day, started_at);

CREATE TABLE IF NOT EXISTS tick_ledger_lanes (
    tick_id                 TEXT NOT NULL REFERENCES tick_ledger_ticks(tick_id),
    day                     TEXT NOT NULL,
    started_at              TEXT NOT NULL,
    driver_key              TEXT NOT NULL,
    lane_operation          TEXT NOT NULL,
    pool                    TEXT NOT NULL,
    status                  TEXT NOT NULL,
    status_word             TEXT NOT NULL,
    idle                    INTEGER NOT NULL,
    pool_exhausted          INTEGER NOT NULL,
    counts_json             TEXT NOT NULL,
    pool_spend_delta_micros INTEGER,
    created_at              TEXT NOT NULL,
    PRIMARY KEY (tick_id, driver_key)
);

CREATE INDEX IF NOT EXISTS idx_tick_ledger_lanes_day
    ON tick_ledger_lanes(day, driver_key);
