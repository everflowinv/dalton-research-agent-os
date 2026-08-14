"""Durable barrier journal for the trusted connector runner.

The journal proves whether a physical transport may have started.  Research
claims never read it as evidence; recovery code uses it to choose released
versus conservative indeterminate quota settlement.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .connector_runner import validate_connector_runner_request
from .store import canonical_json, content_hash


_SCHEMA_PATH = Path(__file__).with_name("runner_journal_schema.sql")
_TRANSITIONS = {
    None: frozenset({"admitted"}),
    "admitted": frozenset({"reserved"}),
    "reserved": frozenset({"transport_started", "observed", "released_recovered"}),
    "transport_started": frozenset({"observed", "indeterminate_recovered"}),
    "observed": frozenset({"responded"}),
    "responded": frozenset(),
    "released_recovered": frozenset(),
    "indeterminate_recovered": frozenset({"responded"}),
}


class RunnerJournalError(Exception):
    pass


class RunnerJournalConflict(RunnerJournalError):
    pass


class RunnerJournalNotFound(RunnerJournalError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RunnerJournalError("runner journal clock must return aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _stored_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise RunnerJournalError("event_at must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RunnerJournalError("event_at must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise RunnerJournalError("event_at must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _payload(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RunnerJournalError("journal payload must be an object")
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise RunnerJournalError("journal payload must be finite JSON") from exc


class RunnerJournal:
    """Append-only journal stored beside Connector authority in Core DB."""

    def __init__(self, store: Any, *, clock: Callable[[], datetime] | None = None):
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("RunnerJournal requires a DaltonStore")
        self._store = store
        self._connection: sqlite3.Connection = store.connection
        self._clock = clock or _now
        # FULL means a committed transport_started barrier is durable before
        # adapter invocation.  SQLite may reject changing this inside a txn.
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def begin_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        wire = validate_connector_runner_request(request)
        request_json = canonical_json(wire)
        now = _timestamp(self._clock())
        with self._store._transaction() as cur:
            row = cur.execute(
                "SELECT request_hash,request_json FROM runner_request_journal "
                "WHERE runner_request_ref=?",
                (wire["id"],),
            ).fetchone()
            if row is not None:
                if (
                    row["request_hash"] != wire["content_hash"]
                    or row["request_json"] != request_json
                ):
                    raise RunnerJournalConflict(
                        "runner request id already identifies another payload"
                    )
                event = self._latest_with_cursor(cur, wire["id"])
                return {"write_status": "duplicate", **event}
            cur.execute(
                "INSERT INTO runner_request_journal"
                "(runner_request_ref,request_hash,connector_invocation_ref,request_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    wire["id"], wire["content_hash"],
                    wire["connector_invocation_ref"], request_json, now,
                ),
            )
            event = self._append_with_cursor(
                cur,
                wire["id"],
                "admitted",
                {"request_hash": wire["content_hash"]},
                event_at=now,
            )
            return {"write_status": "fresh", **event}

    def append(
        self,
        runner_request_ref: str,
        state: str,
        payload: Mapping[str, Any],
        *,
        event_at: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(runner_request_ref, str) or not runner_request_ref:
            raise RunnerJournalError("runner_request_ref must be non-empty")
        if state not in _TRANSITIONS:
            raise RunnerJournalError("journal state is invalid")
        at = _stored_timestamp(event_at) if event_at is not None else _timestamp(self._clock())
        with self._store._transaction() as cur:
            result = self._append_with_cursor(
                cur, runner_request_ref, state, payload, event_at=at
            )
            return {"write_status": "fresh", **result}

    def _append_with_cursor(
        self,
        cur: sqlite3.Cursor,
        runner_request_ref: str,
        state: str,
        payload: Mapping[str, Any],
        *,
        event_at: str,
    ) -> dict[str, Any]:
        request = cur.execute(
            "SELECT 1 FROM runner_request_journal WHERE runner_request_ref=?",
            (runner_request_ref,),
        ).fetchone()
        if request is None:
            raise RunnerJournalNotFound(runner_request_ref)
        body = _payload(payload)
        latest = self._latest_with_cursor(cur, runner_request_ref, required=False)
        prior_state = None if latest is None else latest["state"]
        if state not in _TRANSITIONS[prior_state]:
            raise RunnerJournalConflict(
                f"invalid runner journal transition {prior_state!r} -> {state!r}"
            )
        reservation_ref = body.get("reservation_ref")
        if reservation_ref is not None and (
            not isinstance(reservation_ref, str) or not reservation_ref
        ):
            raise RunnerJournalError("reservation_ref must be null or non-empty")
        ordinal = 1 if latest is None else int(latest["request_ordinal"]) + 1
        base = {
            "runner_request_ref": runner_request_ref,
            "request_ordinal": ordinal,
            "state": state,
            "reservation_ref": reservation_ref,
            "event_at": event_at,
            "payload": body,
        }
        digest = content_hash(base)
        event_id = "runner-journal-event:" + content_hash(
            {
                "runner_request_ref": runner_request_ref,
                "request_ordinal": ordinal,
                "content_hash": digest,
            }
        )
        try:
            cur.execute(
                "INSERT INTO runner_attempt_journal_events"
                "(event_id,runner_request_ref,state,reservation_ref,event_at,payload_json,"
                "content_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    event_id, runner_request_ref, state, reservation_ref, event_at,
                    canonical_json(body), digest, _timestamp(self._clock()),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RunnerJournalConflict(
                "reservation already has a durable transport_started barrier"
            ) from exc
        return {
            "id": event_id,
            "runner_request_ref": runner_request_ref,
            "request_ordinal": ordinal,
            "state": state,
            "reservation_ref": reservation_ref,
            "event_at": event_at,
            "payload": body,
            "content_hash": digest,
        }

    def request(self, runner_request_ref: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT request_json FROM runner_request_journal WHERE runner_request_ref=?",
            (runner_request_ref,),
        ).fetchone()
        if row is None:
            raise RunnerJournalNotFound(runner_request_ref)
        return json.loads(row["request_json"])

    def latest(self, runner_request_ref: str) -> dict[str, Any]:
        row = self._latest_with_cursor(
            self._connection.cursor(), runner_request_ref, required=True
        )
        assert row is not None
        return row

    def event(self, event_ref: str) -> dict[str, Any]:
        if not isinstance(event_ref, str) or not event_ref:
            raise RunnerJournalError("event_ref must be non-empty")
        row = self._connection.execute(
            "SELECT * FROM runner_attempt_journal_events WHERE event_id=?",
            (event_ref,),
        ).fetchone()
        if row is None:
            raise RunnerJournalNotFound(event_ref)
        ordinal = self._connection.execute(
            "SELECT COUNT(*) AS n FROM runner_attempt_journal_events "
            "WHERE runner_request_ref=? AND event_seq<=?",
            (row["runner_request_ref"], row["event_seq"]),
        ).fetchone()
        return self._event(row, int(ordinal["n"]))

    def history(self, runner_request_ref: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM runner_attempt_journal_events WHERE runner_request_ref=? "
            "ORDER BY event_seq",
            (runner_request_ref,),
        ).fetchall()
        if not rows:
            raise RunnerJournalNotFound(runner_request_ref)
        result: list[dict[str, Any]] = []
        for ordinal, row in enumerate(rows, start=1):
            result.append(self._event(row, ordinal))
        return result

    def incomplete_requests(self) -> list[str]:
        rows = self._connection.execute(
            "SELECT r.runner_request_ref FROM runner_request_journal r "
            "JOIN runner_attempt_journal_events e ON e.event_seq=("
            "SELECT MAX(x.event_seq) FROM runner_attempt_journal_events x "
            "WHERE x.runner_request_ref=r.runner_request_ref) "
            "WHERE e.state NOT IN ('responded','released_recovered','indeterminate_recovered') "
            "ORDER BY r.created_at,r.runner_request_ref"
        ).fetchall()
        return [row["runner_request_ref"] for row in rows]

    def reservation_refs(self) -> set[str]:
        rows = self._connection.execute(
            "SELECT DISTINCT reservation_ref FROM runner_attempt_journal_events "
            "WHERE reservation_ref IS NOT NULL"
        ).fetchall()
        return {row["reservation_ref"] for row in rows}

    def _latest_with_cursor(
        self,
        cur: sqlite3.Cursor,
        runner_request_ref: str,
        *,
        required: bool = True,
    ) -> dict[str, Any] | None:
        row = cur.execute(
            "SELECT * FROM runner_attempt_journal_events WHERE runner_request_ref=? "
            "ORDER BY event_seq DESC LIMIT 1",
            (runner_request_ref,),
        ).fetchone()
        if row is None:
            if required:
                raise RunnerJournalNotFound(runner_request_ref)
            return None
        ordinal_row = cur.execute(
            "SELECT COUNT(*) AS n FROM runner_attempt_journal_events "
            "WHERE runner_request_ref=? AND event_seq<=?",
            (runner_request_ref, row["event_seq"]),
        ).fetchone()
        return self._event(row, int(ordinal_row["n"]))

    @staticmethod
    def _event(row: sqlite3.Row, ordinal: int) -> dict[str, Any]:
        return {
            "id": row["event_id"],
            "runner_request_ref": row["runner_request_ref"],
            "request_ordinal": ordinal,
            "state": row["state"],
            "reservation_ref": row["reservation_ref"],
            "event_at": row["event_at"],
            "payload": json.loads(row["payload_json"]),
            "content_hash": row["content_hash"],
        }


__all__ = [
    "RunnerJournal", "RunnerJournalConflict", "RunnerJournalError",
    "RunnerJournalNotFound",
]
