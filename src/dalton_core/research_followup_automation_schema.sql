CREATE TABLE IF NOT EXISTS research_followup_automation_versions (
 version_id TEXT PRIMARY KEY, group_ref TEXT NOT NULL, company_ref TEXT NOT NULL,
 record_json TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(group_ref,content_hash)
);
CREATE TABLE IF NOT EXISTS research_followup_spec_bindings (
 spec_ref TEXT PRIMARY KEY, company_ref TEXT NOT NULL, record_json TEXT NOT NULL,
 content_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS followup_automation_insert BEFORE INSERT ON research_followup_automation_versions
WHEN dalton_followup_automation_authorized()=0 BEGIN SELECT RAISE(ABORT,'followup automation requires authority'); END;
CREATE TRIGGER IF NOT EXISTS followup_spec_binding_insert BEFORE INSERT ON research_followup_spec_bindings
WHEN dalton_followup_automation_authorized()=0 BEGIN SELECT RAISE(ABORT,'followup automation requires authority'); END;
CREATE TRIGGER IF NOT EXISTS followup_automation_no_update BEFORE UPDATE ON research_followup_automation_versions
BEGIN SELECT RAISE(ABORT,'followup automation is immutable'); END;
CREATE TRIGGER IF NOT EXISTS followup_automation_no_delete BEFORE DELETE ON research_followup_automation_versions
BEGIN SELECT RAISE(ABORT,'followup automation is immutable'); END;
CREATE TRIGGER IF NOT EXISTS followup_spec_binding_no_update BEFORE UPDATE ON research_followup_spec_bindings
BEGIN SELECT RAISE(ABORT,'followup spec binding is immutable'); END;
CREATE TRIGGER IF NOT EXISTS followup_spec_binding_no_delete BEFORE DELETE ON research_followup_spec_bindings
BEGIN SELECT RAISE(ABORT,'followup spec binding is immutable'); END;
