CREATE TABLE IF NOT EXISTS event_judgements (
    judgement_id TEXT PRIMARY KEY,
    -- One judgement per event, enforced by the store: the same event is never
    -- judged twice, whatever a lane believes about its own batch.
    event_ref TEXT NOT NULL UNIQUE,
    event_hash TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    decision TEXT NOT NULL,
    action TEXT NOT NULL,
    verdict TEXT NOT NULL,
    cost_micros INTEGER NOT NULL CHECK(cost_micros >= 0),
    mission_version_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS event_judgements_by_company
ON event_judgements(company_ref, created_at DESC, judgement_id);
CREATE INDEX IF NOT EXISTS event_judgements_by_day
ON event_judgements(substr(created_at, 1, 10));

CREATE TABLE IF NOT EXISTS thesis_revision_candidates (
    candidate_id TEXT PRIMARY KEY,
    judgement_ref TEXT NOT NULL REFERENCES event_judgements(judgement_id),
    thesis_version_ref TEXT NOT NULL,
    thesis_version_hash TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    decision TEXT NOT NULL,
    checkpoint_kind TEXT NOT NULL CHECK(checkpoint_kind = 'thesis_revision_candidate'),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(judgement_ref, thesis_version_ref)
);

CREATE TABLE IF NOT EXISTS forecast_revision_proposals (
    proposal_id TEXT PRIMARY KEY,
    judgement_ref TEXT NOT NULL REFERENCES event_judgements(judgement_id),
    company_ref TEXT NOT NULL,
    model_version_ref TEXT,
    driver_ref TEXT NOT NULL,
    period_end TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(judgement_ref, driver_ref, period_end)
);

CREATE TRIGGER IF NOT EXISTS event_judgements_insert_guard
BEFORE INSERT ON event_judgements WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'event judgement insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS event_judgements_no_update
BEFORE UPDATE ON event_judgements BEGIN
    SELECT RAISE(ABORT, 'event judgements are immutable');
END;
CREATE TRIGGER IF NOT EXISTS event_judgements_no_delete
BEFORE DELETE ON event_judgements BEGIN
    SELECT RAISE(ABORT, 'event judgements are immutable');
END;

CREATE TRIGGER IF NOT EXISTS thesis_revision_candidates_insert_guard
BEFORE INSERT ON thesis_revision_candidates WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'thesis revision candidate insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS thesis_revision_candidates_no_update
BEFORE UPDATE ON thesis_revision_candidates BEGIN
    SELECT RAISE(ABORT, 'thesis revision candidates are immutable');
END;
CREATE TRIGGER IF NOT EXISTS thesis_revision_candidates_no_delete
BEFORE DELETE ON thesis_revision_candidates BEGIN
    SELECT RAISE(ABORT, 'thesis revision candidates are immutable');
END;

CREATE TRIGGER IF NOT EXISTS forecast_revision_proposals_insert_guard
BEFORE INSERT ON forecast_revision_proposals WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'forecast revision proposal insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS forecast_revision_proposals_no_update
BEFORE UPDATE ON forecast_revision_proposals BEGIN
    SELECT RAISE(ABORT, 'forecast revision proposals are immutable');
END;
CREATE TRIGGER IF NOT EXISTS forecast_revision_proposals_no_delete
BEFORE DELETE ON forecast_revision_proposals BEGIN
    SELECT RAISE(ABORT, 'forecast revision proposals are immutable');
END;

-- P14a (owner's third instruction): agreeing with the market is worth nothing.
--
-- A reflection is written when we changed our mind, or when the price kept
-- running against what our thesis implies. It says what we expected, what
-- happened, why, what debate we may have missed, and what observable would
-- move the market toward our view. It changes nothing: it is attached to the
-- ThesisRevisionCandidate so the person deciding sees both.
CREATE TABLE IF NOT EXISTS thesis_reflections (
    reflection_id TEXT PRIMARY KEY,
    judgement_ref TEXT NOT NULL UNIQUE REFERENCES event_judgements(judgement_id),
    trigger_event_ref TEXT NOT NULL,
    trigger_event_hash TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    verdict TEXT NOT NULL,
    cost_micros INTEGER NOT NULL CHECK(cost_micros >= 0),
    mission_version_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS thesis_reflections_by_company
ON thesis_reflections(company_ref, created_at DESC, reflection_id);

CREATE TRIGGER IF NOT EXISTS thesis_reflections_insert_guard
BEFORE INSERT ON thesis_reflections WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'thesis reflection insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS thesis_reflections_no_update
BEFORE UPDATE ON thesis_reflections BEGIN
    SELECT RAISE(ABORT, 'thesis reflections are immutable');
END;
CREATE TRIGGER IF NOT EXISTS thesis_reflections_no_delete
BEFORE DELETE ON thesis_reflections BEGIN
    SELECT RAISE(ABORT, 'thesis reflections are immutable');
END;

-- The pool's one book.
--
-- The judgement rows cannot be it: a refused judgement writes no row and its
-- two calls were still paid for, and a reflection is a second pair of calls
-- against the same judgement. Summing the judgements therefore under-reports
-- the day by exactly the spend that a misbehaving model produces most of --
-- which is the direction that matters, because that is the day the cap is
-- there for. Every call outcome lands here, keyed by its WorkOrder so a
-- replayed call is not counted twice.
CREATE TABLE IF NOT EXISTS event_response_spend (
    spend_id TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    work_order_ref TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL,
    outcome TEXT NOT NULL,
    event_ref TEXT NOT NULL,
    cost_micros INTEGER NOT NULL CHECK(cost_micros >= 0),
    mission_version_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS event_response_spend_by_day
ON event_response_spend(day);

CREATE TRIGGER IF NOT EXISTS event_response_spend_insert_guard
BEFORE INSERT ON event_response_spend WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'event response spend insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS event_response_spend_no_update
BEFORE UPDATE ON event_response_spend BEGIN
    SELECT RAISE(ABORT, 'event response spend is immutable');
END;
CREATE TRIGGER IF NOT EXISTS event_response_spend_no_delete
BEFORE DELETE ON event_response_spend BEGIN
    SELECT RAISE(ABORT, 'event response spend is immutable');
END;

-- 2026-09-30: the cheap screen in front of the judge (``event_triage``).
--
-- One append-only row per event it read.  It is not a judgement and nothing
-- reads it as one: ``screened_out`` takes the event out of the unjudged
-- backlog without a decision word, ``escalated`` sends it to the judge exactly
-- as before.  ``route`` says why (the screen flagged it, the audit sample
-- picked it, or its kind is never screened); ``verdict`` is what the screen
-- said, kept on audited groups so the audit can compare it with the judgement
-- that followed.  A failed screen fails open and is not a decision: the run
-- summary lists it and no row is written ('triage_failed' is reserved).
CREATE TABLE IF NOT EXISTS event_triage_decisions (
    triage_id TEXT PRIMARY KEY,
    event_ref TEXT NOT NULL UNIQUE,
    primary_event_ref TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('escalated', 'screened_out')),
    route TEXT NOT NULL CHECK(route IN ('triage', 'audit', 'bypass', 'triage_failed')),
    verdict TEXT NOT NULL CHECK(verdict IN ('escalate', 'no_signal', 'none')),
    cost_micros INTEGER NOT NULL CHECK(cost_micros >= 0),
    policy_ref TEXT NOT NULL,
    mission_version_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS event_triage_decisions_by_outcome
ON event_triage_decisions(outcome, event_ref);
CREATE INDEX IF NOT EXISTS event_triage_decisions_by_day
ON event_triage_decisions(substr(created_at, 1, 10));

CREATE TRIGGER IF NOT EXISTS event_triage_decisions_insert_guard
BEFORE INSERT ON event_triage_decisions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'event triage insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS event_triage_decisions_no_update
BEFORE UPDATE ON event_triage_decisions BEGIN
    SELECT RAISE(ABORT, 'event triage decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS event_triage_decisions_no_delete
BEFORE DELETE ON event_triage_decisions BEGIN
    SELECT RAISE(ABORT, 'event triage decisions are immutable');
END;

-- ADR-0010 follow-up (2026-09-30): a judgement other than NO_CHANGE never
-- vanishes. When it can bind to neither an admitted thesis (a
-- ThesisRevisionCandidate) nor the company's model (a forecast revision or a
-- ForecastRevisionProposal), it leaves exactly one labelled notice for the
-- owner instead: ``needs_thesis`` when there is no admitted thesis it could
-- name, ``thesis_superseded`` when the thesis version it named has since been
-- revised by a person. A notice changes nothing and decides nothing; it is
-- the owner's pointer to the ADR-0001 admission queue.
CREATE TABLE IF NOT EXISTS judgement_followup_notices (
    notice_id TEXT PRIMARY KEY,
    judgement_ref TEXT NOT NULL UNIQUE REFERENCES event_judgements(judgement_id),
    company_ref TEXT NOT NULL,
    notice_kind TEXT NOT NULL CHECK(notice_kind IN ('needs_thesis', 'thesis_superseded')),
    decision TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS judgement_followup_notices_by_company
ON judgement_followup_notices(company_ref, created_at DESC, notice_id);

CREATE TRIGGER IF NOT EXISTS judgement_followup_notices_insert_guard
BEFORE INSERT ON judgement_followup_notices WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'judgement follow-up notice insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS judgement_followup_notices_no_update
BEFORE UPDATE ON judgement_followup_notices BEGIN
    SELECT RAISE(ABORT, 'judgement follow-up notices are immutable');
END;
CREATE TRIGGER IF NOT EXISTS judgement_followup_notices_no_delete
BEFORE DELETE ON judgement_followup_notices BEGIN
    SELECT RAISE(ABORT, 'judgement follow-up notices are immutable');
END;

-- 2026-10-06: a judgement whose preferred view is to keep the current thesis
-- (it names admitted, current thesis versions but proposes neither a new
-- statement nor a new confidence) owes the owner a record, not a decision.
-- Writing it as a ThesisRevisionCandidate offered an 「接受」 that ADR-0008
-- refuses ("this revision says exactly what the current version says"):
-- live legacy ACN, 2 candidates, 2026-10-01.  So it is written here instead,
-- one row per judgement, append-only, and it is listed under the weekly
-- brief's 事件判断的后续.  It changes nothing and decides nothing.
CREATE TABLE IF NOT EXISTS judgement_no_revision_followups (
    followup_id TEXT PRIMARY KEY,
    judgement_ref TEXT NOT NULL UNIQUE REFERENCES event_judgements(judgement_id),
    company_ref TEXT NOT NULL,
    decision TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS judgement_no_revision_followups_by_company
ON judgement_no_revision_followups(company_ref, created_at DESC, followup_id);

CREATE TRIGGER IF NOT EXISTS judgement_no_revision_followups_insert_guard
BEFORE INSERT ON judgement_no_revision_followups WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'judgement no-revision follow-up insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS judgement_no_revision_followups_no_update
BEFORE UPDATE ON judgement_no_revision_followups BEGIN
    SELECT RAISE(ABORT, 'judgement no-revision follow-ups are immutable');
END;
CREATE TRIGGER IF NOT EXISTS judgement_no_revision_followups_no_delete
BEFORE DELETE ON judgement_no_revision_followups BEGIN
    SELECT RAISE(ABORT, 'judgement no-revision follow-ups are immutable');
END;
