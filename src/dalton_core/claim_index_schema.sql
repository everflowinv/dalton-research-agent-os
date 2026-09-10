-- P12b: the Claim index, an append-only projection beside the Ledger.
--
-- Claims are immutable and their contract is frozen. What this table adds is
-- everything a reader needs in order to *choose* between 2,170 of them --
-- what the claim is about, when it is about, how much its source is worth, and
-- whether three copies of the same fact are three facts -- without touching a
-- single ``claim_versions`` row.
--
-- One entry per ``claim_version_ref``, versioned. A tag is a judgement, and a
-- judgement made in September about a claim from August can be made again in
-- October with better rules; both survive and the chain says which came first.
-- Re-recording an unchanged tag is a ``duplicate``, not a new version, so a
-- lane that re-reads the same claim every tick does not grow a version chain
-- made of identical rows.
--
-- ``is_canonical`` is stored rather than computed at read time because it is a
-- statement about a group at a moment: the same claim is canonical until a
-- better-sourced or later one joins its group, and that transition is exactly
-- the thing a reader will later want to see having happened. When a group's
-- canonical member changes, the entries that changed get a new version whose
-- ``revision_reason`` says ``recanonicalised`` -- nothing is edited.
--
-- ``status`` is deliberately absent. It is projected from adjudications by
-- ``DaltonStore.project_claim_status`` and storing a copy here would create a
-- second answer to a question that already has one.

CREATE TABLE IF NOT EXISTS claim_index_entry_versions (
    version_id TEXT PRIMARY KEY,
    entry_ref TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    prior_version_id TEXT REFERENCES claim_index_entry_versions(version_id),
    claim_version_ref TEXT NOT NULL,
    claim_version_hash TEXT NOT NULL,
    claim_ref TEXT NOT NULL,
    subject_ref TEXT NOT NULL,
    aspect TEXT NOT NULL,
    as_of TEXT,
    as_of_basis TEXT NOT NULL,
    importance TEXT NOT NULL CHECK(importance IN (
        'filing', 'management_statement', 'internal_prior', 'sell_side',
        'news', 'other')),
    dedupe_group_ref TEXT NOT NULL,
    is_canonical INTEGER NOT NULL CHECK(is_canonical IN (0, 1)),
    tagger_ref TEXT NOT NULL,
    evidence_kind TEXT NOT NULL DEFAULT 'statement',
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(entry_ref, version_number)
);

-- The reads this exists to serve: one claim's current tag; a company's file by
-- aspect and date; and every member of a duplicate group.
CREATE INDEX IF NOT EXISTS claim_index_entries_by_claim
ON claim_index_entry_versions(claim_version_ref, version_number);
CREATE INDEX IF NOT EXISTS claim_index_entries_by_subject
ON claim_index_entry_versions(subject_ref, aspect, as_of);
CREATE INDEX IF NOT EXISTS claim_index_entries_by_group
ON claim_index_entry_versions(dedupe_group_ref, version_number);

CREATE TRIGGER IF NOT EXISTS claim_index_entry_insert_guard
BEFORE INSERT ON claim_index_entry_versions WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'claim index entry insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS claim_index_entry_no_update
BEFORE UPDATE ON claim_index_entry_versions BEGIN
    SELECT RAISE(ABORT, 'claim index entries are immutable');
END;
CREATE TRIGGER IF NOT EXISTS claim_index_entry_no_delete
BEFORE DELETE ON claim_index_entry_versions BEGIN
    SELECT RAISE(ABORT, 'claim index entries are immutable');
END;
