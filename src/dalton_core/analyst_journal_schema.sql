-- Q1: the analyst journal the Playbook asks a Level 1 analyst to keep.
--
-- `analyst_levels[level:basic-1-desktop-research]` says it plainly: "建立
-- analyst journal（内化反馈、避免重复错误）".  Dalton has been at that level
-- since Phase 9 and has had nowhere to put feedback: the PM reads a document
-- and the reading is gone.
--
-- An entry is an event, not a version chain.  A person who changes their mind
-- writes another entry; nothing is edited and nothing is superseded in place,
-- so `entry_number` is the chain -- monotonic per target, and the reason the
-- table can be append-only without pretending an opinion has versions.
--
-- The target is bound by ref *and* content hash.  "This is good" said about a
-- document that has since been rewritten is a statement about the old
-- document, and a journal that cannot tell the difference is a journal that
-- launders stale approval into fresh confidence.

CREATE TABLE IF NOT EXISTS analyst_journal_entries (
    entry_id TEXT PRIMARY KEY,
    entry_number INTEGER NOT NULL CHECK(entry_number >= 1),
    target_ref TEXT NOT NULL,
    target_hash TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK(target_kind IN (
        'initial_screen','ask_answer','company_dossier','quality_score','claim','weekly_brief'
    )),
    company_ref TEXT,
    verdict TEXT NOT NULL CHECK(verdict IN (
        'read','useful','needs_more_evidence','disagree','revise'
    )),
    note TEXT,
    score_override_json TEXT,
    idempotency_key TEXT,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    -- Automation grading itself is the quality score and has its own record.
    actor_ref TEXT NOT NULL CHECK(actor_ref LIKE 'human:%'),
    created_at TEXT NOT NULL,
    UNIQUE(target_ref, entry_number),
    UNIQUE(target_ref, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_analyst_journal_by_company
ON analyst_journal_entries(company_ref, created_at);

CREATE INDEX IF NOT EXISTS idx_analyst_journal_by_verdict
ON analyst_journal_entries(verdict, created_at);

CREATE TRIGGER IF NOT EXISTS analyst_journal_authorized_insert
BEFORE INSERT ON analyst_journal_entries WHEN dalton_analyst_journal_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'analyst journal insert requires AnalystJournalAuthority'); END;
CREATE TRIGGER IF NOT EXISTS analyst_journal_no_update
BEFORE UPDATE ON analyst_journal_entries BEGIN
    SELECT RAISE(ABORT, 'analyst journal entries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS analyst_journal_no_delete
BEFORE DELETE ON analyst_journal_entries BEGIN
    SELECT RAISE(ABORT, 'analyst journal entries are append-only'); END;
