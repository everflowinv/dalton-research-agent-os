"""MaterialRequest ledger (batch C/E stub): "this product needs this material".

The research capability diagnosis (2026-10-09) found that a product's 「材料缺少」
was free text with no consumer.  This module is the ledger that gives it one:
a drafter, a sufficiency check or a planner that lacks a required material
calls :func:`open_request`; the material acquisition service (batch E) reads
:func:`open_requests`, works its source ladder, and writes what it tried with
:func:`record_attempt` and how it ended with :func:`close`.  This batch builds
the ledger and the API only -- nothing here fetches anything.

A request::

    requester  {lane, product_ref, run_ref, section}   who needs it
    subject    company_ref / industry_ref              about whom
    need       {kind, doc_kinds, data_item, question}  what
    period     e.g. "quarter_ending:2026-08-31"        for which period
    source_priority, acceptance, budget, deadline
    digest     sha256(subject, need, period)           the identity

Idempotent by ``digest``: two products asking for the same transcript share
one request (the second requester is recorded as a ``joined`` event).  Status
is ``open`` / ``found`` / ``partial`` / ``exhausted`` / ``timeout`` /
``superseded`` and is the newest event's; ``found``, ``exhausted``,
``timeout`` and ``superseded`` are terminal.

Storage: ``<state_dir>/material-requests.sqlite``, its own file (like the
evidence index), append-only by trigger.  The same file holds
``material_pack_builds`` -- one row per material pack a lane drafted from,
so the cockpit can show the last pack's hash and time.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = "material-request:0.1"
LEDGER_DB = "material-requests.sqlite"
STATUSES: tuple[str, ...] = ("open", "found", "partial", "exhausted", "timeout", "superseded")
TERMINAL: frozenset[str] = frozenset({"found", "exhausted", "timeout", "superseded"})
ACTIVE: tuple[str, ...] = ("open", "partial")
NEED_KINDS: tuple[str, ...] = ("document", "data", "model", "answer")
DEFAULT_DEADLINE_HOURS = 24
DEFAULT_BUDGET: Mapping[str, Any] = {"pool": "material_acquisition", "max_attempts": 6}
#: The source ladder of the diagnosis (batch E), used when a caller names none.
DEFAULT_SOURCE_PRIORITY: tuple[str, ...] = (
    "indexed_corpus", "sales_notes", "alphaengine", "sec_edgar", "guidepoint", "web")
STATUS_LABELS: Mapping[str, str] = {
    "open": "待获取", "found": "已找到", "partial": "部分找到", "exhausted": "来源已用尽",
    "timeout": "已超时", "superseded": "已被取代",
}
_ACTOR_RE = re.compile(r"(automation|human):[A-Za-z0-9._:-]{1,128}")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS material_requests (
    request_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL UNIQUE,
    subject_ref TEXT NOT NULL,
    need_kind TEXT NOT NULL,
    period TEXT,
    requester_lane TEXT NOT NULL,
    deadline TEXT,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS material_requests_subject ON material_requests(subject_ref, created_at);

CREATE TABLE IF NOT EXISTS material_request_events (
    event_id INTEGER PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES material_requests(request_id),
    event_kind TEXT NOT NULL CHECK (event_kind IN ('opened', 'joined', 'attempt', 'closed')),
    status TEXT NOT NULL CHECK (status IN ('open', 'found', 'partial', 'exhausted', 'timeout', 'superseded')),
    requester_json TEXT,
    record_json TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS material_request_events_request ON material_request_events(request_id, event_id);

CREATE TABLE IF NOT EXISTS material_pack_builds (
    build_id INTEGER PRIMARY KEY,
    company_ref TEXT NOT NULL,
    product TEXT NOT NULL,
    recipe_version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    as_of TEXT,
    total_chars INTEGER NOT NULL,
    sufficiency_status TEXT,
    run_ref TEXT,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS material_pack_builds_company ON material_pack_builds(company_ref, product, build_id);

CREATE TRIGGER IF NOT EXISTS material_requests_no_update BEFORE UPDATE ON material_requests BEGIN
    SELECT RAISE(ABORT, 'material requests are append-only');
END;
CREATE TRIGGER IF NOT EXISTS material_requests_no_delete BEFORE DELETE ON material_requests BEGIN
    SELECT RAISE(ABORT, 'material requests are append-only');
END;
CREATE TRIGGER IF NOT EXISTS material_request_events_no_update BEFORE UPDATE ON material_request_events BEGIN
    SELECT RAISE(ABORT, 'material request events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS material_request_events_no_delete BEFORE DELETE ON material_request_events BEGIN
    SELECT RAISE(ABORT, 'material request events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS material_pack_builds_no_update BEFORE UPDATE ON material_pack_builds BEGIN
    SELECT RAISE(ABORT, 'material pack builds are append-only');
END;
CREATE TRIGGER IF NOT EXISTS material_pack_builds_no_delete BEFORE DELETE ON material_pack_builds BEGIN
    SELECT RAISE(ABORT, 'material pack builds are append-only');
END;
"""


