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
