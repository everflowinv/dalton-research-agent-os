"""Durable, append-only work scheduling for Dalton Core.

The scheduler owns queue time and retry policy.  Workers receive an opaque
lease token, but cannot select their attempt number, extend a lease beyond the
configured bounds, or increase the configured retry limit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .contracts import ResultEnvelope, WorkOrder
from .recorded_completion import (
    build_recorded_parent_response,
    build_recorded_parent_result,
)
from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("scheduler_schema.sql")
_ATTEMPT_STATES = frozenset(
    {"ready", "leased", "succeeded", "retryable", "failed", "expired"}
)
_RESULT_STATES = frozenset({"succeeded", "retryable", "failed"})


class SchedulerError(Exception):
    """Base error for scheduler operations."""


class SchedulerValidationError(SchedulerError):
    pass


class SchedulerConflict(SchedulerError):
    pass


class WorkNotFound(SchedulerError):
    pass


class LeaseRejected(SchedulerError):
    pass


class LeaseExpired(LeaseRejected):
    pass


class RetryExhausted(SchedulerError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise SchedulerValidationError(f"{name} must be a non-empty string")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SchedulerValidationError(f"{name} must be a positive integer")
    return value


def _positive_seconds(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise SchedulerValidationError(f"{name} must be a positive number")
    result = float(value)
    if result == float("inf") or result != result:
        raise SchedulerValidationError(f"{name} must be finite")
    return result


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SchedulerValidationError("scheduler clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise SchedulerValidationError("stored scheduler time is not RFC3339") from exc
    if parsed.tzinfo is None:
        raise SchedulerValidationError("stored scheduler time lacks timezone")
    return parsed.astimezone(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _token_hash(token: str) -> str:
    _nonempty(token, "lease_token")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _sha256(value: Any, name: str) -> str:
    value = _nonempty(value, name)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise SchedulerValidationError(f"{name} must be lowercase SHA-256 hex")
    return value


class Scheduler:
    """SQLite scheduler with atomic claim and append-only history.

    ``max_attempts`` and all lease bounds are trusted construction policy.  A
    WorkOrder or worker reply cannot override them.  ``clock`` is the only
    source of scheduler time, which makes expiry deterministic in tests and
    keeps worker-reported timestamps out of lease decisions.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        connection: sqlite3.Connection | None = None,
        clock: Callable[[], datetime] | None = None,
        max_attempts: int = 3,
        default_lease_seconds: float = 30.0,
        max_lease_seconds: float = 60.0,
        max_renew_seconds: float = 30.0,
        max_total_lease_seconds: float = 300.0,
        policy_version_id: str = "scheduler-policy-0.1",
        trusted_journal_reader: Any | None = None,
        trusted_completion_reader: Any | None = None,
    ) -> None:
        self.path = str(path)
        self.clock = clock or _utc_now
        self.max_attempts = _positive_int(max_attempts, "max_attempts")
        self.default_lease_seconds = _positive_seconds(
            default_lease_seconds, "default_lease_seconds"
        )
        self.max_lease_seconds = _positive_seconds(max_lease_seconds, "max_lease_seconds")
        self.max_renew_seconds = _positive_seconds(max_renew_seconds, "max_renew_seconds")
        self.max_total_lease_seconds = _positive_seconds(
            max_total_lease_seconds, "max_total_lease_seconds"
        )
        if self.default_lease_seconds > self.max_lease_seconds:
            raise SchedulerValidationError(
                "default_lease_seconds cannot exceed max_lease_seconds"
            )
        if self.max_lease_seconds > self.max_total_lease_seconds:
            raise SchedulerValidationError(
                "max_lease_seconds cannot exceed max_total_lease_seconds"
            )
        self.policy_version_id = _nonempty(policy_version_id, "policy_version_id")
        if trusted_journal_reader is not None:
            from .runner_journal import RunnerJournal

            if type(trusted_journal_reader) is not RunnerJournal:
                raise SchedulerValidationError(
                    "trusted_journal_reader must be an exact RunnerJournal"
                )
        if trusted_completion_reader is not None:
            from .connector_authority_port import ConnectorCompletionReceiptReader

            if type(trusted_completion_reader) is not ConnectorCompletionReceiptReader:
                raise SchedulerValidationError(
                    "trusted_completion_reader must be an exact receipt reader"
                )
        if (trusted_journal_reader is None) != (trusted_completion_reader is None):
            raise SchedulerValidationError(
                "journal and completion readers must be bound together"
            )
        self._trusted_journal_reader = trusted_journal_reader
        self._trusted_completion_reader = trusted_completion_reader
        self._authorized = False
        self.connection = connection or sqlite3.connect(self.path, isolation_level=None)
        if connection is None and self.path != ":memory:":
            os.chmod(self.path, 0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.create_function(
            "dalton_scheduler_authorized", 0, lambda: int(self._authorized)
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._ensure_schema_migrations()
        self._ensure_policy()

    @property
    def conn(self) -> sqlite3.Connection:
        return self.connection

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Scheduler":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _now(self) -> datetime:
        value = self.clock()
        # Validate and normalize every read; a mutable/faulty injected clock
        # cannot smuggle naive local time into authority rows.
        _timestamp(value)
        return value.astimezone(timezone.utc)

    def _ensure_schema_migrations(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(scheduler_attempt_events)"
            ).fetchall()
        }
        if "not_before" not in columns:
            self.connection.execute(
                "ALTER TABLE scheduler_attempt_events ADD COLUMN not_before TEXT"
            )
        if "wire_version" not in columns:
            self.connection.execute(
                "ALTER TABLE scheduler_attempt_events ADD COLUMN wire_version TEXT"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self.connection.in_transaction:
            raise RuntimeError("Scheduler operation cannot be nested")
        self.connection.execute("BEGIN IMMEDIATE")
        self._authorized = True
        try:
            yield self.connection.cursor()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            self._authorized = False

    def _policy_wire(self, created_at: str) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.policy_version_id,
            "created_at": created_at,
            "max_attempts": self.max_attempts,
            "default_lease_seconds": self.default_lease_seconds,
            "max_lease_seconds": self.max_lease_seconds,
            "max_renew_seconds": self.max_renew_seconds,
            "max_total_lease_seconds": self.max_total_lease_seconds,
        }

    def _ensure_policy(self) -> None:
        now = _timestamp(self._now())
        policy = self._policy_wire(now)
        # created_at is not policy semantics.  Existing constructors must be
        # able to reopen the same policy version at a later trusted time.
        semantic = {k: v for k, v in policy.items() if k != "created_at"}
        policy_hash = content_hash(semantic)
        with self._transaction() as cur:
            row = cur.execute(
                "SELECT policy_hash FROM scheduler_policy_versions WHERE policy_version_id=?",
                (self.policy_version_id,),
            ).fetchone()
            if row:
                if row["policy_hash"] != policy_hash:
                    raise SchedulerConflict(
                        "scheduler policy version id already has different settings"
                    )
                return
            cur.execute(
                "INSERT INTO scheduler_policy_versions "
                "(policy_version_id, policy_json, policy_hash, created_at) VALUES (?, ?, ?, ?)",
                (self.policy_version_id, canonical_json(policy), policy_hash, now),
            )

    @staticmethod
    def _work_wire(work_order: WorkOrder | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(work_order, WorkOrder):
            return work_order.to_dict()
        if not isinstance(work_order, Mapping):
            raise SchedulerValidationError("work_order must be WorkOrder or mapping")
        try:
            return WorkOrder.from_dict(work_order).to_dict()
        except Exception as exc:
            raise SchedulerValidationError(str(exc)) from exc

    @staticmethod
    def _result_wire(result: ResultEnvelope | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(result, ResultEnvelope):
            return result.to_dict()
        if not isinstance(result, Mapping):
            raise SchedulerValidationError("result_envelope must be ResultEnvelope or mapping")
        try:
            return ResultEnvelope.from_dict(result).to_dict()
        except Exception as exc:
            raise SchedulerValidationError(str(exc)) from exc

    def enqueue(self, work_order: WorkOrder | Mapping[str, Any]) -> dict[str, Any]:
        """Register one immutable WorkOrder and append attempt 1 / ready."""
        wire = self._work_wire(work_order)
        work_order_id = wire["id"]
        work_hash = content_hash(wire)
        idempotency_key = wire["idempotency_key"]
        now = _timestamp(self._now())
        with self._transaction() as cur:
            idem = cur.execute(
                "SELECT request_hash, result_json FROM scheduler_enqueue_idempotency "
                "WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if idem:
                if idem["request_hash"] == work_hash:
                    saved = json.loads(idem["result_json"])
                    saved["status"] = "duplicate"
                    return saved
                return {
                    "status": "conflict",
                    "work_order_id": work_order_id,
                    "work_order_hash": work_hash,
                    "existing_request_hash": idem["request_hash"],
                }
            existing = cur.execute(
                "SELECT work_order_hash FROM scheduler_work_orders WHERE work_order_id=?",
                (work_order_id,),
            ).fetchone()
            if existing:
                return {
                    "status": "duplicate" if existing["work_order_hash"] == work_hash else "conflict",
                    "work_order_id": work_order_id,
                    "work_order_hash": existing["work_order_hash"],
                }
            cur.execute(
                "INSERT INTO scheduler_work_orders "
                "(work_order_id, work_order_json, work_order_hash, policy_version_id, max_attempts, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    work_order_id,
                    canonical_json(wire),
                    work_hash,
                    self.policy_version_id,
                    self.max_attempts,
                    now,
                ),
            )
            event = self._append_event(
                cur,
                work_order_id=work_order_id,
                attempt_number=1,
                state="ready",
                now=now,
                reason="enqueued",
            )
            response = {
                "status": "fresh",
                "work_order_id": work_order_id,
                "work_order_hash": work_hash,
                "attempt": event,
            }
            cur.execute(
                "INSERT INTO scheduler_enqueue_idempotency "
                "(idempotency_key, request_hash, work_order_id, result_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (idempotency_key, work_hash, work_order_id, canonical_json(response), now),
            )
        return response

    def _latest_event(self, cur: sqlite3.Cursor, work_order_id: str) -> sqlite3.Row | None:
        return cur.execute(
            "SELECT * FROM scheduler_attempt_events WHERE work_order_id=? "
            "ORDER BY event_seq DESC LIMIT 1",
            (work_order_id,),
        ).fetchone()

    def _append_event(
        self,
        cur: sqlite3.Cursor,
        *,
        work_order_id: str,
        attempt_number: int,
        state: str,
        now: str,
        lease_revision_id: str | None = None,
        result_envelope_id: str | None = None,
        result_envelope_hash: str | None = None,
        reason: str | None = None,
        not_before: str | None = None,
    ) -> dict[str, Any]:
        if state not in _ATTEMPT_STATES:
            raise SchedulerValidationError(f"invalid attempt state: {state!r}")
        prior = self._latest_event(cur, work_order_id)
        event_id = _id("attempt-event")
        wire = {
            "schema_version": SCHEMA_VERSION,
            # Historical rows have NULL in the SQL wire_version column and
            # were hashed without not_before.  New rows explicitly declare
            # this epoch so an integrity audit never has to guess which
            # canonical shape produced content_hash.
            "wire_version": "0.2",
            "id": event_id,
            "created_at": now,
            "work_order_ref": work_order_id,
            "attempt_number": attempt_number,
            "state": state,
            "lease_ref": lease_revision_id,
            "result_envelope_ref": result_envelope_id,
            "result_envelope_hash": result_envelope_hash,
            "reason": reason,
            "not_before": not_before,
            "prior_event_ref": prior["event_id"] if prior else None,
        }
        wire["content_hash"] = content_hash(wire)
        cur.execute(
            "INSERT INTO scheduler_attempt_events "
            "(event_id, work_order_id, attempt_number, state, lease_revision_id, "
            "result_envelope_id, result_envelope_hash, reason, not_before, wire_version, "
            "prior_event_id, content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                work_order_id,
                attempt_number,
                state,
                lease_revision_id,
                result_envelope_id,
                result_envelope_hash,
                reason,
                not_before,
                wire["wire_version"],
                wire["prior_event_ref"],
                wire["content_hash"],
                now,
            ),
        )
        return wire

    def _new_ready_or_exhausted(
        self,
        cur: sqlite3.Cursor,
        *,
        work_order_id: str,
        completed_attempt: int,
        now: str,
        exhaustion_reason: str,
        retry_at: str | None = None,
    ) -> dict[str, Any]:
        work = cur.execute(
            "SELECT max_attempts FROM scheduler_work_orders WHERE work_order_id=?",
            (work_order_id,),
        ).fetchone()
        if work is None:
            raise WorkNotFound(work_order_id)
        if completed_attempt >= work["max_attempts"]:
            return self._append_event(
                cur,
                work_order_id=work_order_id,
                attempt_number=completed_attempt,
                state="failed",
                now=now,
                reason=exhaustion_reason,
            )
        return self._append_event(
            cur,
            work_order_id=work_order_id,
            attempt_number=completed_attempt + 1,
            state="ready",
            now=now,
            reason=f"retry_after_attempt_{completed_attempt}",
            not_before=retry_at,
        )

    def _latest_lease_for_event(
        self, cur: sqlite3.Cursor, event: sqlite3.Row
    ) -> sqlite3.Row:
        first = cur.execute(
            "SELECT lease_id FROM scheduler_leases WHERE lease_revision_id=?",
            (event["lease_revision_id"],),
        ).fetchone()
        if first is None:
            raise SchedulerConflict("leased event has no lease authority row")
        latest = cur.execute(
            "SELECT * FROM scheduler_leases WHERE lease_id=? ORDER BY lease_version DESC LIMIT 1",
            (first["lease_id"],),
        ).fetchone()
        if latest is None:
            raise SchedulerConflict("lease history is missing")
        return latest

    def _expire_one(
        self, cur: sqlite3.Cursor, event: sqlite3.Row, lease: sqlite3.Row, now: str
    ) -> dict[str, Any]:
        expired = self._append_event(
            cur,
            work_order_id=event["work_order_id"],
            attempt_number=event["attempt_number"],
            state="expired",
            now=now,
            lease_revision_id=lease["lease_revision_id"],
            reason="lease_expired",
        )
        next_event = self._new_ready_or_exhausted(
            cur,
            work_order_id=event["work_order_id"],
            completed_attempt=event["attempt_number"],
            now=now,
            exhaustion_reason="retry_exhausted_after_expiry",
        )
        return {"expired": expired, "next": next_event}

    def _expire_due(self, cur: sqlite3.Cursor, now_dt: datetime, now: str) -> list[dict[str, Any]]:
        current_leased = cur.execute(
            "SELECT e.* FROM scheduler_attempt_events e "
            "JOIN (SELECT work_order_id, MAX(event_seq) AS max_seq "
            "      FROM scheduler_attempt_events GROUP BY work_order_id) current "
            "ON current.max_seq=e.event_seq WHERE e.state='leased' "
            "ORDER BY e.event_seq"
        ).fetchall()
        expired: list[dict[str, Any]] = []
        for event in current_leased:
            lease = self._latest_lease_for_event(cur, event)
            if _parse_time(lease["expires_at"]) <= now_dt:
                expired.append(self._expire_one(cur, event, lease, now))
        return expired

    def sweep_expired(self) -> list[dict[str, Any]]:
        """Expire all overdue leases and create bounded retries atomically."""
        now_dt = self._now()
        now = _timestamp(now_dt)
        with self._transaction() as cur:
            return self._expire_due(cur, now_dt, now)

    def claim(
        self,
        owner_ref: str,
        *,
        work_order_id: str | None = None,
        lease_seconds: float | None = None,
    ) -> dict[str, Any] | None:
        """Atomically claim one ready attempt, returning the one-time token."""
        owner_ref = _nonempty(owner_ref, "owner_ref")
        if work_order_id is not None:
            work_order_id = _nonempty(work_order_id, "work_order_id")
        requested_seconds = (
            None if lease_seconds is None else _positive_seconds(lease_seconds, "lease_seconds")
        )
        now_dt = self._now()
        now = _timestamp(now_dt)
        with self._transaction() as cur:
            self._expire_due(cur, now_dt, now)
            query = (
                "SELECT e.* FROM scheduler_attempt_events e "
                "JOIN (SELECT work_order_id, MAX(event_seq) AS max_seq "
                "      FROM scheduler_attempt_events GROUP BY work_order_id) current "
                "ON current.max_seq=e.event_seq "
                "WHERE e.state='ready' "
                "AND (e.not_before IS NULL OR e.not_before<=?) "
                "AND NOT EXISTS (SELECT 1 FROM scheduler_formal_results r "
                "                WHERE r.work_order_id=e.work_order_id) "
            )
            params: tuple[Any, ...] = (now,)
            if work_order_id is not None:
                query += "AND e.work_order_id=? "
                params = (now, work_order_id)
            query += "ORDER BY e.event_seq LIMIT 1"
            event = cur.execute(query, params).fetchone()
            if event is None:
                return None
            work = cur.execute(
                "SELECT w.work_order_json, w.work_order_hash, w.policy_version_id, w.max_attempts, "
                "p.policy_json FROM scheduler_work_orders w "
                "JOIN scheduler_policy_versions p ON p.policy_version_id=w.policy_version_id "
                "WHERE w.work_order_id=?",
                (event["work_order_id"],),
            ).fetchone()
            if work is None:
                raise SchedulerConflict("ready work is missing its frozen scheduler policy")
            policy = json.loads(work["policy_json"])
            policy_default = _positive_seconds(
                policy["default_lease_seconds"], "frozen default_lease_seconds"
            )
            policy_max = _positive_seconds(
                policy["max_lease_seconds"], "frozen max_lease_seconds"
            )
            seconds = policy_default if requested_seconds is None else requested_seconds
            if seconds > policy_max:
                raise LeaseRejected("requested lease exceeds the work order's frozen policy")
            lease_id = _id("lease")
            revision_id = _id("lease-revision")
            token = secrets.token_urlsafe(32)
            token_hash = _token_hash(token)
            expires_at = _timestamp(now_dt + timedelta(seconds=seconds))
            lease_wire = {
                "schema_version": SCHEMA_VERSION,
                "id": revision_id,
                "lease_id": lease_id,
                "lease_version": 1,
                "created_at": now,
                "work_order_ref": event["work_order_id"],
                "attempt_number": event["attempt_number"],
                "owner_ref": owner_ref,
                "lease_token_hash": token_hash,
                "issued_at": now,
                "renewed_at": None,
                "expires_at": expires_at,
                "prior_lease_ref": None,
            }
            lease_wire["content_hash"] = content_hash(lease_wire)
            cur.execute(
                "INSERT INTO scheduler_leases "
                "(lease_revision_id, lease_id, lease_version, work_order_id, attempt_number, "
                "owner_ref, lease_token_hash, issued_at, renewed_at, expires_at, "
                "prior_lease_revision_id, content_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    lease_id,
                    1,
                    event["work_order_id"],
                    event["attempt_number"],
                    owner_ref,
                    token_hash,
                    now,
                    None,
                    expires_at,
                    None,
                    lease_wire["content_hash"],
                    now,
                ),
            )
            attempt = self._append_event(
                cur,
                work_order_id=event["work_order_id"],
                attempt_number=event["attempt_number"],
                state="leased",
                now=now,
                lease_revision_id=revision_id,
                reason="claimed",
            )
        return {
            "status": "leased",
            "lease_token": token,
            "lease": lease_wire,
            "attempt": attempt,
            "work_order": json.loads(work["work_order_json"]),
            "work_order_hash": work["work_order_hash"],
            "policy_version_id": work["policy_version_id"],
            "max_attempts": work["max_attempts"],
        }

    def _active_lease(
        self,
        cur: sqlite3.Cursor,
        *,
        work_order_id: str,
        attempt_number: int,
        owner_ref: str,
        lease_token: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        event = self._latest_event(cur, work_order_id)
        if event is None:
            raise WorkNotFound(work_order_id)
        if event["attempt_number"] != attempt_number or event["state"] != "leased":
            raise LeaseRejected("attempt is not the current leased attempt")
        lease = self._latest_lease_for_event(cur, event)
        supplied = _token_hash(lease_token)
        if lease["owner_ref"] != owner_ref or not hmac.compare_digest(
            lease["lease_token_hash"], supplied
        ):
            raise LeaseRejected("lease owner or token does not match")
        return event, lease

    def validate_lease_for_use(
        self,
        work_order_id: str,
        attempt_number: int,
        owner_ref: str,
        lease_token: str,
        *,
        lease_revision_ref: str,
        lease_hash: str,
        work_order_hash: str,
    ) -> dict[str, Any]:
        """Revalidate the exact current scheduler lease immediately before use.

        Reading a historical claim is not authorization.  Trusted runtimes call
        this method with the one-time lease token and the closed refs/hashes from
        their command frame.  Expired leases are transitioned through the normal
        append-only expiry path before the method raises.
        """

        work_order_id = _nonempty(work_order_id, "work_order_id")
        attempt_number = _positive_int(attempt_number, "attempt_number")
        owner_ref = _nonempty(owner_ref, "owner_ref")
        lease_token = _nonempty(lease_token, "lease_token")
        lease_revision_ref = _nonempty(lease_revision_ref, "lease_revision_ref")
        lease_hash = _sha256(lease_hash, "lease_hash")
        work_order_hash = _sha256(work_order_hash, "work_order_hash")
        now_dt = self._now()
        now = _timestamp(now_dt)
        expired = False
        result: dict[str, Any] | None = None
        with self._transaction() as cur:
            event, lease = self._active_lease(
                cur,
                work_order_id=work_order_id,
                attempt_number=attempt_number,
                owner_ref=owner_ref,
                lease_token=lease_token,
            )
            if _parse_time(lease["expires_at"]) <= now_dt:
                self._expire_one(cur, event, lease, now)
                expired = True
            else:
                if (
                    lease["lease_revision_id"] != lease_revision_ref
                    or lease["content_hash"] != lease_hash
                ):
                    raise LeaseRejected("scheduler lease revision or hash is stale")
                work = cur.execute(
                    "SELECT work_order_json,work_order_hash FROM scheduler_work_orders "
                    "WHERE work_order_id=?",
                    (work_order_id,),
                ).fetchone()
                if work is None or work["work_order_hash"] != work_order_hash:
                    raise LeaseRejected("scheduler WorkOrder hash is stale")
                lease_wire = {
                    "schema_version": SCHEMA_VERSION,
                    "id": lease["lease_revision_id"],
                    "lease_id": lease["lease_id"],
                    "lease_version": lease["lease_version"],
                    "created_at": lease["created_at"],
                    "work_order_ref": lease["work_order_id"],
                    "attempt_number": lease["attempt_number"],
                    "owner_ref": lease["owner_ref"],
                    "lease_token_hash": lease["lease_token_hash"],
                    "issued_at": lease["issued_at"],
                    "renewed_at": lease["renewed_at"],
                    "expires_at": lease["expires_at"],
                    "prior_lease_ref": lease["prior_lease_revision_id"],
                    "content_hash": lease["content_hash"],
                }
                result = {
                    "status": "usable",
                    "lease": lease_wire,
                    "work_order": json.loads(work["work_order_json"]),
                    "work_order_hash": work["work_order_hash"],
                }
        if expired:
            raise LeaseExpired("scheduler lease expired before use")
        if result is None:
            raise SchedulerConflict("scheduler lease validation produced no result")
        return result

    def renew(
        self,
        work_order_id: str,
        attempt_number: int,
        owner_ref: str,
        lease_token: str,
        *,
        extend_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Append a bounded lease revision after owner/token verification."""
        work_order_id = _nonempty(work_order_id, "work_order_id")
        attempt_number = _positive_int(attempt_number, "attempt_number")
        owner_ref = _nonempty(owner_ref, "owner_ref")
        requested_seconds = (
            None
            if extend_seconds is None
            else _positive_seconds(extend_seconds, "extend_seconds")
        )
        now_dt = self._now()
        now = _timestamp(now_dt)
        expired = False
        exhausted = False
        result: dict[str, Any] | None = None
        with self._transaction() as cur:
            event, lease = self._active_lease(
                cur,
                work_order_id=work_order_id,
                attempt_number=attempt_number,
                owner_ref=owner_ref,
                lease_token=lease_token,
            )
            frozen = cur.execute(
                "SELECT p.policy_json FROM scheduler_work_orders w "
                "JOIN scheduler_policy_versions p ON p.policy_version_id=w.policy_version_id "
                "WHERE w.work_order_id=?",
                (work_order_id,),
            ).fetchone()
            if frozen is None:
                raise SchedulerConflict("leased work is missing its frozen scheduler policy")
            policy = json.loads(frozen["policy_json"])
            policy_max_renew = _positive_seconds(
                policy["max_renew_seconds"], "frozen max_renew_seconds"
            )
            policy_max_total = _positive_seconds(
                policy["max_total_lease_seconds"], "frozen max_total_lease_seconds"
            )
            seconds = policy_max_renew if requested_seconds is None else requested_seconds
            if seconds > policy_max_renew:
                raise LeaseRejected("requested renewal exceeds the work order's frozen policy")
            if _parse_time(lease["expires_at"]) <= now_dt:
                transition = self._expire_one(cur, event, lease, now)
                expired = True
                exhausted = transition["next"]["state"] == "failed"
            else:
                issued_at = _parse_time(lease["issued_at"])
                absolute_deadline = issued_at + timedelta(seconds=policy_max_total)
                new_expiry = min(
                    _parse_time(lease["expires_at"]) + timedelta(seconds=seconds),
                    absolute_deadline,
                )
                if new_expiry <= _parse_time(lease["expires_at"]):
                    raise LeaseRejected("lease reached its maximum total lifetime")
                revision_id = _id("lease-revision")
                wire = {
                    "schema_version": SCHEMA_VERSION,
                    "id": revision_id,
                    "lease_id": lease["lease_id"],
                    "lease_version": lease["lease_version"] + 1,
                    "created_at": now,
                    "work_order_ref": work_order_id,
                    "attempt_number": attempt_number,
                    "owner_ref": owner_ref,
                    "lease_token_hash": lease["lease_token_hash"],
                    "issued_at": lease["issued_at"],
                    "renewed_at": now,
                    "expires_at": _timestamp(new_expiry),
                    "prior_lease_ref": lease["lease_revision_id"],
                }
                wire["content_hash"] = content_hash(wire)
                cur.execute(
                    "INSERT INTO scheduler_leases "
                    "(lease_revision_id, lease_id, lease_version, work_order_id, attempt_number, "
                    "owner_ref, lease_token_hash, issued_at, renewed_at, expires_at, "
                    "prior_lease_revision_id, content_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        revision_id,
                        lease["lease_id"],
                        wire["lease_version"],
                        work_order_id,
                        attempt_number,
                        owner_ref,
                        lease["lease_token_hash"],
                        lease["issued_at"],
                        now,
                        wire["expires_at"],
                        lease["lease_revision_id"],
                        wire["content_hash"],
                        now,
                    ),
                )
                result = wire | {"lease_token": lease_token, "status": "renewed"}
        if expired:
            if exhausted:
                raise RetryExhausted("lease expired and retry policy is exhausted")
            raise LeaseExpired("lease expired before renewal")
        assert result is not None
        return result

    heartbeat = renew

    def complete(
        self,
        work_order_id: str,
        attempt_number: int,
        owner_ref: str,
        lease_token: str,
        result_envelope: ResultEnvelope | Mapping[str, Any],
        *,
        idempotency_key: str,
        result_envelope_hash: str | None = None,
        retry_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Commit one attempt outcome with fresh/duplicate/conflict semantics.

        ``ResultEnvelope.status`` is closed here to ``succeeded``,
        ``retryable`` or ``failed``.  Retryable/expired attempts receive a new
        ready attempt only while the trusted scheduler policy permits it.
        """
        work_order_id = _nonempty(work_order_id, "work_order_id")
        attempt_number = _positive_int(attempt_number, "attempt_number")
        owner_ref = _nonempty(owner_ref, "owner_ref")
        idempotency_key = _nonempty(idempotency_key, "idempotency_key")
        wire = self._result_wire(result_envelope)
        if wire["work_order_ref"] != work_order_id:
            raise SchedulerValidationError("ResultEnvelope.work_order_ref does not match")
        outcome = wire["status"]
        if outcome not in _RESULT_STATES:
            raise SchedulerValidationError(
                "ResultEnvelope.status must be succeeded, retryable, or failed"
            )
        retry_at_wire: str | None = None
        if retry_at is not None:
            if outcome != "retryable":
                raise SchedulerValidationError("retry_at is only valid for retryable results")
            retry_dt = _parse_time(retry_at) if isinstance(retry_at, str) else retry_at
            retry_at_wire = _timestamp(retry_dt)
        calculated_hash = content_hash(wire)
        if result_envelope_hash is not None and result_envelope_hash != calculated_hash:
            raise SchedulerValidationError("ResultEnvelope hash does not match canonical payload")
        request = {
            "work_order_id": work_order_id,
            "attempt_number": attempt_number,
            "owner_ref": owner_ref,
            "lease_token_hash": _token_hash(lease_token),
            "result_envelope_id": wire["id"],
            "result_envelope_hash": calculated_hash,
            "outcome": outcome,
            "retry_at": retry_at_wire,
        }
        request_hash = content_hash(request)
        now_dt = self._now()
        now = _timestamp(now_dt)
        expired = False
        exhausted = False
        response: dict[str, Any] | None = None
        with self._transaction() as cur:
            idem = cur.execute(
                "SELECT request_hash, result_json FROM scheduler_completion_idempotency "
                "WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if idem:
                if idem["request_hash"] == request_hash:
                    saved = json.loads(idem["result_json"])
                    saved["status"] = "duplicate"
                    return saved
                return {
                    "status": "conflict",
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "existing_request_hash": idem["request_hash"],
                }
            event, lease = self._active_lease(
                cur,
                work_order_id=work_order_id,
                attempt_number=attempt_number,
                owner_ref=owner_ref,
                lease_token=lease_token,
            )
            if _parse_time(lease["expires_at"]) <= now_dt:
                transition = self._expire_one(cur, event, lease, now)
                expired = True
                exhausted = transition["next"]["state"] == "failed"
            else:
                final_state = outcome
                receipt = {
                    "result_envelope_id": wire["id"],
                    "work_order_id": work_order_id,
                    "attempt_number": attempt_number,
                    "result_envelope_hash": calculated_hash,
                    "outcome": outcome,
                    "created_at": now,
                }
                receipt_hash = content_hash(receipt)
                try:
                    cur.execute(
                        "INSERT INTO scheduler_result_envelopes "
                        "(result_envelope_id, work_order_id, attempt_number, result_envelope_hash, "
                        "result_envelope_json, outcome, content_hash, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            wire["id"],
                            work_order_id,
                            attempt_number,
                            calculated_hash,
                            canonical_json(wire),
                            outcome,
                            receipt_hash,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise SchedulerConflict(
                        "ResultEnvelope id already identifies another accepted completion"
                    ) from exc
                attempt_event = self._append_event(
                    cur,
                    work_order_id=work_order_id,
                    attempt_number=attempt_number,
                    state=outcome,
                    now=now,
                    lease_revision_id=lease["lease_revision_id"],
                    result_envelope_id=wire["id"],
                    result_envelope_hash=calculated_hash,
                    reason="worker_completion",
                )
                next_event: dict[str, Any] | None = None
                if outcome == "retryable":
                    next_event = self._new_ready_or_exhausted(
                        cur,
                        work_order_id=work_order_id,
                        completed_attempt=attempt_number,
                        now=now,
                        exhaustion_reason="retry_exhausted_after_retryable_result",
                        retry_at=retry_at_wire,
                    )
                    final_state = next_event["state"]
                if outcome in {"succeeded", "failed"}:
                    result_record_id = _id("formal-result")
                    result_record = {
                        "id": result_record_id,
                        "work_order_id": work_order_id,
                        "attempt_number": attempt_number,
                        "result_envelope_id": wire["id"],
                        "result_envelope_hash": calculated_hash,
                        "terminal_state": "failed" if final_state == "failed" else outcome,
                        "created_at": now,
                    }
                    result_record_hash = content_hash(result_record)
                    try:
                        cur.execute(
                            "INSERT INTO scheduler_formal_results "
                            "(result_record_id, work_order_id, attempt_number, result_envelope_id, "
                            "result_envelope_hash, result_envelope_json, terminal_state, content_hash, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                result_record_id,
                                work_order_id,
                                attempt_number,
                                wire["id"],
                                calculated_hash,
                                canonical_json(wire),
                                result_record["terminal_state"],
                                result_record_hash,
                                now,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise SchedulerConflict(
                            "work order already has a formal result"
                        ) from exc
                response = {
                    "status": "fresh",
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "work_order_id": work_order_id,
                    "attempt_number": attempt_number,
                    "attempt_state": outcome,
                    "work_state": final_state,
                    "result_envelope_id": wire["id"],
                    "result_envelope_hash": calculated_hash,
                    "attempt_event": attempt_event,
                    "next_event": next_event,
                }
                cur.execute(
                    "INSERT INTO scheduler_completion_idempotency "
                    "(idempotency_key, request_hash, result_json, created_at) VALUES (?, ?, ?, ?)",
                    (idempotency_key, request_hash, canonical_json(response), now),
                )
        if expired:
            if exhausted:
                raise RetryExhausted("late completion rejected; retry policy is exhausted")
            raise LeaseExpired("late completion rejected after lease expiry")
        assert response is not None
        return response

    def _trusted_journal_completion_proof(
        self,
        *,
        work_order_id: str,
        attempt_number: int,
        owner_ref: str,
        lease_revision_ref: str,
        lease_hash: str,
        work_order_hash: str,
        result_wire: Mapping[str, Any],
        result_hash: str,
        retry_at: str | datetime | None,
    ) -> dict[str, str]:
        reader = self._trusted_journal_reader
        receipts_reader = self._trusted_completion_reader
        if reader is None or not all(
            callable(getattr(reader, name, None))
            for name in ("request", "latest", "event")
        ) or receipts_reader is None:
            raise SchedulerConflict(
                "journal reconciliation has no trusted journal reader"
            )
        metadata = result_wire.get("metadata")
        runner_request_ref = _nonempty(
            metadata.get("runner_request_ref")
            if isinstance(metadata, Mapping)
            else None,
            "result_envelope.metadata.runner_request_ref",
        )
        try:
            request = reader.request(runner_request_ref)
            parent_event = reader.latest(runner_request_ref)
        except Exception as exc:
            raise SchedulerConflict(
                "journal reconciliation lacks an immutable parent completion"
            ) from exc
        if not isinstance(request, Mapping) or not isinstance(parent_event, Mapping):
            raise SchedulerConflict("trusted journal reader returned an invalid record")
        payload = parent_event.get("payload")
        if (
            parent_event.get("runner_request_ref") != runner_request_ref
            or parent_event.get("state") != "responded"
            or not isinstance(payload, Mapping)
            or set(payload)
            != {
                "reservation_ref", "response", "result_envelope",
                "result_envelope_hash", "retry_at", "page_receipts",
                "commit_context",
            }
        ):
            raise SchedulerConflict(
                "trusted journal does not contain a closed parent completion"
            )
        response = payload.get("response")
        context = payload.get("commit_context")
        receipts = payload.get("page_receipts")
        if (
            not isinstance(response, Mapping)
            or not isinstance(context, Mapping)
            or not isinstance(receipts, list)
            or not receipts
            or payload.get("result_envelope") != result_wire
            or payload.get("result_envelope_hash") != result_hash
        ):
            raise SchedulerConflict(
                "journaled parent completion does not bind ResultEnvelope"
            )
        try:
            from .connector_runner import (
                validate_adapter_transport_observation,
                validate_connector_runner_request,
                validate_connector_runner_response,
            )

            request = validate_connector_runner_request(request)
            response = validate_connector_runner_response(response)
        except Exception as exc:
            raise SchedulerConflict(
                "journaled request/response wire is not closed"
            ) from exc
        context_request = context.get("request")
        if not isinstance(context_request, Mapping) or context_request != request:
            raise SchedulerConflict(
                "journaled completion context does not bind its RunnerRequest"
            )
        expected_retry_at = None
        if retry_at is not None:
            expected_retry_at = _timestamp(
                _parse_time(retry_at) if isinstance(retry_at, str) else retry_at
            )
        if (
            request.get("id") != runner_request_ref
            or request.get("work_order_ref") != work_order_id
            or request.get("work_order_hash") != work_order_hash
            or request.get("scheduler_attempt_number") != attempt_number
            or request.get("scheduler_lease_revision_ref") != lease_revision_ref
            or request.get("scheduler_lease_hash") != lease_hash
            or request.get("runner_actor_ref") != owner_ref
            or response.get("runner_request_ref") != runner_request_ref
            or response.get("runner_request_hash") != request.get("content_hash")
            or response.get("result_envelope_ref") != result_wire.get("id")
            or response.get("result_envelope_hash") != result_hash
            or response.get("outcome") != result_wire.get("status")
            or response.get("retry_at") != expected_retry_at
            or payload.get("retry_at") != expected_retry_at
        ):
            raise SchedulerConflict(
                "journaled completion differs from Scheduler lease/result authority"
            )
        context_fields = {
            "schema_version", "plan_hash", "request", "work_order",
            "work_order_hash", "invocation", "execution", "execution_hash",
            "profile", "call_spec",
        }
        invocation = context.get("invocation")
        execution = context.get("execution")
        profile = context.get("profile")
        call_spec = context.get("call_spec")
        if (
            set(context) != context_fields
            or not all(
                isinstance(item, Mapping)
                for item in (invocation, execution, profile, call_spec)
            )
            or context.get("work_order_hash") != work_order_hash
            or context.get("execution_hash") != request.get("execution_hash")
            or result_wire.get("invocation_ref") != execution.get("id")
        ):
            raise SchedulerConflict("journaled commit context is not closed")
        try:
            stored_invocation = receipts_reader.get_invocation(invocation["id"])
            stored_profile = receipts_reader.get_profile(profile["id"])
            stored_call = receipts_reader.get_call_spec(call_spec["id"])
            stored_execution = receipts_reader.get_execution(execution["id"])
        except Exception as exc:
            raise SchedulerConflict(
                "journaled commit context lacks immutable Connector authority"
            ) from exc
        if (
            stored_invocation != invocation
            or stored_profile != profile
            or stored_call != call_spec
            or stored_execution is None
            or stored_execution.get("execution") != execution
            or stored_execution.get("content_hash") != request.get("execution_hash")
            or request.get("connector_invocation_ref") != invocation.get("id")
            or request.get("connector_invocation_hash") != invocation.get("content_hash")
            or request.get("connector_profile_ref") != profile.get("id")
            or request.get("connector_profile_hash") != profile.get("content_hash")
            or request.get("call_spec_ref") != call_spec.get("id")
            or request.get("call_spec_hash") != call_spec.get("content_hash")
        ):
            raise SchedulerConflict(
                "journaled commit context differs from Connector authority"
            )
        receipt_fields = {
            "idempotency_status", "runner_request_ref", "runner_request_hash",
            "reservation_ref", "physical_attempt_ref", "physical_attempt_hash",
            "adapter_request_hash", "usage_entry_ref", "usage_entry_hash",
            "cost_entry_ref", "cost_entry_hash", "quota_settlement_ref",
            "quota_settlement_hash", "attempt_outcome", "retry_at",
            "observation", "raw_object", "error", "raw_artifact_version_ref",
            "raw_artifact_version_hash", "page_result_envelope",
            "page_result_envelope_hash", "completion_event_ref",
            "completion_event_hash", "completion_event_at",
        }
        validated_receipts: list[dict[str, Any]] = []
        prior_artifact_ref = None
        seen_requests: set[str] = set()
        seen_events: set[str] = set()
        for ordinal, raw_receipt in enumerate(receipts, start=1):
            if not isinstance(raw_receipt, Mapping) or set(raw_receipt) != receipt_fields:
                raise SchedulerConflict("journaled page receipt is not closed")
            receipt = dict(raw_receipt)
            expected_page_request = dict(request)
            if ordinal > 1:
                expected_page_request.pop("content_hash", None)
                expected_page_request["id"] = (
                    "connector-runner-request:"
                    + content_hash(
                        {
                            "kind": "connector-runner-request",
                            "idempotency_key": f"{request['id']}:page:{ordinal}",
                        }
                    )
                )
                expected_page_request["idempotency_key"] = (
                    f"{request['idempotency_key']}:page:{ordinal}"
                )
                expected_page_request["content_hash"] = content_hash(
                    expected_page_request
                )
            try:
                page_request = validate_connector_runner_request(
                    reader.request(receipt["runner_request_ref"])
                )
                completion_event = reader.event(receipt["completion_event_ref"])
                reservation = receipts_reader.get_reservation(
                    receipt["reservation_ref"]
                )
                attempt = receipts_reader.get_physical_attempt(
                    receipt["physical_attempt_ref"]
                )
                usage = receipts_reader.get_usage_entry(receipt["usage_entry_ref"])
                cost = receipts_reader.get_cost_entry(receipt["cost_entry_ref"])
                settlement = receipts_reader.get_quota_settlement(
                    receipt["quota_settlement_ref"]
                )
            except Exception as exc:
                raise SchedulerConflict(
                    "journaled page lacks immutable completion authority"
                ) from exc
            event_payload = completion_event.get("payload")
            event_context = (
                event_payload.get("commit_context")
                if isinstance(event_payload, Mapping)
                else None
            )
            event_request = (
                event_context.get("request")
                if isinstance(event_context, Mapping)
                else None
            )
            if (
                receipt["idempotency_status"] != "fresh"
                or page_request != expected_page_request
                or receipt["runner_request_ref"] in seen_requests
                or receipt["completion_event_ref"] in seen_events
                or receipt["runner_request_hash"] != page_request["content_hash"]
                or completion_event.get("runner_request_ref") != page_request["id"]
                or completion_event.get("content_hash")
                != receipt["completion_event_hash"]
                or completion_event.get("event_at") != receipt["completion_event_at"]
                or completion_event.get("state") not in {"observed", "transport_started"}
                or not isinstance(event_payload, Mapping)
                or not isinstance(event_context, Mapping)
                or set(event_context) != context_fields
                or event_request != page_request
                or any(
                    event_context.get(name) != context.get(name)
                    for name in context_fields - {"request"}
                )
                or not isinstance(reservation, Mapping)
                or not isinstance(attempt, Mapping)
                or not isinstance(usage, Mapping)
                or not isinstance(cost, Mapping)
                or not isinstance(settlement, Mapping)
                or event_payload.get("reservation_ref") != reservation.get("id")
                or event_payload.get("reservation_hash")
                != reservation.get("content_hash")
                or event_payload.get("physical_attempt_number") != ordinal
                or event_payload.get("adapter_request_hash")
                != receipt["adapter_request_hash"]
                or reservation.get("connector_invocation_ref") != invocation["id"]
                or reservation.get("physical_attempt_number") != ordinal
                or attempt.get("id") != receipt["physical_attempt_ref"]
                or attempt.get("content_hash") != receipt["physical_attempt_hash"]
                or attempt.get("connector_invocation_ref") != invocation["id"]
                or attempt.get("reservation_ref") != reservation.get("id")
                or attempt.get("physical_attempt_number") != ordinal
                or attempt.get("outcome") != receipt["attempt_outcome"]
                or usage.get("id") != receipt["usage_entry_ref"]
                or usage.get("content_hash") != receipt["usage_entry_hash"]
                or usage.get("physical_attempt_ref") != attempt.get("id")
                or cost.get("id") != receipt["cost_entry_ref"]
                or cost.get("content_hash") != receipt["cost_entry_hash"]
                or cost.get("usage_entry_ref") != usage.get("id")
                or settlement.get("id") != receipt["quota_settlement_ref"]
                or settlement.get("content_hash") != receipt["quota_settlement_hash"]
                or settlement.get("reservation_ref") != reservation.get("id")
                or settlement.get("usage_entry_ref") != usage.get("id")
                or settlement.get("cost_entry_ref") != cost.get("id")
            ):
                raise SchedulerConflict(
                    "journaled page receipt does not bind its authority chain"
                )
            seen_requests.add(receipt["runner_request_ref"])
            seen_events.add(receipt["completion_event_ref"])
            observation = receipt["observation"]
            if observation is not None:
                try:
                    observation = validate_adapter_transport_observation(observation)
                except Exception as exc:
                    raise SchedulerConflict(
                        "journaled page observation is not closed"
                    ) from exc
                if observation["request_hash"] != receipt["adapter_request_hash"]:
                    raise SchedulerConflict(
                        "journaled page observation does not bind AdapterRequest"
                    )
            if completion_event["state"] == "observed":
                observed_fields = {
                    "reservation_ref", "reservation_hash", "physical_attempt_number",
                    "adapter_request_hash", "started_at", "completed_at",
                    "attempt_outcome", "retry_at", "observation", "raw_object",
                    "error", "commit_context",
                }
                if (
                    set(event_payload) != observed_fields
                    or event_payload["attempt_outcome"] != receipt["attempt_outcome"]
                    or event_payload["retry_at"] != receipt["retry_at"]
                    or event_payload["observation"] != receipt["observation"]
                    or event_payload["raw_object"] != receipt["raw_object"]
                    or event_payload["error"] != receipt["error"]
                ):
                    raise SchedulerConflict(
                        "observed event does not bind the exact page receipt"
                    )
            else:
                transport_fields = {
                    "reservation_ref", "reservation_hash", "physical_attempt_number",
                    "adapter_request_hash", "started_at", "raw_sink_ref",
                    "prior_cursor", "commit_context",
                }
                if (
                    set(event_payload) != transport_fields
                    or receipt["attempt_outcome"] != "indeterminate"
                    or receipt["observation"] is not None
                    or receipt["raw_object"] is not None
                ):
                    raise SchedulerConflict(
                        "transport-started receipt is not conservative"
                    )
            successful = receipt["attempt_outcome"] == "succeeded"
            artifact_values = (
                receipt["raw_artifact_version_ref"],
                receipt["raw_artifact_version_hash"],
                receipt["page_result_envelope"],
                receipt["page_result_envelope_hash"],
            )
            if successful != all(item is not None for item in artifact_values):
                raise SchedulerConflict(
                    "journaled page success/artifact authority diverges"
                )
            if successful:
                raw_object = receipt["raw_object"]
                try:
                    page_result = self._result_wire(
                        receipt["page_result_envelope"]
                    )
                    artifact = receipts_reader.get_artifact_version(
                        receipt["raw_artifact_version_ref"]
                    )
                except Exception as exc:
                    raise SchedulerConflict(
                        "successful page lacks immutable artifact authority"
                    ) from exc
                if (
                    not isinstance(raw_object, Mapping)
                    or content_hash(page_result)
                    != receipt["page_result_envelope_hash"]
                    or page_result.get("status") != "succeeded"
                    or page_result.get("work_order_ref") != work_order_id
                    or page_result.get("invocation_ref") != execution["id"]
                    or artifact.get("content_hash")
                    != receipt["raw_artifact_version_hash"]
                    or artifact.get("artifact_content_hash")
                    != raw_object.get("content_hash")
                    or artifact.get("producer_execution_ref") != execution["id"]
                    or artifact.get("result_envelope_ref") != page_result["id"]
                    or artifact.get("result_envelope_hash")
                    != receipt["page_result_envelope_hash"]
                    or artifact.get("prior_version_ref") != prior_artifact_ref
                ):
                    raise SchedulerConflict(
                        "successful page artifact chain is not exact"
                    )
                prior_artifact_ref = receipt["raw_artifact_version_ref"]
            elif any(item is not None for item in artifact_values):
                raise SchedulerConflict("failed page fabricated artifact authority")
            validated_receipts.append(receipt)
        last_receipt = validated_receipts[-1]
        attempt_refs = [item["physical_attempt_ref"] for item in validated_receipts]
        settlement_refs = [
            item["quota_settlement_ref"] for item in validated_receipts
        ]
        usage_refs = [item["usage_entry_ref"] for item in validated_receipts]
        retained_artifacts = any(
            item["raw_artifact_version_ref"] is not None
            for item in validated_receipts
        )
        source_ref = response.get("source_envelope_ref")
        source = None
        if source_ref is not None:
            try:
                source = receipts_reader.get_source_envelope(source_ref)
            except Exception as exc:
                raise SchedulerConflict(
                    "parent completion lacks immutable SourceEnvelope"
                ) from exc
        expected_artifact_refs = (
            execution.get("output_refs", [])
            if result_wire["status"] == "succeeded" or retained_artifacts
            else []
        )
        if (
            payload.get("reservation_ref")
            != validated_receipts[0]["reservation_ref"]
            or response.get("physical_attempt_ref")
            != last_receipt["physical_attempt_ref"]
            or response.get("physical_attempt_hash")
            != last_receipt["physical_attempt_hash"]
            or response.get("usage_entry_ref") != last_receipt["usage_entry_ref"]
            or response.get("usage_entry_hash") != last_receipt["usage_entry_hash"]
            or response.get("cost_entry_ref") != last_receipt["cost_entry_ref"]
            or response.get("cost_entry_hash") != last_receipt["cost_entry_hash"]
            or response.get("quota_settlement_ref")
            != last_receipt["quota_settlement_ref"]
            or response.get("quota_settlement_hash")
            != last_receipt["quota_settlement_hash"]
            or result_wire.get("outputs", {}).get("connector_invocation_ref")
            != invocation["id"]
            or result_wire.get("outputs", {}).get("physical_attempt_refs")
            != attempt_refs
            or result_wire.get("outputs", {}).get("result_physical_attempt_ref")
            != last_receipt["physical_attempt_ref"]
            or result_wire.get("outputs", {}).get("quota_settlement_refs")
            != settlement_refs
            or result_wire.get("usage_refs") != usage_refs
            or result_wire.get("artifact_refs") != expected_artifact_refs
        ):
            raise SchedulerConflict(
                "parent Result/response does not bind the page authority chain"
            )
        if result_wire["status"] == "succeeded":
            expected_source_id = (
                "source-envelope:"
                + content_hash(
                    {
                        "kind": "source-envelope",
                        "idempotency_key": (
                            f"runner-shadow:{invocation['id']}:aggregate:source"
                        ),
                    }
                )
            )
            if (
                any(item["attempt_outcome"] != "succeeded" for item in validated_receipts)
                or not isinstance(source, Mapping)
                or source.get("id") != expected_source_id
                or source.get("content_hash") != response.get("source_envelope_hash")
                or source.get("physical_attempt_refs") != attempt_refs
                or source.get("result_physical_attempt_ref")
                != last_receipt["physical_attempt_ref"]
                or source.get("raw_artifact_version_ref")
                != last_receipt["raw_artifact_version_ref"]
                or response.get("raw_artifact_version_ref")
                != last_receipt["raw_artifact_version_ref"]
                or response.get("raw_artifact_version_hash")
                != last_receipt["raw_artifact_version_hash"]
                or result_wire.get("outputs", {}).get("source_envelope_ref")
                != source.get("id")
            ):
                raise SchedulerConflict(
                    "successful parent completion lacks exact source authority"
                )
        elif (
            last_receipt["attempt_outcome"] == "succeeded"
            or source is not None
            or response.get("raw_artifact_version_ref") is not None
            or result_wire.get("outputs", {}).get("source_envelope_ref") is not None
        ):
            raise SchedulerConflict(
                "non-success parent completion fabricated success authority"
            )
        expected_result = build_recorded_parent_result(
            request_ref=runner_request_ref,
            work_order_ref=work_order_id,
            execution_ref=execution["id"],
            execution_output_refs=execution.get("output_refs", []),
            invocation_ref=invocation["id"],
            receipts=validated_receipts,
            completion_recorded_at=completion_event["recorded_at"],
            source=source,
        )
        expected_result_hash = content_hash(expected_result)
        expected_response = build_recorded_parent_response(
            request=request,
            invocation=invocation,
            receipts=validated_receipts,
            result=expected_result,
            result_hash=expected_result_hash,
            source=source,
            retry_at=expected_retry_at,
        )
        if (
            result_wire != expected_result
            or result_hash != expected_result_hash
            or response != expected_response
        ):
            raise SchedulerConflict(
                "parent ResultEnvelope/RunnerResponse is not authority-derived"
            )
        completion_ref = _nonempty(
            last_receipt.get("completion_event_ref"), "completion_event_ref"
        )
        try:
            completion_event = reader.event(completion_ref)
            page_request = reader.request(last_receipt.get("runner_request_ref"))
        except Exception as exc:
            raise SchedulerConflict(
                "journaled completion lacks its immutable page event"
            ) from exc
        event_context = completion_event.get("payload", {}).get("commit_context")
        event_request = (
            event_context.get("request")
            if isinstance(event_context, Mapping)
            else None
        )
        if (
            not isinstance(page_request, Mapping)
            or event_request != page_request
            or completion_event.get("runner_request_ref")
            != last_receipt.get("runner_request_ref")
            or completion_event.get("state") not in {"transport_started", "observed"}
            or completion_event.get("content_hash")
            != last_receipt.get("completion_event_hash")
            or completion_event.get("event_at")
            != last_receipt.get("completion_event_at")
            or page_request.get("work_order_ref") != work_order_id
            or page_request.get("work_order_hash") != work_order_hash
            or page_request.get("scheduler_attempt_number") != attempt_number
            or page_request.get("scheduler_lease_revision_ref") != lease_revision_ref
            or page_request.get("scheduler_lease_hash") != lease_hash
            or page_request.get("runner_actor_ref") != owner_ref
            or page_request.get("connector_invocation_ref")
            != request.get("connector_invocation_ref")
        ):
            raise SchedulerConflict(
                "journaled completion page proof is not exact"
            )
        parent_recorded_at = _parse_time(parent_event.get("recorded_at"))
        completion_recorded_at = _parse_time(completion_event.get("recorded_at"))
        if parent_recorded_at < completion_recorded_at:
            raise SchedulerConflict(
                "parent completion predates its final page authority"
            )
        return {
            "runner_request_ref": runner_request_ref,
            "parent_event_ref": _nonempty(parent_event.get("id"), "parent_event_ref"),
            "parent_event_hash": _sha256(
                parent_event.get("content_hash"), "parent_event_hash"
            ),
            "completion_event_ref": completion_ref,
            "completion_event_hash": _sha256(
                completion_event.get("content_hash"), "completion_event_hash"
            ),
            "completion_event_recorded_at": _timestamp(completion_recorded_at),
            "parent_event_recorded_at": _timestamp(parent_recorded_at),
        }

    def reconcile_journaled_completion(
        self,
        work_order_id: str,
        attempt_number: int,
        owner_ref: str,
        result_envelope: ResultEnvelope | Mapping[str, Any],
        *,
        lease_revision_ref: str,
        lease_hash: str,
        work_order_hash: str,
        idempotency_key: str,
        result_envelope_hash: str,
        retry_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Accept a trusted durable completion proven to predate lease expiry.

        This is a narrow recovery path for a journal owned by another SQLite
        authority.  The Scheduler reads its constructor-bound trusted journal;
        callers cannot submit event hashes or timestamps as proof.  It never
        authorizes new work: a later attempt that has already been claimed or
        completed makes reconciliation fail closed.
        """

        work_order_id = _nonempty(work_order_id, "work_order_id")
        attempt_number = _positive_int(attempt_number, "attempt_number")
        owner_ref = _nonempty(owner_ref, "owner_ref")
        lease_revision_ref = _nonempty(lease_revision_ref, "lease_revision_ref")
        lease_hash = _sha256(lease_hash, "lease_hash")
        work_order_hash = _sha256(work_order_hash, "work_order_hash")
        idempotency_key = _nonempty(idempotency_key, "idempotency_key")
        wire = self._result_wire(result_envelope)
        calculated_hash = content_hash(wire)
        if (
            wire["work_order_ref"] != work_order_id
            or result_envelope_hash != calculated_hash
        ):
            raise SchedulerValidationError(
                "journaled ResultEnvelope authority does not match"
            )
        outcome = wire["status"]
        if outcome not in _RESULT_STATES:
            raise SchedulerValidationError("journaled result outcome is invalid")
        proof = self._trusted_journal_completion_proof(
            work_order_id=work_order_id,
            attempt_number=attempt_number,
            owner_ref=owner_ref,
            lease_revision_ref=lease_revision_ref,
            lease_hash=lease_hash,
            work_order_hash=work_order_hash,
            result_wire=wire,
            result_hash=calculated_hash,
            retry_at=retry_at,
        )
        journal_at = _parse_time(proof["parent_event_recorded_at"])
        retry_at_wire = None
        if retry_at is not None:
            if outcome != "retryable":
                raise SchedulerValidationError(
                    "retry_at is only valid for retryable results"
                )
            retry_at_wire = _timestamp(
                _parse_time(retry_at) if isinstance(retry_at, str) else retry_at
            )
        request = {
            "operation": "reconcile_journaled_completion",
            "work_order_id": work_order_id,
            "attempt_number": attempt_number,
            "owner_ref": owner_ref,
            "lease_revision_ref": lease_revision_ref,
            "lease_hash": lease_hash,
            "work_order_hash": work_order_hash,
            "runner_request_ref": proof["runner_request_ref"],
            "parent_journal_event_ref": proof["parent_event_ref"],
            "parent_journal_event_hash": proof["parent_event_hash"],
            "parent_journal_recorded_at": _timestamp(journal_at),
            "completion_event_ref": proof["completion_event_ref"],
            "completion_event_hash": proof["completion_event_hash"],
            "completion_event_recorded_at": proof["completion_event_recorded_at"],
            "result_envelope_id": wire["id"],
            "result_envelope_hash": calculated_hash,
            "outcome": outcome,
            "retry_at": retry_at_wire,
        }
        request_hash = content_hash(request)
        now = _timestamp(self._now())
        with self._transaction() as cur:
            idem = cur.execute(
                "SELECT request_hash,result_json FROM scheduler_completion_idempotency "
                "WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if idem is not None:
                if idem["request_hash"] != request_hash:
                    return {
                        "status": "conflict", "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "existing_request_hash": idem["request_hash"],
                    }
                saved = json.loads(idem["result_json"])
                saved["status"] = "duplicate"
                return saved
            work = cur.execute(
                "SELECT work_order_hash,max_attempts FROM scheduler_work_orders "
                "WHERE work_order_id=?",
                (work_order_id,),
            ).fetchone()
            lease = cur.execute(
                "SELECT * FROM scheduler_leases WHERE lease_revision_id=?",
                (lease_revision_ref,),
            ).fetchone()
            if (
                work is None or work["work_order_hash"] != work_order_hash
                or lease is None or lease["content_hash"] != lease_hash
                or lease["work_order_id"] != work_order_id
                or int(lease["attempt_number"]) != attempt_number
                or lease["owner_ref"] != owner_ref
            ):
                raise SchedulerConflict(
                    "journal reconciliation lease/work authority is invalid"
                )
            if not (
                _parse_time(lease["issued_at"])
                <= journal_at
                < _parse_time(lease["expires_at"])
            ):
                raise LeaseExpired(
                    "journaled completion was not durable within the lease"
                )
            current = self._latest_event(cur, work_order_id)
            if current is None:
                raise SchedulerConflict("journal reconciliation has no attempt history")
            current_attempt = int(current["attempt_number"])
            same_live_attempt = (
                current_attempt == attempt_number
                and current["state"] == "leased"
                and current["lease_revision_id"] == lease_revision_ref
            )
            expired_unclaimed = (
                current_attempt in {attempt_number, attempt_number + 1}
                and current["state"] in {"ready", "failed"}
                and cur.execute(
                    "SELECT 1 FROM scheduler_attempt_events WHERE work_order_id=? "
                    "AND attempt_number=? AND state='expired'",
                    (work_order_id, attempt_number),
                ).fetchone()
                is not None
            )
            reclaimed = cur.execute(
                "SELECT 1 FROM scheduler_attempt_events WHERE work_order_id=? "
                "AND attempt_number>? AND state='leased' LIMIT 1",
                (work_order_id, attempt_number),
            ).fetchone()
            if reclaimed is not None or (
                not same_live_attempt and not expired_unclaimed
            ):
                raise SchedulerConflict(
                    "a reclaimed or completed attempt blocks journal reconciliation"
                )
            if cur.execute(
                "SELECT 1 FROM scheduler_formal_results WHERE work_order_id=?",
                (work_order_id,),
            ).fetchone() is not None:
                raise SchedulerConflict(
                    "formal result already blocks journal reconciliation"
                )
            receipt = {
                "result_envelope_id": wire["id"], "work_order_id": work_order_id,
                "attempt_number": attempt_number,
                "result_envelope_hash": calculated_hash, "outcome": outcome,
                "created_at": now,
            }
            receipt_hash = content_hash(receipt)
            try:
                cur.execute(
                    "INSERT INTO scheduler_result_envelopes "
                    "(result_envelope_id,work_order_id,attempt_number,"
                    "result_envelope_hash,result_envelope_json,outcome,content_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        wire["id"], work_order_id, attempt_number, calculated_hash,
                        canonical_json(wire), outcome, receipt_hash, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise SchedulerConflict(
                    "journaled ResultEnvelope id already exists"
                ) from exc
            attempt_event = self._append_event(
                cur, work_order_id=work_order_id, attempt_number=attempt_number,
                state=outcome, now=now, lease_revision_id=lease_revision_ref,
                result_envelope_id=wire["id"],
                result_envelope_hash=calculated_hash,
                reason="trusted_journal_reconciliation",
            )
            next_event = None
            final_state = outcome
            if outcome == "retryable":
                if same_live_attempt:
                    next_event = self._new_ready_or_exhausted(
                        cur, work_order_id=work_order_id,
                        completed_attempt=attempt_number, now=now,
                        exhaustion_reason=(
                            "retry_exhausted_after_journaled_retryable_result"
                        ),
                        retry_at=retry_at_wire,
                    )
                else:
                    next_event = self._append_event(
                        cur, work_order_id=work_order_id,
                        attempt_number=current_attempt, state=current["state"],
                        now=now, reason="journal_reconciliation_restored_retry_state",
                        not_before=current["not_before"],
                    )
                final_state = next_event["state"]
            if outcome in {"succeeded", "failed"}:
                formal = {
                    "id": _id("formal-result"), "work_order_id": work_order_id,
                    "attempt_number": attempt_number,
                    "result_envelope_id": wire["id"],
                    "result_envelope_hash": calculated_hash,
                    "terminal_state": outcome, "created_at": now,
                }
                cur.execute(
                    "INSERT INTO scheduler_formal_results "
                    "(result_record_id,work_order_id,attempt_number,result_envelope_id,"
                    "result_envelope_hash,result_envelope_json,terminal_state,content_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        formal["id"], work_order_id, attempt_number, wire["id"],
                        calculated_hash, canonical_json(wire), outcome,
                        content_hash(formal), now,
                    ),
                )
            response = {
                "status": "fresh", "idempotency_key": idempotency_key,
                "request_hash": request_hash, "work_order_id": work_order_id,
                "attempt_number": attempt_number, "attempt_state": outcome,
                "work_state": final_state, "result_envelope_id": wire["id"],
                "result_envelope_hash": calculated_hash,
                "attempt_event": attempt_event, "next_event": next_event,
                "parent_journal_event_ref": proof["parent_event_ref"],
                "parent_journal_event_hash": proof["parent_event_hash"],
                "completion_event_ref": proof["completion_event_ref"],
                "completion_event_hash": proof["completion_event_hash"],
            }
            cur.execute(
                "INSERT INTO scheduler_completion_idempotency "
                "(idempotency_key,request_hash,result_json,created_at) VALUES(?,?,?,?)",
                (idempotency_key, request_hash, canonical_json(response), now),
            )
            return response

    def status(self, work_order_id: str) -> dict[str, Any]:
        work_order_id = _nonempty(work_order_id, "work_order_id")
        work = self.connection.execute(
            "SELECT * FROM scheduler_work_orders WHERE work_order_id=?", (work_order_id,)
        ).fetchone()
        if work is None:
            raise WorkNotFound(work_order_id)
        event = self.connection.execute(
            "SELECT * FROM scheduler_attempt_events WHERE work_order_id=? "
            "ORDER BY event_seq DESC LIMIT 1",
            (work_order_id,),
        ).fetchone()
        formal = self.connection.execute(
            "SELECT * FROM scheduler_formal_results WHERE work_order_id=?", (work_order_id,)
        ).fetchone()
        state = formal["terminal_state"] if formal else event["state"]
        return {
            "work_order_id": work_order_id,
            "work_order_hash": work["work_order_hash"],
            "policy_version_id": work["policy_version_id"],
            "max_attempts": work["max_attempts"],
            "attempt_number": event["attempt_number"],
            "not_before": event["not_before"],
            "state": state,
            "latest_event_id": event["event_id"],
            "formal_result_id": formal["result_record_id"] if formal else None,
        }

    def attempt_history(self, work_order_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM scheduler_attempt_events WHERE work_order_id=? ORDER BY event_seq",
            (_nonempty(work_order_id, "work_order_id"),),
        ).fetchall()
        return [dict(row) for row in rows]

    def lease_history(self, lease_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM scheduler_leases WHERE lease_id=? ORDER BY lease_version",
            (_nonempty(lease_id, "lease_id"),),
        ).fetchall()
        return [dict(row) for row in rows]

    def work_order_authority(self, work_order_id: str) -> dict[str, Any] | None:
        """Return the immutable Scheduler copy of one admitted WorkOrder."""
        row = self.connection.execute(
            "SELECT work_order_json,work_order_hash FROM scheduler_work_orders "
            "WHERE work_order_id=?",
            (_nonempty(work_order_id, "work_order_id"),),
        ).fetchone()
        if row is None:
            return None
        try:
            wire = json.loads(row["work_order_json"])
            normalized = WorkOrder.from_dict(wire).to_dict()
        except Exception as exc:
            raise SchedulerConflict("stored WorkOrder authority is invalid") from exc
        calculated = content_hash(normalized)
        if (
            canonical_json(normalized) != row["work_order_json"]
            or calculated != row["work_order_hash"]
        ):
            raise SchedulerConflict("stored WorkOrder authority drifted")
        return {
            "work_order": normalized,
            "work_order_hash": row["work_order_hash"],
        }

    def formal_result(self, work_order_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM scheduler_formal_results WHERE work_order_id=?",
            (_nonempty(work_order_id, "work_order_id"),),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["result_envelope"] = json.loads(result.pop("result_envelope_json"))
        return result


# Deliberately local aliases; parent integration may export them from
# dalton_core.__init__ after this isolated slice is accepted.
DurableScheduler = Scheduler
