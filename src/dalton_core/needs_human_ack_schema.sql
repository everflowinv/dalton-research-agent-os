-- 2026-10-09: the owner's acknowledgement of a 待我处理 card.
--
-- Some cards have no completing action -- a document the source will not
-- serve, an earnings release the 10-Q will supersede, a spool near its
-- ceiling -- and until now they could not be cleared at all.  One append-only
-- row per acknowledgement: which card, under which condition (a hash of what
-- the card was about when the owner saw it), what the owner chose (已知悉 /
-- 暂不提醒 N 天 / 放弃这份资料), until when, why, who.  The page hides a card
-- while the newest row for it still matches its condition and has not
-- expired; nothing here is ever updated, so a changed condition or an
-- expired snooze brings the card back by itself, and the history stays.

CREATE TABLE IF NOT EXISTS needs_human_acknowledgements (
    ack_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL UNIQUE CHECK(sequence >= 1),
    card_ref TEXT NOT NULL CHECK(length(trim(card_ref)) > 0),
    kind TEXT NOT NULL CHECK(length(trim(kind)) > 0),
    choice TEXT NOT NULL CHECK(choice IN ('noted', 'snooze', 'abandon_document', 'approve_spend')),
    condition_hash TEXT NOT NULL CHECK(length(condition_hash) = 64),
    until_at TEXT,
    document_ref TEXT,
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    actor_ref TEXT NOT NULL CHECK(actor_ref LIKE 'human:%'),
    idempotency_key TEXT UNIQUE,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS needs_human_acknowledgements_by_card
ON needs_human_acknowledgements(card_ref, sequence DESC);

CREATE TRIGGER IF NOT EXISTS needs_human_acknowledgements_insert_guard
BEFORE INSERT ON needs_human_acknowledgements WHEN dalton_authorized() = 0 BEGIN
    SELECT RAISE(ABORT, 'needs-human acknowledgement insert requires DaltonStore');
END;
CREATE TRIGGER IF NOT EXISTS needs_human_acknowledgements_no_update
BEFORE UPDATE ON needs_human_acknowledgements BEGIN
    SELECT RAISE(ABORT, 'needs-human acknowledgements are append-only');
END;
CREATE TRIGGER IF NOT EXISTS needs_human_acknowledgements_no_delete
BEFORE DELETE ON needs_human_acknowledgements BEGIN
    SELECT RAISE(ABORT, 'needs-human acknowledgements are append-only');
END;
