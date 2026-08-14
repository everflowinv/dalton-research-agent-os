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
    ) -> dict[str, Any]:
        if state not in _ATTEMPT_STATES:
            raise SchedulerValidationError(f"invalid attempt state: {state!r}")
        prior = self._latest_event(cur, work_order_id)
        event_id = _id("attempt-event")
        wire = {
            "schema_version": SCHEMA_VERSION,
            "id": event_id,
            "created_at": now,
            "work_order_ref": work_order_id,
            "attempt_number": attempt_number,
            "state": state,
            "lease_ref": lease_revision_id,
            "result_envelope_ref": result_envelope_id,
            "result_envelope_hash": result_envelope_hash,
            "reason": reason,
            "prior_event_ref": prior["event_id"] if prior else None,
        }
        wire["content_hash"] = content_hash(wire)
        cur.execute(
            "INSERT INTO scheduler_attempt_events "
            "(event_id, work_order_id, attempt_number, state, lease_revision_id, "
            "result_envelope_id, result_envelope_hash, reason, prior_event_id, content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                work_order_id,
                attempt_number,
                state,
                lease_revision_id,
                result_envelope_id,
                result_envelope_hash,
                reason,
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
                "AND NOT EXISTS (SELECT 1 FROM scheduler_formal_results r "
                "                WHERE r.work_order_id=e.work_order_id) "
            )
            params: tuple[Any, ...] = ()
            if work_order_id is not None:
                query += "AND e.work_order_id=? "
                params = (work_order_id,)
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
