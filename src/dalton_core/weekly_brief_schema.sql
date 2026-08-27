PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS weekly_brief_issue_versions (
    version_id TEXT PRIMARY KEY,
    brief_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES weekly_brief_issue_versions(version_id),
    industry_ref TEXT NOT NULL,
    evidence_pack_version_ref TEXT NOT NULL REFERENCES industry_evidence_pack_versions(version_id),
    evidence_pack_version_hash TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    markdown_hash TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(brief_ref, version_number)
);

CREATE TABLE IF NOT EXISTS weekly_brief_issue_pointer (
    brief_ref TEXT PRIMARY KEY,
    version_id TEXT NOT NULL UNIQUE REFERENCES weekly_brief_issue_versions(version_id),
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weekly_brief_deliveries (
    delivery_id TEXT PRIMARY KEY,
    issue_version_ref TEXT NOT NULL REFERENCES weekly_brief_issue_versions(version_id),
    issue_version_hash TEXT NOT NULL,
    destination_ref TEXT NOT NULL,
    external_message_ref TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    UNIQUE(issue_version_ref, destination_ref, external_message_ref)
);

CREATE TABLE IF NOT EXISTS weekly_brief_feedback (
    feedback_id TEXT PRIMARY KEY,
    issue_version_ref TEXT NOT NULL REFERENCES weekly_brief_issue_versions(version_id),
    issue_version_hash TEXT NOT NULL,
    verdict TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    prior_feedback_ref TEXT REFERENCES weekly_brief_feedback(feedback_id),
    subject_ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weekly_brief_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_weekly_brief_issue_history
ON weekly_brief_issue_versions(brief_ref, version_number);
CREATE INDEX IF NOT EXISTS idx_weekly_brief_deliveries_issue
ON weekly_brief_deliveries(issue_version_ref, delivered_at);
CREATE INDEX IF NOT EXISTS idx_weekly_brief_feedback_issue
ON weekly_brief_feedback(issue_version_ref, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_weekly_brief_feedback_single_successor
ON weekly_brief_feedback(prior_feedback_ref)
WHERE prior_feedback_ref IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS weekly_brief_issue_versions_authorized_insert
BEFORE INSERT ON weekly_brief_issue_versions WHEN dalton_weekly_brief_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'weekly brief issue insert requires WeeklyBriefAuthority'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_issue_pointer_authorized_insert
BEFORE INSERT ON weekly_brief_issue_pointer WHEN dalton_weekly_brief_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'weekly brief pointer insert requires WeeklyBriefAuthority'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_issue_pointer_authorized_update
BEFORE UPDATE ON weekly_brief_issue_pointer WHEN dalton_weekly_brief_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'weekly brief pointer update requires WeeklyBriefAuthority'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_deliveries_authorized_insert
BEFORE INSERT ON weekly_brief_deliveries WHEN dalton_weekly_brief_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'weekly brief delivery insert requires WeeklyBriefAuthority'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_feedback_authorized_insert
BEFORE INSERT ON weekly_brief_feedback WHEN dalton_weekly_brief_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'weekly brief feedback insert requires WeeklyBriefAuthority'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_idempotency_authorized_insert
BEFORE INSERT ON weekly_brief_idempotency WHEN dalton_weekly_brief_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'weekly brief idempotency insert requires WeeklyBriefAuthority'); END;

CREATE TRIGGER IF NOT EXISTS weekly_brief_issue_versions_no_update
BEFORE UPDATE ON weekly_brief_issue_versions BEGIN SELECT RAISE(ABORT, 'weekly brief issues are immutable'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_issue_versions_no_delete
BEFORE DELETE ON weekly_brief_issue_versions BEGIN SELECT RAISE(ABORT, 'weekly brief issues are immutable'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_issue_pointer_no_delete
BEFORE DELETE ON weekly_brief_issue_pointer BEGIN SELECT RAISE(ABORT, 'weekly brief pointer cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_deliveries_no_update
BEFORE UPDATE ON weekly_brief_deliveries BEGIN SELECT RAISE(ABORT, 'weekly brief deliveries are immutable'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_deliveries_no_delete
BEFORE DELETE ON weekly_brief_deliveries BEGIN SELECT RAISE(ABORT, 'weekly brief deliveries are immutable'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_feedback_no_update
BEFORE UPDATE ON weekly_brief_feedback BEGIN SELECT RAISE(ABORT, 'weekly brief feedback is immutable'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_feedback_no_delete
BEFORE DELETE ON weekly_brief_feedback BEGIN SELECT RAISE(ABORT, 'weekly brief feedback is immutable'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_idempotency_no_update
BEFORE UPDATE ON weekly_brief_idempotency BEGIN SELECT RAISE(ABORT, 'weekly brief idempotency is immutable'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_idempotency_no_delete
BEFORE DELETE ON weekly_brief_idempotency BEGIN SELECT RAISE(ABORT, 'weekly brief idempotency is immutable'); END;
