-- P12d: the Deep Insight Gate's twelve answers, and the person's one decision.
--
-- Two tables and no pointer.  "The current draft" is the highest version
-- number of the chain, derived rather than stored, because a pointer is a
-- second answer to a question the chain already answers and the two can
-- disagree.
--
-- The decision is a separate table rather than a column on the draft for the
-- reason ADR-0008 gives: the draft is append-only, so a decision written into
-- it would have to arrive before the draft existed.  One decision per draft
-- version, bound to that version's content hash, so a decision can never be
-- read as being about a draft the owner did not see.

CREATE TABLE IF NOT EXISTS deep_insight_gate_versions (
    version_id TEXT PRIMARY KEY,
    gate_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES deep_insight_gate_versions(version_id),
    company_ref TEXT NOT NULL,
    change_reason TEXT NOT NULL,
    -- What the twelve answers say, without the version number or the clock:
    -- two drafts with the same body answer the gate the same way, whatever
    -- occasioned the second attempt.
    body_hash TEXT NOT NULL,
    -- The union of every ref the draft cites, canonicalised, so "does this
    -- draft rest on anything the current one did not" is a read.
    evidence_scope_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(gate_ref, version_number)
);

CREATE INDEX IF NOT EXISTS deep_insight_gate_versions_by_company
ON deep_insight_gate_versions(company_ref, version_number);

CREATE TRIGGER IF NOT EXISTS deep_insight_gate_insert_guard
BEFORE INSERT ON deep_insight_gate_versions
WHEN dalton_deep_insight_gate_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'deep insight gate insert requires DeepInsightGateAuthority');
END;

CREATE TRIGGER IF NOT EXISTS deep_insight_gate_no_update
BEFORE UPDATE ON deep_insight_gate_versions BEGIN
    SELECT RAISE(ABORT, 'deep insight gate drafts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS deep_insight_gate_no_delete
BEFORE DELETE ON deep_insight_gate_versions BEGIN
    SELECT RAISE(ABORT, 'deep insight gate drafts are immutable');
END;

CREATE TABLE IF NOT EXISTS deep_insight_gate_decisions (
    decision_id TEXT PRIMARY KEY,
    -- One decision per draft version.  A second decision on the same draft is
    -- a person changing their mind, and the way to do that is a new draft.
    gate_version_ref TEXT NOT NULL UNIQUE
        REFERENCES deep_insight_gate_versions(version_id),
    gate_version_hash TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN (
        'approve','return_for_more_work','reject'
    )),
    reason TEXT NOT NULL,
    -- The mission stage record the approval produced, when it produced one.
    -- The ladder is the CoverageMission's; this column only names the row it
    -- wrote so the two can be read together.
    stage_record_ref TEXT,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS deep_insight_gate_decisions_by_company
ON deep_insight_gate_decisions(company_ref, created_at);

CREATE TRIGGER IF NOT EXISTS deep_insight_gate_decision_insert_guard
BEFORE INSERT ON deep_insight_gate_decisions
WHEN dalton_deep_insight_gate_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'deep insight gate decision requires DeepInsightGateAuthority');
END;

CREATE TRIGGER IF NOT EXISTS deep_insight_gate_decision_no_update
BEFORE UPDATE ON deep_insight_gate_decisions BEGIN
    SELECT RAISE(ABORT, 'deep insight gate decisions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS deep_insight_gate_decision_no_delete
BEFORE DELETE ON deep_insight_gate_decisions BEGIN
    SELECT RAISE(ABORT, 'deep insight gate decisions are append-only');
END;
