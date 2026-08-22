"""Per-day hard spend cap and owner alerts for the paid thesis-impact lane.

This authority exists so that any future scheduled run of the thesis-impact
model WorkOrders is bounded by an immutable day cap before a broker call, and
so that fail-closed outcomes (cap exceeded, terminal WorkOrder failure) leave
durable decisions the owner can be alerted from.  It owns its own disposable
owner-only SQLite: no Research Ledger, Scheduler, or broker handle.

Reservations are per (work order, attempt, phase) and are settled to actual
accounted cost after the call.  An admission without a settlement keeps
counting its full reservation, so a crash between the paid call and the
settlement stays conservative instead of silently freeing budget.  Rejections
are durable append-only decisions: the same admission identity can never be
admitted after it was rejected.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
ALERT_KINDS = frozenset({"day_budget_exceeded", "work_order_failed"})
ALERT_SEVERITIES = frozenset({"high", "medium"})
ALERT_MAX_DELIVERY_ATTEMPTS = 5
_SCHEMA_PATH = Path(__file__).with_name("thesis_impact_budget_schema.sql")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ThesisImpactBudgetError(RuntimeError):
    """Base error for the day-budget and alert authority."""


class ThesisImpactBudgetValidationError(ThesisImpactBudgetError):
    """A closed contract field or argument is invalid."""


class ThesisImpactDayBudgetExceeded(ThesisImpactBudgetError):
    """The exact admission would exceed the immutable day cap."""

    def __init__(self, rejection: Mapping[str, Any]) -> None:
        super().__init__(
            "thesis-impact day budget exceeded: committed "
            f"{rejection['day_committed_micros']} + reserved "
            f"{rejection['reserved_micros']} > cap {rejection['day_cap_micros']}"
        )
        self.rejection = dict(rejection)


class ThesisImpactBudgetConflict(ThesisImpactBudgetError):
    """An append-only record was reused with different semantics."""


def _utc(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ThesisImpactBudgetValidationError("timestamps must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _micros(value: Any, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ThesisImpactBudgetValidationError(f"{name} must be integer micros")
    if value < 0 or (positive and value <= 0):
        raise ThesisImpactBudgetValidationError(f"{name} is outside the admitted range")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ThesisImpactBudgetValidationError(f"{name} must be non-empty text")
    return value


def _day(value: Any) -> str:
    if not isinstance(value, str) or not _DAY_RE.fullmatch(value):
        raise ThesisImpactBudgetValidationError("day must be an exact YYYY-MM-DD date")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ThesisImpactBudgetValidationError("day is not a real date") from exc
    return value


class ThesisImpactBudgetStore:
    """Owner-only day-budget admission and alert authority."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            target = Path(self.path)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        if self.path != ":memory:":
            self.connection.execute("PRAGMA journal_mode=WAL")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ThesisImpactBudgetStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self.connection.in_transaction:
            raise ThesisImpactBudgetError("nested budget transaction")
        self.connection.execute("BEGIN IMMEDIATE")
        cur = self.connection.cursor()
        try:
            yield cur
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            cur.close()

    def register_policy(
        self,
        *,
        policy_version_id: str,
        day_cap_micros: int,
        prior_version_id: str | None = None,
    ) -> dict[str, Any]:
        policy_version_id = _text(policy_version_id, "policy_version_id")
        day_cap_micros = _micros(day_cap_micros, "day_cap_micros", positive=True)
        if prior_version_id is not None:
            prior_version_id = _text(prior_version_id, "prior_version_id")
        wire = {
            "schema_version": SCHEMA_VERSION,
            "policy_version_id": policy_version_id,
            "day_cap_micros": day_cap_micros,
            "currency": "USD",
            "prior_version_id": prior_version_id,
            "created_at": _utc(self.clock()),
        }
        wire["content_hash"] = content_hash(wire)
        with self._transaction() as cur:
            existing = cur.execute(
                "SELECT record_json FROM thesis_impact_budget_policies "
                "WHERE policy_version_id=?",
                (policy_version_id,),
            ).fetchone()
            if existing is not None:
                if existing["record_json"] != canonical_json(wire):
                    raise ThesisImpactBudgetConflict(
                        "budget policy identity was reused with different semantics"
                    )
                return {**wire, "status": "duplicate"}
            if prior_version_id is not None and cur.execute(
                "SELECT 1 FROM thesis_impact_budget_policies WHERE policy_version_id=?",
                (prior_version_id,),
            ).fetchone() is None:
                raise ThesisImpactBudgetConflict(
                    "budget policy prior version is not registered"
                )
            cur.execute(
                "INSERT INTO thesis_impact_budget_policies("
                "policy_version_id,day_cap_micros,currency,prior_version_id,"
                "record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    policy_version_id,
                    day_cap_micros,
                    "USD",
                    prior_version_id,
                    canonical_json(wire),
                    wire["content_hash"],
                    wire["created_at"],
                ),
            )
        return {**wire, "status": "fresh"}

    def policy(self, policy_version_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT record_json FROM thesis_impact_budget_policies "
            "WHERE policy_version_id=?",
            (_text(policy_version_id, "policy_version_id"),),
        ).fetchone()
        if row is None:
            raise ThesisImpactBudgetConflict("budget policy is not registered")
        return json.loads(row["record_json"])

    @staticmethod
    def _day_committed(cur: sqlite3.Cursor, policy_version_id: str, day: str) -> int:
        settled = cur.execute(
            "SELECT COALESCE(SUM(s.actual_micros),0) AS total "
            "FROM thesis_impact_day_settlements s "
            "JOIN thesis_impact_day_admissions a ON a.admission_id=s.admission_id "
            "WHERE a.policy_version_id=? AND a.day=?",
            (policy_version_id, day),
        ).fetchone()["total"]
        open_reserved = cur.execute(
            "SELECT COALESCE(SUM(a.reserved_micros),0) AS total "
            "FROM thesis_impact_day_admissions a "
            "WHERE a.policy_version_id=? AND a.day=? AND NOT EXISTS ("
            " SELECT 1 FROM thesis_impact_day_settlements s "
            " WHERE s.admission_id=a.admission_id)",
            (policy_version_id, day),
        ).fetchone()["total"]
        return int(settled) + int(open_reserved)

    def day_summary(self, *, policy_version_id: str, day: str) -> dict[str, Any]:
        policy = self.policy(policy_version_id)
        day = _day(day)
        cur = self.connection.cursor()
        try:
            committed = self._day_committed(cur, policy_version_id, day)
        finally:
            cur.close()
        return {
            "schema_version": SCHEMA_VERSION,
            "policy_version_id": policy_version_id,
            "day": day,
            "day_cap_micros": policy["day_cap_micros"],
            "committed_micros": committed,
            "remaining_micros": policy["day_cap_micros"] - committed,
        }

    def admit(
        self,
        *,
        policy_version_id: str,
        day: str,
        work_order_ref: str,
        attempt_number: int,
        phase: str,
        route_decision_ref: str,
        reserved_micros: int,
    ) -> dict[str, Any]:
        """Reserve against the day cap or persist a durable rejection.

        The rejection row commits in the same transaction that observed the
        over-cap committed total; the exception is raised only after that
        decision is durable.
        """

        policy = self.policy(policy_version_id)
        day = _day(day)
        work_order_ref = _text(work_order_ref, "work_order_ref")
        route_decision_ref = _text(route_decision_ref, "route_decision_ref")
        reserved_micros = _micros(reserved_micros, "reserved_micros", positive=True)
        if (
            isinstance(attempt_number, bool)
            or not isinstance(attempt_number, int)
            or attempt_number < 1
        ):
            raise ThesisImpactBudgetValidationError(
                "attempt_number must be a positive integer"
            )
        if phase not in {"assessment", "verification"}:
            raise ThesisImpactBudgetValidationError("phase is not admitted")
        identity = {
            "work_order_ref": work_order_ref,
            "attempt_number": attempt_number,
            "phase": phase,
        }
        wire = {
            "schema_version": SCHEMA_VERSION,
            "admission_id": "thesis-impact-admission:" + content_hash(identity)[:32],
            "policy_version_id": policy_version_id,
            "day": day,
            **identity,
            "route_decision_ref": route_decision_ref,
            "reserved_micros": reserved_micros,
            "created_at": _utc(self.clock()),
        }
        wire["content_hash"] = content_hash(wire)
        rejection: dict[str, Any] | None = None
        with self._transaction() as cur:
            existing = cur.execute(
                "SELECT record_json FROM thesis_impact_day_admissions "
                "WHERE work_order_ref=? AND attempt_number=? AND phase=?",
                (work_order_ref, attempt_number, phase),
            ).fetchone()
            if existing is not None:
                if existing["record_json"] != canonical_json(wire):
                    raise ThesisImpactBudgetConflict(
                        "admission identity was reused with different semantics"
                    )
                return {**wire, "status": "duplicate"}
            prior_row = cur.execute(
                "SELECT record_json FROM thesis_impact_day_rejections "
                "WHERE work_order_ref=? AND attempt_number=? AND phase=?",
                (work_order_ref, attempt_number, phase),
            ).fetchone()
            committed = self._day_committed(cur, policy_version_id, day)
            if committed + reserved_micros > policy["day_cap_micros"]:
                rejection = (
                    json.loads(prior_row["record_json"])
                    if prior_row is not None
                    else {
                        "schema_version": SCHEMA_VERSION,
                        "rejection_id": "thesis-impact-rejection:"
                        + content_hash(identity)[:32],
                        "policy_version_id": policy_version_id,
                        "day": day,
                        **identity,
                        "route_decision_ref": route_decision_ref,
                        "reserved_micros": reserved_micros,
                        "day_committed_micros": committed,
                        "day_cap_micros": policy["day_cap_micros"],
                        "created_at": _utc(self.clock()),
                    }
                )
                if prior_row is None:
                    rejection["content_hash"] = content_hash(rejection)
                    cur.execute(
                        "INSERT INTO thesis_impact_day_rejections("
                        "rejection_id,policy_version_id,day,work_order_ref,"
                        "attempt_number,phase,route_decision_ref,reserved_micros,"
                        "day_committed_micros,day_cap_micros,record_json,"
                        "content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            rejection["rejection_id"],
                            policy_version_id,
                            day,
                            work_order_ref,
                            attempt_number,
                            phase,
                            route_decision_ref,
                            reserved_micros,
                            committed,
                            policy["day_cap_micros"],
                            canonical_json(rejection),
                            rejection["content_hash"],
                            rejection["created_at"],
                        ),
                    )
            elif prior_row is not None:
                raise ThesisImpactBudgetConflict(
                    "a rejected admission cannot later be admitted"
                )
            else:
                cur.execute(
                    "INSERT INTO thesis_impact_day_admissions("
                    "admission_id,policy_version_id,day,work_order_ref,attempt_number,"
                    "phase,route_decision_ref,reserved_micros,record_json,content_hash,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        wire["admission_id"],
                        policy_version_id,
                        day,
                        work_order_ref,
                        attempt_number,
                        phase,
                        route_decision_ref,
                        reserved_micros,
                        canonical_json(wire),
                        wire["content_hash"],
                        wire["created_at"],
                    ),
                )
        if rejection is not None:
            raise ThesisImpactDayBudgetExceeded(rejection)
        return {**wire, "status": "fresh"}

    def settle(
        self,
        admission_id: str,
        *,
        actual_micros: int,
        usage_entry_ref: str | None = None,
    ) -> dict[str, Any]:
        """Bind one admission to its actual accounted cost (idempotent)."""

        admission_id = _text(admission_id, "admission_id")
        actual_micros = _micros(actual_micros, "actual_micros")
        if usage_entry_ref is not None:
            usage_entry_ref = _text(usage_entry_ref, "usage_entry_ref")
        wire = {
            "schema_version": SCHEMA_VERSION,
            "settlement_id": "thesis-impact-settlement:"
            + content_hash({"admission_id": admission_id})[:32],
            "admission_id": admission_id,
            "actual_micros": actual_micros,
            "usage_entry_ref": usage_entry_ref,
            "created_at": _utc(self.clock()),
        }
        wire["content_hash"] = content_hash(wire)
        with self._transaction() as cur:
            if cur.execute(
                "SELECT 1 FROM thesis_impact_day_admissions WHERE admission_id=?",
                (admission_id,),
            ).fetchone() is None:
                raise ThesisImpactBudgetConflict("settlement references no admission")
            existing = cur.execute(
                "SELECT record_json FROM thesis_impact_day_settlements "
                "WHERE admission_id=?",
                (admission_id,),
            ).fetchone()
            if existing is not None:
                if existing["record_json"] != canonical_json(wire):
                    raise ThesisImpactBudgetConflict(
                        "admission was already settled with different semantics"
                    )
                return {**wire, "status": "duplicate"}
            cur.execute(
                "INSERT INTO thesis_impact_day_settlements("
                "settlement_id,admission_id,actual_micros,usage_entry_ref,"
                "record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    wire["settlement_id"],
                    admission_id,
                    actual_micros,
                    usage_entry_ref,
                    canonical_json(wire),
                    wire["content_hash"],
                    wire["created_at"],
                ),
            )
        return {**wire, "status": "fresh"}

    def record_alert(
        self,
        *,
        alert_id: str,
        kind: str,
        severity: str,
        work_order_ref: str | None = None,
        phase: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one owner alert and its pending delivery event (idempotent)."""

        alert_id = _text(alert_id, "alert_id")
        if kind not in ALERT_KINDS:
            raise ThesisImpactBudgetValidationError("alert kind is not admitted")
        if severity not in ALERT_SEVERITIES:
            raise ThesisImpactBudgetValidationError("alert severity is not admitted")
        if work_order_ref is not None:
            work_order_ref = _text(work_order_ref, "work_order_ref")
        if phase is not None and phase not in {"assessment", "verification"}:
            raise ThesisImpactBudgetValidationError("alert phase is not admitted")
        detail_json = canonical_json(dict(detail or {}))
        row = self.connection.execute(
            "SELECT detail_json FROM thesis_impact_alerts WHERE alert_id=?",
            (alert_id,),
        ).fetchone()
        if row is not None:
            if row["detail_json"] != detail_json:
                raise ThesisImpactBudgetConflict(
                    "alert identity was reused with different semantics"
                )
            return {"alert_id": alert_id, "status": "duplicate"}
        created_at = _utc(self.clock())
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO thesis_impact_alerts("
                "alert_id,kind,severity,work_order_ref,phase,detail_json,created_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (alert_id, kind, severity, work_order_ref, phase, detail_json, created_at),
            )
            cur.execute(
                "INSERT INTO thesis_impact_alert_events("
                "event_id,alert_id,state,actor_ref,created_at) VALUES(?,?,?,?,?)",
                (
                    "thesis-impact-alert-event:"
                    + content_hash({"alert_id": alert_id, "state": "pending"})[:32],
                    alert_id,
                    "pending",
                    "system:thesis-impact-budget",
                    created_at,
                ),
            )
        return {"alert_id": alert_id, "status": "fresh"}

    def pending_alerts(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ThesisImpactBudgetValidationError("limit must be 1..1000")
        rows = self.connection.execute(
            "SELECT a.*,e.state,e.claim_expires_at,e.endpoint_ref,e.error_code "
            "FROM thesis_impact_alerts a JOIN thesis_impact_alert_events e "
            "ON e.event_seq=(SELECT MAX(x.event_seq) FROM thesis_impact_alert_events x "
            "WHERE x.alert_id=a.alert_id) "
            "WHERE e.state IN ('pending','claimed','failed') "
            "ORDER BY a.created_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail_json"])} for row in rows]

    def claim_alerts(
        self,
        *,
        endpoint_ref: str,
        actor_ref: str,
        claim_ttl_seconds: int = 120,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        endpoint_ref = _text(endpoint_ref, "endpoint_ref")
        actor_ref = _text(actor_ref, "actor_ref")
        for value, name, upper in (
            (claim_ttl_seconds, "claim_ttl_seconds", 3600),
            (limit, "limit", 100),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
                raise ThesisImpactBudgetValidationError(f"{name} must be 1..{upper}")
        now = _utc(self.clock())
        rows = self.connection.execute(
            "SELECT a.alert_id, "
            "(SELECT COUNT(*) FROM thesis_impact_alert_events c "
            " WHERE c.alert_id=a.alert_id AND c.state='claimed') AS attempt_count "
            "FROM thesis_impact_alerts a JOIN thesis_impact_alert_events e "
            "ON e.event_seq=(SELECT MAX(x.event_seq) FROM thesis_impact_alert_events x "
            "WHERE x.alert_id=a.alert_id) "
            "WHERE (SELECT COUNT(*) FROM thesis_impact_alert_events c "
            " WHERE c.alert_id=a.alert_id AND c.state='claimed')<? AND ("
            "e.state='pending' OR e.state='failed' OR "
            "(e.state='claimed' AND e.claim_expires_at IS NOT NULL "
            " AND e.claim_expires_at<=?)) "
            "ORDER BY a.created_at LIMIT ?",
            (ALERT_MAX_DELIVERY_ATTEMPTS, now, limit),
        ).fetchall()
        expires = (
            datetime.fromisoformat(now) + timedelta(seconds=claim_ttl_seconds)
        ).isoformat(timespec="microseconds")
        claims: list[dict[str, Any]] = []
        with self._transaction() as cur:
            for row in rows:
                alert_id = row["alert_id"]
                attempt_number = int(row["attempt_count"]) + 1
                cur.execute(
                    "INSERT INTO thesis_impact_alert_events("
                    "event_id,alert_id,state,claim_expires_at,endpoint_ref,actor_ref,"
                    "created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        "thesis-impact-alert-event:"
                        + content_hash({
                            "alert_id": alert_id,
                            "state": "claimed",
                            "now": now,
                            "endpoint_ref": endpoint_ref,
                            "attempt_number": attempt_number,
                        })[:32],
                        alert_id,
                        "claimed",
                        expires,
                        endpoint_ref,
                        actor_ref,
                        now,
                    ),
                )
                claims.append({
                    "alert_id": alert_id,
                    "attempt_number": attempt_number,
                    "claim_expires_at": expires,
                })
        return claims

    def record_alert_delivery(
        self,
        alert_id: str,
        *,
        state: str,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        if state not in {"delivered", "failed"}:
            raise ThesisImpactBudgetValidationError("delivery state is not admitted")
        if error_code is not None:
            error_code = _text(error_code, "error_code")
        _text(alert_id, "alert_id")
        created_at = _utc(self.clock())
        with self._transaction() as cur:
            if cur.execute(
                "SELECT 1 FROM thesis_impact_alerts WHERE alert_id=?", (alert_id,)
            ).fetchone() is None:
                raise ThesisImpactBudgetConflict("delivery references no alert")
            prior_events = int(cur.execute(
                "SELECT COUNT(*) FROM thesis_impact_alert_events "
                "WHERE alert_id=? AND state IN ('delivered','failed')",
                (alert_id,),
            ).fetchone()[0])
            cur.execute(
                "INSERT INTO thesis_impact_alert_events("
                "event_id,alert_id,state,error_code,actor_ref,created_at"
                ") VALUES(?,?,?,?,?,?)",
                (
                    "thesis-impact-alert-event:"
                    + content_hash({
                        "alert_id": alert_id,
                        "state": state,
                        "now": created_at,
                        "attempt_number": prior_events + 1,
                    })[:32],
                    alert_id,
                    state,
                    error_code,
                    "system:thesis-impact-budget",
                    created_at,
                ),
            )
        return {"alert_id": alert_id, "state": state}


__all__ = [
    "ALERT_KINDS",
    "ALERT_MAX_DELIVERY_ATTEMPTS",
    "ALERT_SEVERITIES",
    "SCHEMA_VERSION",
    "ThesisImpactBudgetConflict",
    "ThesisImpactBudgetError",
    "ThesisImpactBudgetStore",
    "ThesisImpactBudgetValidationError",
    "ThesisImpactDayBudgetExceeded",
]
