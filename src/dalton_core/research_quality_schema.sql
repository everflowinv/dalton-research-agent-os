-- Q1: what one artefact scored against one rubric, kept forever.
--
-- A score is a statement about an exact document under an exact standard, so
-- the row binds both by hash: the artefact's content hash and the rubric's.
-- If either moves, the next score is a new version and the version chain shows
-- when the standard changed rather than the record changing under it.
--
-- The two layers are stored apart inside record_json (`deterministic` and
-- `judge`, plus an optional `verifier`), because one is a fact about the
-- document and the other is a model reading it; a reader has to be able to
-- trust the first without trusting the second.
--
-- scoring_identity_hash is what makes re-scoring the same document under the
-- same rubric with the same model configuration a duplicate rather than a
-- second opinion.  Without it, a score that came out badly could simply be
-- asked again -- which is the failure mode of every quality gate that scores
-- on demand.  The scorer version is part of that identity, so fixing a broken
-- check can still be applied to a document that the broken check had scored.

CREATE TABLE IF NOT EXISTS research_quality_score_versions (
    version_id TEXT PRIMARY KEY,
    score_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_ref TEXT REFERENCES research_quality_score_versions(version_id),
    artefact_kind TEXT NOT NULL CHECK(artefact_kind IN (
        -- 'weekly_brief' added by Q2. The table had never been deployed
        -- when it was widened, so no live database carries the narrower
        -- CHECK; a deployed one would have needed a migration, because
        -- CREATE TABLE IF NOT EXISTS does not revisit a constraint.
        'initial_screen','ask_answer','company_dossier','weekly_brief'
    )),
    target_ref TEXT NOT NULL,
    target_hash TEXT NOT NULL,
    subject_ref TEXT,
    rubric_ref TEXT NOT NULL,
    rubric_hash TEXT NOT NULL,
    scorer_version TEXT NOT NULL,
    model_config_fingerprint TEXT NOT NULL,
    scoring_identity_hash TEXT NOT NULL,
    judge_status TEXT,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(score_ref, version_number)
);

-- One *settled* score per identity.  A refused judge is not settled: it
-- returned nothing readable, and letting one malformed reply permanently
-- occupy an identity would mean the document could never be judged under this
-- rubric again.  So the uniqueness that enforces the duplicate rule covers
-- deterministic-only rows (judge_status IS NULL) and scored ones, and leaves
-- refusals out; `record()` still refuses a second refusal, so a retry loop
-- cannot fill the chain with them.
CREATE UNIQUE INDEX IF NOT EXISTS idx_research_quality_settled_identity
ON research_quality_score_versions(score_ref, scoring_identity_hash)
WHERE judge_status IS NULL OR judge_status = 'scored';

CREATE INDEX IF NOT EXISTS idx_research_quality_by_target
ON research_quality_score_versions(target_ref, rubric_ref, created_at);

CREATE INDEX IF NOT EXISTS idx_research_quality_by_subject
ON research_quality_score_versions(subject_ref, artefact_kind, created_at);

CREATE TABLE IF NOT EXISTS research_quality_score_pointer (
    score_ref TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES research_quality_score_versions(version_id),
    version_number INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS research_quality_scores_authorized_insert
BEFORE INSERT ON research_quality_score_versions WHEN dalton_research_quality_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'quality score insert requires QualityScoreAuthority'); END;
CREATE TRIGGER IF NOT EXISTS research_quality_scores_no_update
BEFORE UPDATE ON research_quality_score_versions BEGIN
    SELECT RAISE(ABORT, 'quality scores are append-only'); END;
CREATE TRIGGER IF NOT EXISTS research_quality_scores_no_delete
BEFORE DELETE ON research_quality_score_versions BEGIN
    SELECT RAISE(ABORT, 'quality scores are append-only'); END;

CREATE TRIGGER IF NOT EXISTS research_quality_pointer_authorized_insert
BEFORE INSERT ON research_quality_score_pointer WHEN dalton_research_quality_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'quality score pointer insert requires QualityScoreAuthority'); END;
CREATE TRIGGER IF NOT EXISTS research_quality_pointer_authorized_update
BEFORE UPDATE ON research_quality_score_pointer WHEN dalton_research_quality_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'quality score pointer update requires QualityScoreAuthority'); END;
CREATE TRIGGER IF NOT EXISTS research_quality_pointer_no_delete
BEFORE DELETE ON research_quality_score_pointer BEGIN
    SELECT RAISE(ABORT, 'quality score pointers cannot be deleted'); END;
