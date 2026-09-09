CREATE TABLE IF NOT EXISTS research_events (
    event_id TEXT PRIMARY KEY,
    company_ref TEXT NOT NULL,
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    evidence_tier TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    mission_version_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- The whole of the idempotency rule, enforced by the store rather than by
    -- whoever remembers to check: one company, one kind, one payload is one
    -- event however many times an emitter re-reads the row behind it.
    UNIQUE(company_ref, kind, payload_hash)
);

CREATE INDEX IF NOT EXISTS research_events_by_company
ON research_events(company_ref, occurred_at DESC, event_id);
CREATE INDEX IF NOT EXISTS research_events_by_kind
ON research_events(kind, occurred_at DESC, event_id);

CREATE TRIGGER IF NOT EXISTS research_events_insert_guard
BEFORE INSERT ON research_events WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research event insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS research_events_no_update
BEFORE UPDATE ON research_events BEGIN
    SELECT RAISE(ABORT, 'research events are immutable');
END;
CREATE TRIGGER IF NOT EXISTS research_events_no_delete
BEFORE DELETE ON research_events BEGIN
    SELECT RAISE(ABORT, 'research events are immutable');
END;
