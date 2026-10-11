-- Batch E: the material acquisition service's own tables, in the same file as
-- the MaterialRequest ledger (``material-requests.sqlite``). Research and
-- accounting facts remain append-only; the scheduling cursor is mutable metadata.  Nothing here stores a document: found material is acquired through the
-- existing acquisition manifests and indexed by the evidence index; these
-- tables only say what was spent, what was asked of a connector on a
-- request's behalf, and which arrived document answered which request.

-- One row per connector spend on a request's behalf (the day ledger of the
-- ``material_acquisition`` pool).  ``calls`` is what the source's own call
-- window counts; ``cost_micros`` is dollars where the source is metered.
CREATE TABLE IF NOT EXISTS material_acquisition_spend (
    spend_id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    request_id TEXT NOT NULL,
    source TEXT NOT NULL,
    calls INTEGER NOT NULL CHECK (calls >= 0),
    cost_micros INTEGER NOT NULL CHECK (cost_micros >= 0),
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS material_acquisition_spend_day ON material_acquisition_spend(day, request_id);

-- A targeted query: "run this source's search for this company now, on behalf
-- of this request".  The owning coordinator (AlphaEngine, web search, the SEC
-- filings index, Guidepoint) drains it on its own tick through its own
-- governed launcher, mission grant, health hold and call window, and appends
-- what happened to ``material_targeted_query_events``.
CREATE TABLE IF NOT EXISTS material_targeted_queries (
    query_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    spec_ref TEXT,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS material_targeted_queries_source ON material_targeted_queries(source_ref, created_at);
CREATE INDEX IF NOT EXISTS material_targeted_queries_request ON material_targeted_queries(request_id, source);

CREATE TABLE IF NOT EXISTS material_targeted_query_events (
    event_id INTEGER PRIMARY KEY,
    query_id TEXT NOT NULL REFERENCES material_targeted_queries(query_id),
    status TEXT NOT NULL CHECK (status IN ('launched', 'refused', 'deferred', 'failed')),
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS material_targeted_query_events_query ON material_targeted_query_events(query_id, event_id);

-- ``material_arrived``: the request <-> document link.  The evidence index's
-- ``material_arrivals`` ledger records that a document arrived for a company;
-- this records which request it answered, so a product waiting on that
-- request (a redraft threshold, a ``requires_material`` rewrite flag) can
-- resolve.  One row per (request, document).
CREATE TABLE IF NOT EXISTS material_request_arrivals (
    link_id INTEGER PRIMARY KEY,
    request_id TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    arrival_id INTEGER,
    kind TEXT,
    source TEXT,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (request_id, document_ref)
);
CREATE INDEX IF NOT EXISTS material_request_arrivals_company ON material_request_arrivals(company_ref, link_id);
CREATE INDEX IF NOT EXISTS material_request_arrivals_document ON material_request_arrivals(document_ref);

-- What a conversion sweep has already turned into requests (a rewrite flag, a
-- Dossier repair target, a refused planner inquiry), so an unchanged input is
-- not re-read into the ledger every tick.
CREATE TABLE IF NOT EXISTS material_acquisition_conversions (
    input_ref TEXT PRIMARY KEY,
    request_id TEXT,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS material_acquisition_spend_no_update BEFORE UPDATE ON material_acquisition_spend BEGIN
    SELECT RAISE(ABORT, 'material acquisition spend is append-only');
END;
CREATE TRIGGER IF NOT EXISTS material_acquisition_spend_no_delete BEFORE DELETE ON material_acquisition_spend BEGIN
    SELECT RAISE(ABORT, 'material acquisition spend is append-only');
END;
CREATE TRIGGER IF NOT EXISTS material_targeted_queries_no_update BEFORE UPDATE ON material_targeted_queries BEGIN
    SELECT RAISE(ABORT, 'targeted queries are append-only');
END;
CREATE TRIGGER IF NOT EXISTS material_targeted_queries_no_delete BEFORE DELETE ON material_targeted_queries BEGIN
    SELECT RAISE(ABORT, 'targeted queries are append-only');
END;
CREATE TRIGGER IF NOT EXISTS material_targeted_query_events_no_update BEFORE UPDATE ON material_targeted_query_events BEGIN
    SELECT RAISE(ABORT, 'targeted query events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS material_targeted_query_events_no_delete BEFORE DELETE ON material_targeted_query_events BEGIN
    SELECT RAISE(ABORT, 'targeted query events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS material_request_arrivals_no_update BEFORE UPDATE ON material_request_arrivals BEGIN
    SELECT RAISE(ABORT, 'material request arrivals are append-only');
END;
CREATE TRIGGER IF NOT EXISTS material_request_arrivals_no_delete BEFORE DELETE ON material_request_arrivals BEGIN
    SELECT RAISE(ABORT, 'material request arrivals are append-only');
END;
CREATE TRIGGER IF NOT EXISTS material_acquisition_conversions_no_update BEFORE UPDATE ON material_acquisition_conversions BEGIN
    SELECT RAISE(ABORT, 'material acquisition conversions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS material_acquisition_conversions_no_delete BEFORE DELETE ON material_acquisition_conversions BEGIN
    SELECT RAISE(ABORT, 'material acquisition conversions are append-only');
END;

-- Mutable scheduling metadata, not an acquisition attempt or budget entry.
-- Resume a bounded priority sweep without allowing waiting requests to occupy
-- its first slots forever. Old request, event and spend rows remain immutable.
CREATE TABLE IF NOT EXISTS material_acquisition_service_cursor (
    cursor_id TEXT PRIMARY KEY CHECK (cursor_id = 'priority-round-robin'),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL
);
