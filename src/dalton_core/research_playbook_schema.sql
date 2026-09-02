PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS research_playbook_versions (
    playbook_version_id TEXT PRIMARY KEY,
    playbook_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES research_playbook_versions(playbook_version_id),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(playbook_ref, version_number)
);

CREATE TABLE IF NOT EXISTS research_playbook_pointer (
    playbook_ref TEXT PRIMARY KEY,
    playbook_version_id TEXT NOT NULL UNIQUE REFERENCES research_playbook_versions(playbook_version_id),
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_playbook_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_playbook_history
ON research_playbook_versions(playbook_ref, version_number);

CREATE TRIGGER IF NOT EXISTS research_playbook_versions_authorized_insert
BEFORE INSERT ON research_playbook_versions WHEN dalton_research_playbook_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research playbook insert requires ResearchPlaybookAuthority'); END;
CREATE TRIGGER IF NOT EXISTS research_playbook_pointer_authorized_insert
BEFORE INSERT ON research_playbook_pointer WHEN dalton_research_playbook_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research playbook pointer insert requires ResearchPlaybookAuthority'); END;
CREATE TRIGGER IF NOT EXISTS research_playbook_pointer_authorized_update
BEFORE UPDATE ON research_playbook_pointer WHEN dalton_research_playbook_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research playbook pointer update requires ResearchPlaybookAuthority'); END;
CREATE TRIGGER IF NOT EXISTS research_playbook_idempotency_authorized_insert
BEFORE INSERT ON research_playbook_idempotency WHEN dalton_research_playbook_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'research playbook idempotency insert requires ResearchPlaybookAuthority'); END;

CREATE TRIGGER IF NOT EXISTS research_playbook_versions_no_update
BEFORE UPDATE ON research_playbook_versions BEGIN SELECT RAISE(ABORT, 'research playbook versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_playbook_versions_no_delete
BEFORE DELETE ON research_playbook_versions BEGIN SELECT RAISE(ABORT, 'research playbook versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_playbook_pointer_no_delete
BEFORE DELETE ON research_playbook_pointer BEGIN SELECT RAISE(ABORT, 'research playbook pointer cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS research_playbook_idempotency_no_update
BEFORE UPDATE ON research_playbook_idempotency BEGIN SELECT RAISE(ABORT, 'research playbook idempotency is immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_playbook_idempotency_no_delete
BEFORE DELETE ON research_playbook_idempotency BEGIN SELECT RAISE(ABORT, 'research playbook idempotency is immutable'); END;
