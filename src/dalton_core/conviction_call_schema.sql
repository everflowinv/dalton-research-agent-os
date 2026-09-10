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
--
-- ADR-0008 applies here like it does to every other output authority: there is
-- no terminal call. A company's calls are a version chain -- `call_ref` names
-- it, `version_number` orders it, `prior_version_id` links it -- and every
-- version names the `change_reason` that occasioned it and the exact refs it
-- learned from. What "superseded" means for a call is narrower than for a
-- forecast cell, and it is deliberately a *human* fact: accepting a new call
-- for a company supersedes the one the owner accepted before it. Nothing is
-- rewritten to record that -- the accepting decision carries `supersedes_ref`,
-- so the mark and the act that caused it are one row, and the earlier
-- proposal is found superseded by reading forward rather than by having been
-- edited.

CREATE TABLE IF NOT EXISTS conviction_call_proposals (
    proposal_id TEXT PRIMARY KEY,
    -- One chain per company, named after it.
    call_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES conviction_call_proposals(proposal_id),
    -- ADR-0008's closed vocabulary, shared with the forecast and dossier
    -- authorities rather than copied: two closed lists claiming to be the same
    -- list is how a contract stops being one.
    change_reason TEXT NOT NULL CHECK(change_reason IN (
        'filing_actual', 'driver_event', 'assumption_review',
        'evidence_thicker', 'human_revision')),
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
    UNIQUE(company_ref, evidence_fingerprint),
    UNIQUE(call_ref, version_number),
    -- The weekly cap, in the schema as well as in the authority. The authority
    -- refuses a second call for the week with a reason a lane can report; this
    -- is the constraint that makes it true even if two processes raced, which
    -- is exactly the case the authority's read-then-write cannot cover.
    UNIQUE(company_ref, week_key)
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
    -- The call this acceptance replaced, when there was one. Set by the
    -- authority, never by the caller: which call was standing is a fact about
    -- the chain, not something a client gets to assert.
    supersedes_ref TEXT REFERENCES conviction_call_proposals(proposal_id),
    -- A retry of the same decision is the same decision. Without this a
    -- dropped response on `defer` would leave the owner unable to tell whether
    -- their deferral landed, and pressing again would append a second one.
    idempotency_key TEXT,
    -- Every decision says why, including acceptance. "Why did we take this
    -- one and not the other four" is the question the weekly review asks.
    reason TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    -- Automation proposes; a person decides. Both gates say so: this one, and
    -- the writer operation that is human-governance only.
    actor_ref TEXT NOT NULL CHECK(actor_ref LIKE 'human:%'),
    created_at TEXT NOT NULL,
    UNIQUE(proposal_ref, decision_number),
    UNIQUE(proposal_ref, idempotency_key)
);

CREATE INDEX IF NOT EXISTS conviction_call_decisions_by_proposal
ON conviction_call_decisions(proposal_ref, decision_number);
-- The read that answers "is this accepted call still the one standing".
CREATE INDEX IF NOT EXISTS conviction_call_decisions_by_supersedes
ON conviction_call_decisions(supersedes_ref);

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
