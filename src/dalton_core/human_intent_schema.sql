PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS intent_context_packs (
    context_pack_id TEXT PRIMARY KEY,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS human_utterance_versions (
    utterance_version_id TEXT PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    actor_ref TEXT NOT NULL,
    context_pack_ref TEXT NOT NULL,
    context_pack_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(context_pack_ref) REFERENCES intent_context_packs(context_pack_id)
);

CREATE TABLE IF NOT EXISTS intent_interpretation_attempts (
    attempt_id TEXT PRIMARY KEY,
    utterance_version_ref TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('accepted', 'rejected', 'failed')),
    error_code TEXT,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(utterance_version_ref) REFERENCES human_utterance_versions(utterance_version_id)
);

CREATE TABLE IF NOT EXISTS intent_candidate_versions (
    candidate_version_id TEXT PRIMARY KEY,
    utterance_version_ref TEXT NOT NULL UNIQUE,
    attempt_ref TEXT NOT NULL UNIQUE,
    intent_kind TEXT NOT NULL,
    disposition TEXT NOT NULL,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(utterance_version_ref) REFERENCES human_utterance_versions(utterance_version_id),
    FOREIGN KEY(attempt_ref) REFERENCES intent_interpretation_attempts(attempt_id)
);

CREATE TABLE IF NOT EXISTS intent_compose_requests (
    request_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS intent_context_packs_no_update
BEFORE UPDATE ON intent_context_packs BEGIN SELECT RAISE(ABORT, 'intent_context_packs is immutable'); END;
CREATE TRIGGER IF NOT EXISTS intent_context_packs_no_delete
BEFORE DELETE ON intent_context_packs BEGIN SELECT RAISE(ABORT, 'intent_context_packs is immutable'); END;
CREATE TRIGGER IF NOT EXISTS human_utterance_versions_no_update
BEFORE UPDATE ON human_utterance_versions BEGIN SELECT RAISE(ABORT, 'human_utterance_versions is immutable'); END;
CREATE TRIGGER IF NOT EXISTS human_utterance_versions_no_delete
BEFORE DELETE ON human_utterance_versions BEGIN SELECT RAISE(ABORT, 'human_utterance_versions is immutable'); END;
CREATE TRIGGER IF NOT EXISTS intent_interpretation_attempts_no_update
BEFORE UPDATE ON intent_interpretation_attempts BEGIN SELECT RAISE(ABORT, 'intent_interpretation_attempts is immutable'); END;
CREATE TRIGGER IF NOT EXISTS intent_interpretation_attempts_no_delete
BEFORE DELETE ON intent_interpretation_attempts BEGIN SELECT RAISE(ABORT, 'intent_interpretation_attempts is immutable'); END;
CREATE TRIGGER IF NOT EXISTS intent_candidate_versions_no_update
BEFORE UPDATE ON intent_candidate_versions BEGIN SELECT RAISE(ABORT, 'intent_candidate_versions is immutable'); END;
CREATE TRIGGER IF NOT EXISTS intent_candidate_versions_no_delete
BEFORE DELETE ON intent_candidate_versions BEGIN SELECT RAISE(ABORT, 'intent_candidate_versions is immutable'); END;
CREATE TRIGGER IF NOT EXISTS intent_compose_requests_no_update
BEFORE UPDATE ON intent_compose_requests BEGIN SELECT RAISE(ABORT, 'intent_compose_requests is immutable'); END;
CREATE TRIGGER IF NOT EXISTS intent_compose_requests_no_delete
BEFORE DELETE ON intent_compose_requests BEGIN SELECT RAISE(ABORT, 'intent_compose_requests is immutable'); END;
