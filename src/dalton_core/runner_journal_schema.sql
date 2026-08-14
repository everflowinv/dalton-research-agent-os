-- Durable physical runner journal.  These rows record execution barriers and
-- recovery facts; they are operational authority, not Research Ledger facts.

CREATE TABLE IF NOT EXISTS runner_request_journal (
    runner_request_ref TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    connector_invocation_ref TEXT NOT NULL,
    request_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runner_attempt_journal_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    runner_request_ref TEXT NOT NULL REFERENCES runner_request_journal(runner_request_ref),
    state TEXT NOT NULL CHECK (state IN (
        'admitted', 'reserved', 'transport_started', 'observed', 'responded',
        'released_recovered', 'indeterminate_recovered'
    )),
    reservation_ref TEXT,
    event_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS runner_journal_request_seq
ON runner_attempt_journal_events(runner_request_ref, event_seq);

-- A reservation can authorize at most one durable transport start.
CREATE UNIQUE INDEX IF NOT EXISTS runner_journal_one_transport_start
ON runner_attempt_journal_events(reservation_ref)
WHERE state='transport_started';

CREATE TRIGGER IF NOT EXISTS runner_request_journal_authorized_insert
BEFORE INSERT ON runner_request_journal WHEN dalton_authorized()=0 BEGIN
    SELECT RAISE(ABORT, 'runner request journal insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS runner_attempt_journal_authorized_insert
BEFORE INSERT ON runner_attempt_journal_events WHEN dalton_authorized()=0 BEGIN
    SELECT RAISE(ABORT, 'runner event journal insert requires DaltonStore');
END;

CREATE TRIGGER IF NOT EXISTS runner_request_journal_no_update
BEFORE UPDATE ON runner_request_journal BEGIN
    SELECT RAISE(ABORT, 'runner request journal is immutable');
END;
CREATE TRIGGER IF NOT EXISTS runner_request_journal_no_delete
BEFORE DELETE ON runner_request_journal BEGIN
    SELECT RAISE(ABORT, 'runner request journal is immutable');
END;
CREATE TRIGGER IF NOT EXISTS runner_attempt_journal_no_update
BEFORE UPDATE ON runner_attempt_journal_events BEGIN
    SELECT RAISE(ABORT, 'runner event journal is immutable');
END;
CREATE TRIGGER IF NOT EXISTS runner_attempt_journal_no_delete
BEFORE DELETE ON runner_attempt_journal_events BEGIN
    SELECT RAISE(ABORT, 'runner event journal is immutable');
END;
