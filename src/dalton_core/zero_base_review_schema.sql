-- W4 (Chem retrospective §1 row 3, §5 item 3): the monthly zero-base review.
--
-- Chem designed this and never ran it: the `dalton-coverage-zero-base` cron
-- fired zero times and the task list held three `zero_base_review` rows that
-- nothing ever picked up. The idea was right and the plumbing was absent, so
-- here the plumbing comes first: a lane with a cadence, an authority with a
-- version chain, and an output whose only power is to propose.
--
-- The question is deliberately not "has anything changed". It is "if we were
-- seeing this company for the first time today, would we form a view at all"
-- -- asked from a zero position, once a month per covered company past the
-- Initial Screen, and again after each earnings calibration, because a print
-- is the moment the accumulated story is most likely to be wrong.
--
-- One chain per company per mission. A review is a version of the same record
-- rather than a new one, so "what did we think in March, and what changed by
-- June" is a version walk like every other output in this system (ADR-0008).
-- `inputs_hash` covers everything the prompt was built from, so a review run
-- twice over an unmoved Ledger is a `duplicate` and costs one call, not two.
CREATE TABLE IF NOT EXISTS zero_base_review_versions (
    version_id TEXT PRIMARY KEY,
    review_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_ref TEXT REFERENCES zero_base_review_versions(version_id),
    mission_ref TEXT NOT NULL,
    mission_version_ref TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    -- Which of the two things made this review owed. Closed, because a third
    -- trigger would be a cadence decision and cadence decisions are policy.
    trigger TEXT NOT NULL CHECK(trigger IN ('monthly', 'earnings_calibration')),
    -- The month for a monthly review, the calibration's version ref for an
    -- earnings one. It is what makes "this period has been reviewed" a
    -- question the lane can answer with one read.
    period_label TEXT NOT NULL,
    -- The first of the four questions, promoted to a column because it is the
    -- one a reader sorts on: would we form a view today at all.
    form_a_view TEXT NOT NULL CHECK(form_a_view IN ('yes', 'no', 'unclear')),
    rewritten_line_count INTEGER NOT NULL CHECK(rewritten_line_count >= 0),
    stale_debate_count INTEGER NOT NULL CHECK(stale_debate_count >= 0),
    candidate_count INTEGER NOT NULL CHECK(candidate_count >= 0),
    next_verification_date TEXT,
    inputs_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(review_ref, version_number),
    UNIQUE(review_ref, inputs_hash)
);

CREATE INDEX IF NOT EXISTS zero_base_reviews_by_company
ON zero_base_review_versions(company_ref, created_at DESC, version_id);
CREATE INDEX IF NOT EXISTS zero_base_reviews_by_period
ON zero_base_review_versions(review_ref, trigger, period_label);

CREATE TABLE IF NOT EXISTS zero_base_review_pointer (
    review_ref TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES zero_base_review_versions(version_id),
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    company_ref TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ADR-0007, reached from the other end.
--
-- A revision candidate raised by the judgement lane hangs off the judgement
-- that raised it, and that table's `judgement_ref` is a foreign key into
-- `event_judgements`. A zero-base review is not a judgement about an event --
-- there is no event; the whole point is that nothing had to happen -- so its
-- candidates could not live in that table without either a fake judgement row
-- or a rewrite of an append-only authority. Both were refused.
--
-- So the candidates live here, in the same shape, with the same
-- `checkpoint_kind`, and `ThesisRevisionAuthority` reads both tables. The
-- decision, the decision ledger and the resulting ThesisVersion are the
-- existing ones: there is one place a person answers a revision proposal, and
-- one thesis chain, whatever raised the proposal.
CREATE TABLE IF NOT EXISTS zero_base_revision_candidates (
    candidate_id TEXT PRIMARY KEY,
    review_ref TEXT NOT NULL,
    review_version_ref TEXT NOT NULL REFERENCES zero_base_review_versions(version_id),
    thesis_version_ref TEXT NOT NULL,
    thesis_version_hash TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    decision TEXT NOT NULL,
    checkpoint_kind TEXT NOT NULL CHECK(checkpoint_kind = 'thesis_revision_candidate'),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(review_version_ref, thesis_version_ref)
);

CREATE INDEX IF NOT EXISTS zero_base_revision_candidates_by_company
ON zero_base_revision_candidates(company_ref, created_at, candidate_id);

CREATE TRIGGER IF NOT EXISTS zero_base_reviews_insert_guard
BEFORE INSERT ON zero_base_review_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'zero base review insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS zero_base_reviews_no_update
BEFORE UPDATE ON zero_base_review_versions BEGIN
    SELECT RAISE(ABORT, 'zero base reviews are append-only');
END;
CREATE TRIGGER IF NOT EXISTS zero_base_reviews_no_delete
BEFORE DELETE ON zero_base_review_versions BEGIN
    SELECT RAISE(ABORT, 'zero base reviews are append-only');
END;

CREATE TRIGGER IF NOT EXISTS zero_base_review_pointer_insert_guard
BEFORE INSERT ON zero_base_review_pointer WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'zero base review pointer requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS zero_base_review_pointer_update_guard
BEFORE UPDATE ON zero_base_review_pointer WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'zero base review pointer requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS zero_base_review_pointer_no_delete
BEFORE DELETE ON zero_base_review_pointer BEGIN
    SELECT RAISE(ABORT, 'zero base review pointer rows are never deleted');
END;

CREATE TRIGGER IF NOT EXISTS zero_base_revision_candidates_insert_guard
BEFORE INSERT ON zero_base_revision_candidates WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'zero base revision candidate insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS zero_base_revision_candidates_no_update
BEFORE UPDATE ON zero_base_revision_candidates BEGIN
    SELECT RAISE(ABORT, 'zero base revision candidates are immutable');
END;
CREATE TRIGGER IF NOT EXISTS zero_base_revision_candidates_no_delete
BEFORE DELETE ON zero_base_revision_candidates BEGIN
    SELECT RAISE(ABORT, 'zero base revision candidates are immutable');
END;
