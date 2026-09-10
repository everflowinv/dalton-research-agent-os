-- P15d: the call, and the person who decided it.
--
-- Everything else this system writes is a description of the world. A
-- conviction call is the one object that says *do something about it*, and the
-- owner's rule from 2026-09-09 is the whole reason it has the shape it has:
-- being bullish while the market is bullish is worth nothing, so a call that
-- cannot say where the market is wrong and what would drag it towards us is
-- not a call, it is a paraphrase of the price.
--
-- Two tables, and the split between them is the point. Automation may write a
-- row in the first one and can never write a row in the second: a proposal
-- opens the `conviction_call` human checkpoint and nothing more. The decision
-- is a separate append-only record, made by a `human:` principal, and it is
-- bound to the exact bytes it was made about -- `proposal_hash` must equal the
-- proposal's `content_hash`, so "I accepted this call" can never be inherited
-- by a call that was rewritten afterwards. Nothing here is ever rewritten, so
-- that cannot happen either; the binding is belt and braces because this is
-- the one place a person's judgement enters the record.
--
-- `accept` and `reject` settle a proposal; `defer` does not. A deferred call
-- leaves the approvals queue and can be decided again later, and the chain of
-- decisions is readable in order -- "deferred on the 9th, accepted on the
-- 16th after the print" is a research fact, and a table that overwrote the
-- first decision would lose it.

CREATE TABLE IF NOT EXISTS conviction_call_proposals (
    proposal_id TEXT PRIMARY KEY,
    call_ref TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    -- ISO year-week. The lane's "at most one call per company per week" rule
    -- is enforced in the authority against this column rather than in the
    -- coordinator's memory, because a coordinator's memory does not survive a
    -- restart and a second call in the same week is exactly what a restart
    -- would produce.
    week_key TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('long', 'short', 'avoid')),
    -- The Playbook's frozen five-word Active Coverage vocabulary.
    decision_word TEXT NOT NULL CHECK(decision_word IN (
        'NO_CHANGE', 'THESIS_STRENGTHENED', 'THESIS_WEAKENED',
        'THESIS_BROKEN', 'NEW_THESIS')),
    confidence TEXT NOT NULL CHECK(confidence IN ('low', 'medium', 'high')),
    time_horizon TEXT NOT NULL,
    -- Whether the Playbook's own risk/reward standard is met, as the
    -- deterministic check found it: met / not_met / unavailable. A call that
    -- does not meet the standard is still proposable -- the person decides --
    -- but it may never look like one that does.
    risk_reward_status TEXT NOT NULL CHECK(risk_reward_status IN (
        'met', 'not_met', 'unavailable', 'not_applicable')),
    -- The exact evidence this call was drawn from. Idempotency is per
    -- (company, fingerprint): a tick that fires again over unchanged evidence
    -- is the same call, not a second one.
    evidence_fingerprint TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(company_ref, evidence_fingerprint)
);

CREATE INDEX IF NOT EXISTS conviction_call_proposals_by_week
ON conviction_call_proposals(company_ref, week_key);
CREATE INDEX IF NOT EXISTS conviction_call_proposals_by_created
ON conviction_call_proposals(created_at);

CREATE TABLE IF NOT EXISTS conviction_call_decisions (
    decision_id TEXT PRIMARY KEY,
    proposal_ref TEXT NOT NULL REFERENCES conviction_call_proposals(proposal_id),
    -- The bytes decided about. Equal to the proposal's content_hash or the
    -- write is refused.
    proposal_hash TEXT NOT NULL,
    decision_number INTEGER NOT NULL CHECK(decision_number >= 1),
    decision TEXT NOT NULL CHECK(decision IN ('accept', 'reject', 'defer')),
    -- Every decision says why, including acceptance. "Why did we take this
    -- one and not the other four" is the question the weekly review asks.
    reason TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    -- Automation proposes; a person decides. Both gates say so: this one, and
    -- the writer operation that is human-governance only.
    actor_ref TEXT NOT NULL CHECK(actor_ref LIKE 'human:%'),
    created_at TEXT NOT NULL,
    UNIQUE(proposal_ref, decision_number)
);

CREATE INDEX IF NOT EXISTS conviction_call_decisions_by_proposal
ON conviction_call_decisions(proposal_ref, decision_number);

CREATE TRIGGER IF NOT EXISTS conviction_call_proposal_insert_guard
BEFORE INSERT ON conviction_call_proposals WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'conviction call proposal insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS conviction_call_proposal_no_update
BEFORE UPDATE ON conviction_call_proposals BEGIN
    SELECT RAISE(ABORT, 'conviction call proposals are immutable');
END;
CREATE TRIGGER IF NOT EXISTS conviction_call_proposal_no_delete
BEFORE DELETE ON conviction_call_proposals BEGIN
    SELECT RAISE(ABORT, 'conviction call proposals are immutable');
END;

CREATE TRIGGER IF NOT EXISTS conviction_call_decision_insert_guard
BEFORE INSERT ON conviction_call_decisions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'conviction call decision insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS conviction_call_decision_no_update
BEFORE UPDATE ON conviction_call_decisions BEGIN
    SELECT RAISE(ABORT, 'conviction call decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS conviction_call_decision_no_delete
BEFORE DELETE ON conviction_call_decisions BEGIN
    SELECT RAISE(ABORT, 'conviction call decisions are immutable');
END;
