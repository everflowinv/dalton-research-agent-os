-- Additive mission-progress authority. Research pack FKs and old records stay intact.
CREATE TABLE IF NOT EXISTS weekly_brief_progress_issues (
 version_id TEXT PRIMARY KEY, brief_ref TEXT NOT NULL, version_number INTEGER NOT NULL CHECK(version_number>=1),
 prior_version_id TEXT REFERENCES weekly_brief_progress_issues(version_id), industry_ref TEXT NOT NULL,
 snapshot_hash TEXT NOT NULL, markdown_hash TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
 record_json TEXT NOT NULL, content_hash TEXT NOT NULL, actor_ref TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(brief_ref,version_number)
);
CREATE TABLE IF NOT EXISTS weekly_brief_progress_cycle_admissions (
 cycle_id TEXT PRIMARY KEY, plan_ref TEXT NOT NULL, plan_hash TEXT NOT NULL,
 policy_version_ref TEXT NOT NULL REFERENCES governance_policy_versions(policy_version_id), policy_version_hash TEXT NOT NULL,
 scheduled_for TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL, brief_ref TEXT NOT NULL,
 issue_version_ref TEXT NOT NULL UNIQUE REFERENCES weekly_brief_progress_issues(version_id),
 destination_ref TEXT NOT NULL, record_json TEXT NOT NULL, content_hash TEXT NOT NULL,
 actor_ref TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(brief_ref,scheduled_for)
);
CREATE TABLE IF NOT EXISTS weekly_brief_progress_deliveries (
 delivery_id TEXT PRIMARY KEY, issue_version_ref TEXT NOT NULL REFERENCES weekly_brief_progress_issues(version_id),
 issue_version_hash TEXT NOT NULL, destination_ref TEXT NOT NULL, external_message_ref TEXT NOT NULL,
 artifact_sha256 TEXT NOT NULL, record_json TEXT NOT NULL, content_hash TEXT NOT NULL,
 actor_ref TEXT NOT NULL, delivered_at TEXT NOT NULL, UNIQUE(issue_version_ref,destination_ref,external_message_ref)
);
-- Transactions serialize inserts. Both directions enforce one slot across plans/variants.
CREATE TRIGGER IF NOT EXISTS weekly_brief_progress_slot_unique BEFORE INSERT ON weekly_brief_progress_cycle_admissions
WHEN EXISTS(SELECT 1 FROM weekly_brief_cycle_admissions WHERE brief_ref=NEW.brief_ref AND julianday(scheduled_for)=julianday(NEW.scheduled_for))
BEGIN SELECT RAISE(ABORT,'weekly brief slot already admitted as research'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_research_slot_unique BEFORE INSERT ON weekly_brief_cycle_admissions
WHEN EXISTS(SELECT 1 FROM weekly_brief_progress_cycle_admissions WHERE brief_ref=NEW.brief_ref AND julianday(scheduled_for)=julianday(NEW.scheduled_for))
OR EXISTS(SELECT 1 FROM weekly_brief_cycle_admissions WHERE brief_ref=NEW.brief_ref AND julianday(scheduled_for)=julianday(NEW.scheduled_for))
BEGIN SELECT RAISE(ABORT,'weekly brief slot already admitted'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_progress_issues_authorized_insert BEFORE INSERT ON weekly_brief_progress_issues
WHEN dalton_weekly_brief_authorized()=0 BEGIN SELECT RAISE(ABORT,'mission progress insert requires WeeklyBriefAuthority'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_progress_issues_no_update BEFORE UPDATE ON weekly_brief_progress_issues
BEGIN SELECT RAISE(ABORT,'mission progress records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_progress_issues_no_delete BEFORE DELETE ON weekly_brief_progress_issues
BEGIN SELECT RAISE(ABORT,'mission progress records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_progress_cycle_admissions_authorized_insert BEFORE INSERT ON weekly_brief_progress_cycle_admissions
WHEN dalton_weekly_brief_authorized()=0 BEGIN SELECT RAISE(ABORT,'mission progress insert requires WeeklyBriefAuthority'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_progress_cycle_admissions_no_update BEFORE UPDATE ON weekly_brief_progress_cycle_admissions
BEGIN SELECT RAISE(ABORT,'mission progress records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_progress_cycle_admissions_no_delete BEFORE DELETE ON weekly_brief_progress_cycle_admissions
BEGIN SELECT RAISE(ABORT,'mission progress records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_progress_deliveries_authorized_insert BEFORE INSERT ON weekly_brief_progress_deliveries
WHEN dalton_weekly_brief_authorized()=0 BEGIN SELECT RAISE(ABORT,'mission progress insert requires WeeklyBriefAuthority'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_progress_deliveries_no_update BEFORE UPDATE ON weekly_brief_progress_deliveries
BEGIN SELECT RAISE(ABORT,'mission progress records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS weekly_brief_progress_deliveries_no_delete BEFORE DELETE ON weekly_brief_progress_deliveries
BEGIN SELECT RAISE(ABORT,'mission progress records are immutable'); END;
