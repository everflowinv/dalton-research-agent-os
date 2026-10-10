"""The owner's acknowledgement of a 待我处理 card (2026-10-09).

Owner, 10-08: 「看下几个环境下"待我处理"的项目，我根本不知道该怎么处理，没办法消除掉这个通知」.
Several cards are true and have nothing to press: a page the source refuses
to serve in one piece (``fetch_failure_capped``), an earnings release whose
exhibit could not be read (``earnings_release_unread``), a spool near its
ceiling (``raw_spool_capacity``), a final text that gave up
(``publication_parked``).  A notice that can never be cleared is one the owner
stops reading.

So the owner may answer such a card with one of three choices:

* ``noted`` -- 「已知悉」: hide it until what it is about changes;
* ``snooze`` -- 「暂不提醒 N 天」: hide it for N days (1..30), or until what it
  is about changes, whichever comes first;
* ``abandon_document`` -- 「放弃这份资料」 (``fetch_failure_capped`` only): the
  acquisition coordinator stops fetching that discovered document for good,
  and the card goes with it.

**Append-only authority.**  One row per answer in
``needs_human_acknowledgements``, written by the writer op
``acknowledge_needs_human_card`` (``human:`` principals only, reason
required).  Nothing is ever updated: the newest row for a card wins, a snooze
expires by the clock, and the history of who set what aside, when and why
stays readable.

**What "changed" means** is :func:`condition_of`, one small dict per kind --
the failure count of a capped fetch, the band (90 / 95 / 98 / over) of the
spool, the accession and reason of an unread release, the source hash and
attempts of a parked text.  The page hides a card only while the newest
acknowledgement's ``condition_hash`` equals the card's current one; a new
failure, a new filing or a higher band brings it back by itself.

Read-only callers (the cockpit, :func:`dalton_core.needs_human.collect`) use
:func:`latest_acknowledgements` on a read-only connection; a Core that never
had an acknowledgement simply has no table yet.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .store import canonical_json, content_hash
from .terminology import label as _term

SCHEMA_VERSION = "needs-human-acknowledgement:0.1"
_SCHEMA_PATH = Path(__file__).with_name("needs_human_ack_schema.sql")

CHOICES: tuple[str, ...] = ("noted", "snooze", "abandon_document", "approve_spend")
# Batch am: the analyst's words come from the terminology registry ("ack").
CHOICE_LABELS: Mapping[str, str] = MappingProxyType({
    "noted": _term("ack", "noted"),
    "snooze": _term("ack", "snooze"),
    "abandon_document": _term("ack", "abandon_document"),
    "approve_spend": _term("ack", "approve_spend"),
})
#: Which kinds may be acknowledged, and with which choices.  Closed: a card
#: that asks for a decision (a gate, a memo, a held admission) is decided,
#: never acknowledged away.
KIND_CHOICES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "fetch_failure_capped": ("noted", "snooze", "abandon_document"),
    "earnings_release_unread": ("noted", "snooze"),
    "raw_spool_capacity": ("noted", "snooze"),
    "publication_parked": ("noted", "snooze"),
    # Batch E: the material acquisition doors.  ``approve_spend`` is the one
    # acknowledgement that changes something: the card's ref names the extra
    # dollars and calls, and the service adds them to that day's pool (or to
    # that request's cap) when it reads the row back.
    "material_sources_exhausted": ("noted", "snooze"),
    "material_budget_exceeded": ("approve_spend", "noted", "snooze"),
    "material_credentials_needed": ("noted", "snooze"),
})
SNOOZE_DAYS: tuple[int, ...] = (1, 3, 7, 14, 30)
MAX_SNOOZE_DAYS = 30
MAX_REASON_CHARS = 600
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_FETCH_CAP_PREFIX = "fetch-cap:"


class NeedsHumanAckError(ValueError):
    """An acknowledgement the authority refuses."""


def _utc(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def spool_band(percent: Any, over: Any) -> str:
    """The raw-spool band a card is in: crossing to a higher one re-shows it."""

    if over:
        return "over"
    value = int(percent or 0)
    return "98" if value >= 98 else "95" if value >= 95 else "90"


def condition_of(item: Mapping[str, Any]) -> dict[str, Any]:
    """What one card is about, in the terms that should bring it back if changed."""

    kind = str(item.get("kind") or "")
    detail = item.get("detail") if isinstance(item.get("detail"), Mapping) else {}
    if kind == "fetch_failure_capped":
        return {"kind": kind, "document_ref": detail.get("document_ref"),
                "consecutive_failures": detail.get("consecutive_failures")}
    if kind == "earnings_release_unread":
        return {"kind": kind, "accession": detail.get("accession"),
                "status": detail.get("status"), "reason": detail.get("reason")}
    if kind == "raw_spool_capacity":
        return {"kind": kind, "band": spool_band(detail.get("percent"), detail.get("over_ceiling")),
                "max_total_bytes": detail.get("max_total_bytes")}
    if kind in ("material_sources_exhausted", "material_credentials_needed"):
        return {"kind": kind, "ref": item.get("ref"),
                "request_refs": sorted(detail.get("request_refs") or ())}
    if kind == "material_budget_exceeded":
        return {"kind": kind, "ref": item.get("ref")}
    if kind == "publication_parked":
        return {"kind": kind, "product_hash": detail.get("product_hash"),
                "batch_ref": detail.get("batch_ref"), "attempts": detail.get("attempts"),
                "reason": detail.get("reason")}
    return {"kind": kind, "ref": item.get("ref"), "title": item.get("title")}


def condition_hash(item: Mapping[str, Any]) -> str:
    return content_hash(condition_of(item))


def acknowledge_entry(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """The choices a card offers, for its ``entry`` (``None``: not acknowledgeable)."""

    kind = str(item.get("kind") or "")
    choices = KIND_CHOICES.get(kind)
    if not choices:
        return None
    options = []
    for choice in choices:
        option: dict[str, Any] = {"choice": choice, "label": CHOICE_LABELS[choice]}
        if choice == "snooze":
            option["days"] = list(SNOOZE_DAYS)
            option["label"] = f"{_term('ack', 'snooze')}（选天数）"
        if choice == "approve_spend":
            detail = item.get("detail") if isinstance(item.get("detail"), Mapping) else {}
            option["confirm"] = (f"批准追加材料获取额度 {detail.get('approve_usd')} 美元"
                                 f"（约 {detail.get('approve_calls')} 次检索）？批准后系统继续检索剩下的来源。")
        if choice == "abandon_document":
            option["danger"] = True
            option["confirm"] = ("忽略该缺失资料：系统不再尝试获取它，这张卡片也不再出现；"
                                 "研究按现有数据继续推导。确认？")
        options.append(option)
    return {"card_ref": str(item.get("ref") or ""), "kind": kind,
            "condition_hash": condition_hash(item), "choices": options,
            "note": (f"「{_term('ack', 'noted')}」：这件事没有变化前不再提醒；情况变化（例如又失败、出现新的申报、"
                     "存储用量跨过更高一档）会自动重新出现。可以补充一句说明（选填），会和操作一起存档。")}


class NeedsHumanAckAuthority:
    """The append-only acknowledgement book.  Writes only through a DaltonStore."""

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("NeedsHumanAckAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        _migrate_choices(self.connection)

    def record(self, *, card_ref: Any, kind: Any, choice: Any, condition_hash: Any,
               reason: Any, actor_ref: Any, snooze_days: Any = None,
               idempotency_key: Any = None, now: datetime | None = None) -> dict[str, Any]:
        """Append one acknowledgement; the same idempotency key replays the first."""

        card_ref = _text(card_ref, "card_ref", 512)
        kind = _text(kind, "kind", 64)
        if kind not in KIND_CHOICES:
            raise NeedsHumanAckError("this kind of card is decided, not acknowledged")
        if choice not in KIND_CHOICES[kind]:
            raise NeedsHumanAckError(f"{kind} offers {list(KIND_CHOICES[kind])}")
        if not isinstance(condition_hash, str) or _SHA.match(condition_hash) is None:
            raise NeedsHumanAckError("condition_hash must be a 64-hex digest")
        reason = _text(reason, "reason", MAX_REASON_CHARS)
        actor_ref = _text(actor_ref, "actor_ref", 160)
        if not actor_ref.startswith("human:") or len(actor_ref) <= len("human:"):
            raise NeedsHumanAckError("only a person may acknowledge a card")
        moment = _utc(now)
        until = None
        if choice == "snooze":
            if isinstance(snooze_days, bool) or not isinstance(snooze_days, int) \
                    or not 1 <= snooze_days <= MAX_SNOOZE_DAYS:
                raise NeedsHumanAckError("snooze_days must be 1..30")
            until = _iso(moment + timedelta(days=snooze_days))
        elif snooze_days is not None:
            raise NeedsHumanAckError("only a snooze takes snooze_days")
        document_ref = None
        if choice == "approve_spend" and not card_ref.startswith("material-budget:"):
            raise NeedsHumanAckError("only a material budget card names an amount to approve")
        if choice == "abandon_document":
            if not card_ref.startswith(_FETCH_CAP_PREFIX):
                raise NeedsHumanAckError("only a capped fetch names a document to abandon")
            document_ref = card_ref[len(_FETCH_CAP_PREFIX):]
            if not document_ref:
                raise NeedsHumanAckError("the card names no document")
        if idempotency_key is not None:
            idempotency_key = _text(idempotency_key, "idempotency_key", 200)
        request = {"card_ref": card_ref, "kind": kind, "choice": choice,
                   "condition_hash": condition_hash, "snooze_days": snooze_days,
                   "document_ref": document_ref, "reason": reason, "actor_ref": actor_ref}
        with self.store._transaction() as cur:
            if idempotency_key is not None:
                row = cur.execute(
                    "SELECT record_json FROM needs_human_acknowledgements WHERE idempotency_key=?",
                    (idempotency_key,)).fetchone()
                if row is not None:
                    existing = json.loads(row[0])
                    if existing.get("request_hash") != content_hash(request):
                        raise NeedsHumanAckError(
                            "idempotency key was already used for a different acknowledgement")
                    return {**existing, "status": "replayed"}
            if document_ref is not None:
                found = cur.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='coverage_mission_discovered_documents'").fetchone()
                if found is None or cur.execute(
                        "SELECT 1 FROM coverage_mission_discovered_documents "
                        "WHERE document_ref=? LIMIT 1", (document_ref,)).fetchone() is None:
                    raise NeedsHumanAckError("no discovered document has this ref")
            last = cur.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM needs_human_acknowledgements").fetchone()[0]
            sequence = int(last) + 1
            created_at = moment.isoformat(timespec="microseconds")
            record = {
                "schema_version": SCHEMA_VERSION,
                "id": "needs-human-ack:" + content_hash(
                    {"sequence": sequence, "request": request, "at": created_at})[:32],
                "sequence": sequence, **request, "until": until,
                "request_hash": content_hash(request), "created_at": created_at,
            }
            record["content_hash"] = content_hash(record)
            cur.execute(
                "INSERT INTO needs_human_acknowledgements(ack_id,sequence,card_ref,kind,choice,"
                "condition_hash,until_at,document_ref,reason,actor_ref,idempotency_key,"
                "record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], sequence, card_ref, kind, choice, condition_hash, until,
                 document_ref, reason, actor_ref, idempotency_key, canonical_json(record),
                 record["content_hash"], created_at))
        return {**record, "status": "recorded"}


def _migrate_choices(connection: sqlite3.Connection) -> bool:
    """Batch E: widen the ``choice`` CHECK of a Core created before ``approve_spend``.

    SQLite cannot alter a CHECK in place, so the (small, append-only) table is
    rebuilt once: renamed aside, recreated from the schema file without its
    triggers, its rows copied unchanged, the old copy dropped, and the
    schema's indexes and triggers created again.  Idempotent: a table whose
    definition already names every choice is left alone.
    """

    row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                             "AND name='needs_human_acknowledgements'").fetchone()
    if row is None or all(f"'{choice}'" in str(row[0]) for choice in CHOICES):
        return False
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    match = re.search(r"CREATE TABLE IF NOT EXISTS needs_human_acknowledgements \(.*?\n\);", schema, re.S)
    if match is None:
        raise NeedsHumanAckError("acknowledgement schema has no table definition")
    connection.execute("SAVEPOINT needs_human_ack_choices")
    try:
        connection.execute("ALTER TABLE needs_human_acknowledgements RENAME TO needs_human_acknowledgements_pre_e")
        connection.execute(match.group(0))
        connection.execute("INSERT INTO needs_human_acknowledgements SELECT * FROM needs_human_acknowledgements_pre_e")
        connection.execute("DROP TABLE needs_human_acknowledgements_pre_e")
        connection.execute("RELEASE needs_human_ack_choices")
    except BaseException:
        connection.execute("ROLLBACK TO needs_human_ack_choices")
        connection.execute("RELEASE needs_human_ack_choices")
        raise
    connection.executescript(schema)
    return True


def _text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NeedsHumanAckError(f"{name} is required")
    value = value.strip()
    if len(value) > maximum:
        raise NeedsHumanAckError(f"{name} must be at most {maximum} characters")
    return value


def _rows(connection: Any, sql: str, params: Iterable[Any] = ()) -> list[Any]:
    try:
        return connection.execute(sql, tuple(params)).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise


def latest_acknowledgements(connection: Any) -> dict[str, dict[str, Any]]:
    """card_ref -> its newest acknowledgement.  Works read-only and on a fresh Core."""

    out: dict[str, dict[str, Any]] = {}
    if connection is None:
        return out
    for row in _rows(connection, "SELECT card_ref, record_json FROM needs_human_acknowledgements "
                                 "ORDER BY sequence"):
        try:
            record = json.loads(row[1])
        except (TypeError, ValueError):
            continue
        if isinstance(record, dict):
            out[str(row[0])] = record
    return out


def abandoned_document_refs(connection: Any) -> tuple[str, ...]:
    """Every discovered document the owner gave up on (「放弃这份资料」).

    The acquisition coordinator honours it: an abandoned document is never
    offered for a fetch or a retry again.  Abandoning is final by design --
    a newer row for the same card cannot undo it -- because nothing in the
    source changes by itself; a document that becomes servable again is found
    by the next search under a new discovery.
    """

    if connection is None:
        return ()
    return tuple(sorted({str(row[0]) for row in _rows(
        connection, "SELECT DISTINCT document_ref FROM needs_human_acknowledgements "
                    "WHERE choice='abandon_document' AND document_ref IS NOT NULL")}))


def hides(item: Mapping[str, Any], ack: Mapping[str, Any] | None, *,
          now: datetime | None = None) -> bool:
    """Does this acknowledgement still hide this card right now?"""

    if not isinstance(ack, Mapping):
        return False
    if ack.get("choice") == "abandon_document":
        return True
    if ack.get("condition_hash") != condition_hash(item):
        return False
    until = ack.get("until")
    if until:
        try:
            moment = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
        except ValueError:
            return False
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return _utc(now) < moment
    return True


def split_acknowledged(items: Iterable[dict[str, Any]], acks: Mapping[str, Mapping[str, Any]],
                       *, now: datetime | None = None
                       ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(shown, acknowledged)``: each acknowledged card as a short line for the page."""

    shown: list[dict[str, Any]] = []
    hidden: list[dict[str, Any]] = []
    for item in items:
        ack = acks.get(str(item.get("ref") or ""))
        if hides(item, ack, now=now):
            hidden.append({"kind": item.get("kind"), "ref": item.get("ref"),
                           "title": item.get("title"), "choice": ack["choice"],
                           "choice_label": CHOICE_LABELS.get(str(ack["choice"]), ack["choice"]),
                           "until": ack.get("until"), "reason": ack.get("reason"),
                           "at": ack.get("created_at")})
        else:
            shown.append(item)
    return shown, hidden


__all__ = [
    "CHOICES", "CHOICE_LABELS", "KIND_CHOICES", "MAX_SNOOZE_DAYS", "NeedsHumanAckAuthority",
    "NeedsHumanAckError", "SCHEMA_VERSION", "SNOOZE_DAYS", "abandoned_document_refs",
    "acknowledge_entry", "condition_hash", "condition_of", "hides", "latest_acknowledgements",
    "split_acknowledged", "spool_band",
]
