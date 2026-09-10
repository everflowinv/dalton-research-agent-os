-- P17d: every park, resume and terminal verdict a lane reached, kept.
--
-- The predecessor project's Task 62 failed three times on
-- ``AlphaEngine Desktop status=no_module_page`` and was permanently suspended.
-- The only place that was visible was one cell of one task CSV: the cron
-- history said every run was ``ok``, and the health page said the system was
-- fine.  So the outage was real, recorded, and unreadable.
--
-- One row per event, appended, never updated and never deleted.  "What is
-- parked right now" is a **fold over these rows**, not a column somebody keeps
-- in step: a parked item is one whose latest event is ``parked`` with no later
-- ``resumed`` / ``dependency_ok`` for its dependency and no ``terminal``.  That
-- is why resume can be honest -- the same replay runs after a restart as
-- before it, and nothing has to be trusted not to have edited the state.
--
-- Retention is a reader-side window, like the tick ledger's: the rows stay and
-- an archiver added later decides what to do with the older ones.

CREATE TABLE IF NOT EXISTS lane_failure_events (
    event_id        TEXT PRIMARY KEY,
    recorded_at     TEXT NOT NULL,
    day             TEXT NOT NULL,
    lane            TEXT NOT NULL,
    item_key        TEXT NOT NULL,
    -- parked | parked_again | resumed | terminal | held | dependency_ok | not_permitted | permission_ok
    event           TEXT NOT NULL,
    -- dependency_unavailable | content_refused | not_permitted | transient
    failure_class   TEXT NOT NULL,
    -- The name the parked item waits on; NULL for a class that waits on
    -- nothing.  A later ``dependency_ok`` for this name is what resumes it.
    dependency      TEXT,
    -- The lane's own words, verbatim and truncated, never a class name
    -- standing in for them.
    reason          TEXT NOT NULL,
    -- Which rule in ``lane_failure_class.RULES`` decided this, so a wrong
    -- verdict can be traced to the line that made it.  ``unmapped`` is a real
    -- and expected value: it means the table has not seen this sentence.
    rule            TEXT NOT NULL,
    status          TEXT,
    content_hash    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lane_failure_events_replay
    ON lane_failure_events(recorded_at, event_id);

CREATE INDEX IF NOT EXISTS idx_lane_failure_events_dependency
    ON lane_failure_events(dependency, recorded_at);

CREATE INDEX IF NOT EXISTS idx_lane_failure_events_item
    ON lane_failure_events(lane, item_key, recorded_at);

-- Append-only, enforced rather than promised.
CREATE TRIGGER IF NOT EXISTS lane_failure_events_no_update
BEFORE UPDATE ON lane_failure_events
BEGIN
    SELECT RAISE(ABORT, 'lane_failure_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS lane_failure_events_no_delete
BEFORE DELETE ON lane_failure_events
BEGIN
    SELECT RAISE(ABORT, 'lane_failure_events is append-only');
END;
