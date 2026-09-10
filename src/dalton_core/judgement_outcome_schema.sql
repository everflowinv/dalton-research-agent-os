-- W4 (Chem retrospective §3.3): the book that says whether "no change" was right.
--
-- The judgement lane records `no_change` with a reason, which is more than
-- Chem ever had, and still cannot show that any of those decisions were
-- correct.  Chem's 89 of 92 NO_CHANGE is the case in point: a lane that only
-- ever agrees with itself is indistinguishable from a lane that is not
-- thinking, and no amount of prose in the `because` field settles which one
-- it is.
--
-- So every judgement gets a derived row here, written by a frozen formula
-- with no model call in it:
--
--   * a `no_change` whose price then ran against the equal-weight basket of
--     the other covered companies, past the same threshold and over the same
--     window the `price_divergence` detector already uses, is a
--     `should_have_moved` **candidate**.  It is not a verdict and nothing
--     acts on it; it is the row a person needs in order to ask why.
--   * a `revise`-shaped decision whose direction a later actual confirms is
--     `moved_right`.
--
-- Two properties make it a ledger rather than a dashboard:
--
-- **Replayable.**  Every input the formula read is in `record_json` and
-- hashed into `inputs_hash`; recomputing the check from the same rows
-- produces the same hash and writes nothing.  The formula is versioned
-- (`formula_ref`), so a changed formula is visibly a different reading rather
-- than a silent restatement of an old one.
--
-- **Append-only with a version chain.**  A check is `pending` until the
-- window has enough settled sessions in it, and then it is not.  That is a
-- new version of the same check, not an edit: how long we waited before we
-- could tell is part of what this table is for.
CREATE TABLE IF NOT EXISTS judgement_outcome_check_versions (
    version_id TEXT PRIMARY KEY,
    check_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES judgement_outcome_check_versions(version_id),
    judgement_ref TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    check_kind TEXT NOT NULL CHECK(check_kind IN ('no_change', 'revise')),
    outcome TEXT NOT NULL,
    formula_ref TEXT NOT NULL,
    -- Every number the formula read.  This is the duplicate rule: a recheck
    -- that read the same rows writes nothing.
    inputs_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(check_ref, version_number),
    UNIQUE(check_ref, inputs_hash)
);

CREATE INDEX IF NOT EXISTS judgement_outcome_checks_by_company
ON judgement_outcome_check_versions(company_ref, created_at DESC, version_id);
CREATE INDEX IF NOT EXISTS judgement_outcome_checks_by_judgement
ON judgement_outcome_check_versions(judgement_ref, version_number DESC);

CREATE TABLE IF NOT EXISTS judgement_outcome_check_pointer (
    check_ref TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES judgement_outcome_check_versions(version_id),
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    judgement_ref TEXT NOT NULL,
    company_ref TEXT NOT NULL,
    check_kind TEXT NOT NULL,
    outcome TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS judgement_outcome_pointer_by_outcome
ON judgement_outcome_check_pointer(outcome, company_ref);

CREATE TRIGGER IF NOT EXISTS judgement_outcome_checks_insert_guard
BEFORE INSERT ON judgement_outcome_check_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'judgement outcome check insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS judgement_outcome_checks_no_update
BEFORE UPDATE ON judgement_outcome_check_versions BEGIN
    SELECT RAISE(ABORT, 'judgement outcome checks are append-only');
END;
CREATE TRIGGER IF NOT EXISTS judgement_outcome_checks_no_delete
BEFORE DELETE ON judgement_outcome_check_versions BEGIN
    SELECT RAISE(ABORT, 'judgement outcome checks are append-only');
END;

CREATE TRIGGER IF NOT EXISTS judgement_outcome_pointer_insert_guard
BEFORE INSERT ON judgement_outcome_check_pointer WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'judgement outcome pointer requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS judgement_outcome_pointer_update_guard
BEFORE UPDATE ON judgement_outcome_check_pointer WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'judgement outcome pointer requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS judgement_outcome_pointer_no_delete
BEFORE DELETE ON judgement_outcome_check_pointer BEGIN
    SELECT RAISE(ABORT, 'judgement outcome pointer rows are never deleted');
END;
