-- Batch B (2026-10-09): append-only re-derivations of consensus period labels.
--
-- A stored consensus version is never rewritten.  When the period calendar
-- learns more (an 8-K earnings release advances the last reported period, a
-- vendor block is found not to have rolled), the corrected labels for that
-- version are appended here, bound to the version, the relabel rule version
-- and the calendar evidence used.  Same evidence -> same binding -> no row.
CREATE TABLE IF NOT EXISTS consensus_period_relabels (
    relabel_id TEXT PRIMARY KEY,
    company_ref TEXT NOT NULL,
    consensus_version_ref TEXT NOT NULL,
    relabel_version TEXT NOT NULL,
    binding_hash TEXT NOT NULL UNIQUE,
    last_reported_period_end TEXT NOT NULL,
    changed_count INTEGER NOT NULL CHECK(changed_count >= 0),
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS consensus_period_relabels_by_version
ON consensus_period_relabels(consensus_version_ref, created_at);
CREATE TRIGGER IF NOT EXISTS consensus_period_relabels_insert_guard
BEFORE INSERT ON consensus_period_relabels WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'consensus period relabel insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS consensus_period_relabels_no_update
BEFORE UPDATE ON consensus_period_relabels BEGIN
    SELECT RAISE(ABORT, 'consensus period relabels are append-only');
END;
CREATE TRIGGER IF NOT EXISTS consensus_period_relabels_no_delete
BEFORE DELETE ON consensus_period_relabels BEGIN
    SELECT RAISE(ABORT, 'consensus period relabels are append-only');
END;