class MaterialRequestError(ValueError):
    """A malformed request or an impossible transition."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def ledger_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / LEDGER_DB


def _connect(state_dir: str | Path) -> sqlite3.Connection:
    path = ledger_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.executescript(_SCHEMA)
    return connection


@contextmanager
def _writer(state_dir: str | Path) -> Iterator[sqlite3.Connection]:
    connection = _connect(state_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()


@contextmanager
def _reader(state_dir: str | Path) -> Iterator[sqlite3.Connection | None]:
    path = ledger_path(state_dir)
    if not path.is_file():
        yield None
        return
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        yield None
        return
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# normalising a request
# ---------------------------------------------------------------------------


def _text(value: Any, name: str, *, maximum: int = 512, required: bool = True) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise MaterialRequestError(f"{name} is required")
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise MaterialRequestError(f"{name} must be text of at most {maximum} characters")
    return value.strip()


def normalise_requester(requester: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(requester, Mapping):
        raise MaterialRequestError("requester must be {lane, product_ref, run_ref, section}")
    return {"lane": _text(requester.get("lane"), "requester.lane", maximum=128),
            "product_ref": _text(requester.get("product_ref"), "requester.product_ref", required=False),
            "run_ref": _text(requester.get("run_ref"), "requester.run_ref", required=False),
            "section": _text(requester.get("section"), "requester.section", required=False)}


def normalise_need(need: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(need, Mapping):
        raise MaterialRequestError("need must be {kind, doc_kinds, data_item, question}")
    kind = need.get("kind")
    if kind not in NEED_KINDS:
        raise MaterialRequestError(f"need.kind must be one of {list(NEED_KINDS)}")
    doc_kinds = need.get("doc_kinds") or []
    if isinstance(doc_kinds, str) or not all(isinstance(item, str) and item for item in doc_kinds):
        raise MaterialRequestError("need.doc_kinds must be a list of document kinds")
    out = {"kind": kind, "doc_kinds": sorted(set(doc_kinds)),
           "data_item": _text(need.get("data_item"), "need.data_item", required=False),
           "question": _text(need.get("question"), "need.question", maximum=2000, required=False)}
    if kind == "document" and not out["doc_kinds"]:
        raise MaterialRequestError("a document need names its doc_kinds")
    if kind in ("data", "model") and not out["data_item"]:
        raise MaterialRequestError(f"a {kind} need names its data_item")
    if kind == "answer" and not out["question"]:
        raise MaterialRequestError("an answer need carries its question")
    return out


def request_digest(subject: str, need: Mapping[str, Any], period: str | None) -> str:
    """The identity of a request: ``sha256(subject, need, period)``."""

    return _sha({"subject": subject, "need": normalise_need(need), "period": period or None})


def _status_of(connection: sqlite3.Connection, request_id: str) -> tuple[str, bool, list[dict[str, Any]]]:
    rows = connection.execute(
        "SELECT event_id, event_kind, status, requester_json, record_json, actor_ref, created_at "
        "FROM material_request_events WHERE request_id=? ORDER BY event_id", (request_id,)).fetchall()
    events = []
    for row in rows:
        record = json.loads(row["record_json"])
        events.append({"event_id": row["event_id"], "event_kind": row["event_kind"], "status": row["status"],
                       "requester": json.loads(row["requester_json"]) if row["requester_json"] else None,
                       "actor_ref": row["actor_ref"], "created_at": row["created_at"], **record})
    status = events[-1]["status"] if events else "open"
    # Batch E: a request ``reopen``-ed after its negative-cache window (an
    # ``opened`` event after a ``closed`` one) is open again; only a close
    # after the newest opening counts.
    last_open = max((index for index, event in enumerate(events) if event["event_kind"] == "opened"),
                    default=-1)
    closed = any(event["event_kind"] == "closed" for event in events[last_open + 1:])
    return status, closed, events


def _view(connection: sqlite3.Connection, row: sqlite3.Row, *, now: str | None = None,
          with_events: bool = False) -> dict[str, Any]:
    record = json.loads(row["record_json"])
    status, closed, events = _status_of(connection, row["request_id"])
    requesters = [record["requester"]] + [event["requester"] for event in events
                                          if event["event_kind"] == "joined" and event.get("requester")]
    attempts = [event for event in events if event["event_kind"] == "attempt"]
    moment = now or _now()
    reopenings = [event for event in events if event["event_kind"] == "opened" and event.get("reopened")]
    if reopenings:
        # A reopened request carries the deadline of its newest round.
        record = {**record, "deadline": reopenings[-1].get("deadline") or record.get("deadline"),
                  "reopened_at": reopenings[-1]["created_at"], "round": len(reopenings) + 1}
    out = {**record, "status": status, "status_label": STATUS_LABELS[status], "closed": closed,
           "terminal": status in TERMINAL or closed, "requesters": requesters,
           "attempt_count": len(attempts),
           "last_event_at": events[-1]["created_at"] if events else record["created_at"],
           "overdue": bool(record.get("deadline")) and status in ACTIVE and not closed
                      and str(record["deadline"]) < moment}
    if with_events:
        out["events"] = events
    return out


# ---------------------------------------------------------------------------
# the API
# ---------------------------------------------------------------------------


def open_request(state_dir: str | Path, *, requester: Mapping[str, Any], subject: str,
                 need: Mapping[str, Any], period: str | None = None,
                 source_priority: Sequence[str] | None = None,
                 acceptance: Mapping[str, Any] | None = None, budget: Mapping[str, Any] | None = None,
                 deadline: str | None = None, actor_ref: str = "automation:material-request",
                 now: str | None = None) -> dict[str, Any]:
    """Open (or join) the request for this material; idempotent by digest.

    Returns the request view plus ``write_status``: ``fresh`` (new request),
    ``joined`` (an existing request gained this requester) or ``duplicate``
    (this requester had already asked).  An existing request is returned as
    it stands, whatever its status -- a different period is a different
    digest and a new request.
    """

    subject = _text(subject, "subject", maximum=256)
    who = normalise_requester(requester)
    what = normalise_need(need)
    period = _text(period, "period", maximum=128, required=False)
    if not _ACTOR_RE.fullmatch(str(actor_ref)):
        raise MaterialRequestError("actor_ref must be automation:… or human:…")
    stamp = now or _now()
    digest = request_digest(subject, what, period)
    if deadline is None:
        deadline = (datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    + timedelta(hours=DEFAULT_DEADLINE_HOURS)).isoformat(timespec="seconds")
    record = {
        "schema_version": SCHEMA_VERSION,
        "id": "material-request:" + digest[:32],
        "digest": digest,
        "requester": who,
        "subject": subject,
        "need": what,
        "period": period,
        "source_priority": list(source_priority or DEFAULT_SOURCE_PRIORITY),
        "acceptance": dict(acceptance or {}),
        "budget": {**DEFAULT_BUDGET, **dict(budget or {})},
        "deadline": deadline,
        "actor_ref": actor_ref,
        "created_at": stamp,
    }
    record["content_hash"] = _sha({k: v for k, v in record.items() if k != "content_hash"})
    with _writer(state_dir) as connection:
        row = connection.execute("SELECT * FROM material_requests WHERE digest=?", (digest,)).fetchone()
        if row is not None:
            current = _view(connection, row, now=stamp)
            if who in current["requesters"]:
                return {**current, "write_status": "duplicate"}
            connection.execute(
                "INSERT INTO material_request_events(request_id,event_kind,status,requester_json,record_json,"
                "actor_ref,created_at) VALUES (?,?,?,?,?,?,?)",
                (row["request_id"], "joined", current["status"], _canonical(who), _canonical({}), actor_ref, stamp))
            return {**_view(connection, row, now=stamp), "write_status": "joined"}
        connection.execute(
            "INSERT INTO material_requests(request_id,digest,subject_ref,need_kind,period,requester_lane,deadline,"
            "record_json,content_hash,actor_ref,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (record["id"], digest, subject, what["kind"], period, who["lane"], deadline, _canonical(record),
             record["content_hash"], actor_ref, stamp))
        connection.execute(
            "INSERT INTO material_request_events(request_id,event_kind,status,requester_json,record_json,actor_ref,"
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (record["id"], "opened", "open", _canonical(who), _canonical({}), actor_ref, stamp))
        row = connection.execute("SELECT * FROM material_requests WHERE request_id=?", (record["id"],)).fetchone()
        return {**_view(connection, row, now=stamp), "write_status": "fresh"}


def record_attempt(state_dir: str | Path, request_id: str, *, source: str, outcome: str,
                   detail: str | None = None, items: Sequence[Mapping[str, Any]] | None = None,
                   status: str | None = None, cost_micros: int = 0, query: Any = None, calls: int = 0,
                   actor_ref: str = "automation:material-acquisition", now: str | None = None) -> dict[str, Any]:
    """One rung of the source ladder tried: what, where, with what result.

    ``status`` moves the request (``open`` or ``partial``; terminal outcomes
    go through :func:`close`).  A closed request refuses further attempts.
    """

    source = _text(source, "source", maximum=128)
    outcome = _text(outcome, "outcome", maximum=64)
    if status is not None and status not in ACTIVE:
        raise MaterialRequestError("an attempt leaves a request open or partial; use close() to end it")
    if not _ACTOR_RE.fullmatch(str(actor_ref)):
        raise MaterialRequestError("actor_ref must be automation:… or human:…")
    stamp = now or _now()
    with _writer(state_dir) as connection:
        row = connection.execute("SELECT * FROM material_requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            raise MaterialRequestError(f"no such request: {request_id}")
        current, closed, _ = _status_of(connection, request_id)
        if closed or current in TERMINAL:
            raise MaterialRequestError(f"{request_id} is already {current}")
        body = {"source": source, "outcome": outcome, "detail": (detail or "")[:2000] or None,
                "items": [dict(item) for item in items or ()][:50], "cost_micros": int(cost_micros)}
        # Batch E: what was asked of the source and how many of its calls it
        # took (the ``material_acquisition`` pool counts calls as well as dollars).
        if query is not None:
            if isinstance(query, str):
                body["query"] = query[:2000]
            elif isinstance(query, Mapping):
                body["query"] = json.loads(_canonical(dict(query)))
            else:
                body["query"] = [str(q)[:400] for q in list(query)[:5]]
        if calls:
            body["calls"] = int(calls)
        connection.execute(
            "INSERT INTO material_request_events(request_id,event_kind,status,requester_json,record_json,actor_ref,"
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (request_id, "attempt", status or current, None, _canonical(body), actor_ref, stamp))
        return _view(connection, row, now=stamp, with_events=True)


def close(state_dir: str | Path, request_id: str, *, status: str, reason: str,
          items: Sequence[Mapping[str, Any]] | None = None,
          actor_ref: str = "automation:material-acquisition", now: str | None = None) -> dict[str, Any]:
    """End a request: ``found`` / ``partial`` / ``exhausted`` / ``timeout`` / ``superseded``.

    Idempotent for the same status; a different second ending is refused.
    """

    if status not in STATUSES or status == "open":
        raise MaterialRequestError(f"close status must be one of {[s for s in STATUSES if s != 'open']}")
    reason = _text(reason, "reason", maximum=2000)
    if not _ACTOR_RE.fullmatch(str(actor_ref)):
        raise MaterialRequestError("actor_ref must be automation:… or human:…")
    stamp = now or _now()
    with _writer(state_dir) as connection:
        row = connection.execute("SELECT * FROM material_requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            raise MaterialRequestError(f"no such request: {request_id}")
        current, closed, _ = _status_of(connection, request_id)
        if closed:
            if current == status:
                return {**_view(connection, row, now=stamp), "write_status": "duplicate"}
            raise MaterialRequestError(f"{request_id} was already closed as {current}")
        body = {"reason": reason, "items": [dict(item) for item in items or ()][:50]}
        connection.execute(
            "INSERT INTO material_request_events(request_id,event_kind,status,requester_json,record_json,actor_ref,"
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (request_id, "closed", status, None, _canonical(body), actor_ref, stamp))
        return {**_view(connection, row, now=stamp), "write_status": "fresh"}


def reopen(state_dir: str | Path, request_id: str, *, reason: str, deadline: str | None = None,
           actor_ref: str = "automation:material-acquisition", now: str | None = None) -> dict[str, Any]:
    """Start a new round for an ``exhausted`` or ``timeout`` request (batch E).

    The negative cache: the material acquisition service does not retry an
    exhausted request for 7 days unless new material for the company was
    discovered; when it does, this appends an ``opened`` event, so the
    request (same digest, same id, same requesters) is ``open`` again with a
    fresh deadline and its earlier rounds' attempts stay on the record.
    ``found`` and ``superseded`` are final and refused; an active request is
    returned unchanged (``write_status: duplicate``).
    """

    reason = _text(reason, "reason", maximum=2000)
    if not _ACTOR_RE.fullmatch(str(actor_ref)):
        raise MaterialRequestError("actor_ref must be automation:… or human:…")
    stamp = now or _now()
    if deadline is None:
        deadline = (datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    + timedelta(hours=DEFAULT_DEADLINE_HOURS)).isoformat(timespec="seconds")
    with _writer(state_dir) as connection:
        row = connection.execute("SELECT * FROM material_requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            raise MaterialRequestError(f"no such request: {request_id}")
        current, closed, _ = _status_of(connection, request_id)
        if not closed and current in ACTIVE:
            return {**_view(connection, row, now=stamp), "write_status": "duplicate"}
        if current not in ("exhausted", "timeout", "partial"):
            raise MaterialRequestError(f"{request_id} is {current}; only exhausted, timed-out or partial "
                                       "requests are reopened")
        connection.execute(
            "INSERT INTO material_request_events(request_id,event_kind,status,requester_json,record_json,actor_ref,"
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (request_id, "opened", "open", None,
             _canonical({"reopened": True, "reason": reason, "deadline": deadline, "previous_status": current}),
             actor_ref, stamp))
        return {**_view(connection, row, now=stamp), "write_status": "fresh"}


def request(state_dir: str | Path, request_id: str, *, with_events: bool = True) -> dict[str, Any] | None:
    with _reader(state_dir) as connection:
        if connection is None:
            return None
        row = connection.execute("SELECT * FROM material_requests WHERE request_id=?", (request_id,)).fetchone()
        return None if row is None else _view(connection, row, with_events=with_events)


def find_request(state_dir: str | Path, *, subject: str, need: Mapping[str, Any],
                 period: str | None = None) -> dict[str, Any] | None:
    """The request this (subject, need, period) would open, if it exists; read-only."""

    digest = request_digest(subject, need, period)
    with _reader(state_dir) as connection:
        if connection is None:
            return None
        row = connection.execute("SELECT * FROM material_requests WHERE digest=?", (digest,)).fetchone()
        return None if row is None else _view(connection, row)


def open_requests(state_dir: str | Path, *, subject: str | None = None, lane: str | None = None,
                  product_ref: str | None = None, statuses: Sequence[str] | None = ACTIVE,
                  include_closed: bool = False, limit: int = 500, now: str | None = None) -> list[dict[str, Any]]:
    """Requests in ``statuses`` (default open + partial), oldest first.

    What the acquisition service consumes; ``statuses=None`` lists every
    request.  A request closed as ``partial`` is not active unless
    ``include_closed``.  ``lane`` / ``product_ref`` match any of a request's requesters.
    """

    with _reader(state_dir) as connection:
        if connection is None:
            return []
        where, params = [], []
        if subject:
            where.append("subject_ref=?")
            params.append(subject)
        rows = connection.execute(
            "SELECT * FROM material_requests" + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY created_at, request_id", params).fetchall()
        out = []
        for row in rows:
            view = _view(connection, row, now=now)
            if statuses is not None and view["status"] not in statuses:
                continue
            if statuses is not None and view["closed"] and not include_closed:
                continue
            if lane and not any(r.get("lane") == lane for r in view["requesters"]):
                continue
            if product_ref and not any(r.get("product_ref") == product_ref for r in view["requesters"]):
                continue
            out.append(view)
            if len(out) >= max(1, int(limit)):
                break
        return out


def status_counts(state_dir: str | Path, *, subject: str | None = None) -> dict[str, int]:
    counts = {status: 0 for status in STATUSES}
    for item in open_requests(state_dir, subject=subject, statuses=None, limit=100000):
        counts[item["status"]] += 1
    return counts


# ---------------------------------------------------------------------------
# material pack builds (what a lane drafted from)
# ---------------------------------------------------------------------------


def record_pack_build(state_dir: str | Path, pack_record: Mapping[str, Any], *, run_ref: str | None = None,
                      now: str | None = None) -> dict[str, Any]:
    """Append one ``MaterialPack.record()`` the lane drafted from."""

    stamp = now or _now()
    for key in ("company_ref", "product", "recipe_version", "content_hash"):
        if not pack_record.get(key):
            raise MaterialRequestError(f"pack record lacks {key}")
    with _writer(state_dir) as connection:
        cursor = connection.execute(
            "INSERT INTO material_pack_builds(company_ref,product,recipe_version,content_hash,as_of,total_chars,"
            "sufficiency_status,run_ref,record_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pack_record["company_ref"], pack_record["product"], pack_record["recipe_version"],
             pack_record["content_hash"], pack_record.get("as_of"), int(pack_record.get("total_chars") or 0),
             (pack_record.get("sufficiency") or {}).get("status"), run_ref, _canonical(dict(pack_record)), stamp))
        return {"build_id": cursor.lastrowid, "created_at": stamp, **dict(pack_record)}


def last_pack_build(state_dir: str | Path, company_ref: str, product: str | None = None) -> dict[str, Any] | None:
    with _reader(state_dir) as connection:
        if connection is None:
            return None
        sql = "SELECT * FROM material_pack_builds WHERE company_ref=?"
        params: list[Any] = [company_ref]
        if product:
            sql += " AND product=?"
            params.append(product)
        row = connection.execute(sql + " ORDER BY build_id DESC LIMIT 1", params).fetchone()
        if row is None:
            return None
        return {"build_id": row["build_id"], "product": row["product"], "recipe_version": row["recipe_version"],
                "content_hash": row["content_hash"], "as_of": row["as_of"], "total_chars": row["total_chars"],
                "sufficiency_status": row["sufficiency_status"], "run_ref": row["run_ref"],
                "created_at": row["created_at"]}


__all__ = [
    "ACTIVE", "DEFAULT_DEADLINE_HOURS", "DEFAULT_SOURCE_PRIORITY", "LEDGER_DB", "MaterialRequestError", "NEED_KINDS",
    "SCHEMA_VERSION", "STATUSES", "STATUS_LABELS", "TERMINAL", "close", "find_request", "last_pack_build",
    "ledger_path", "normalise_need", "normalise_requester", "open_request", "open_requests", "record_attempt",
    "record_pack_build", "reopen", "request", "request_digest", "status_counts",
]
