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
import os
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
        # P13n: say which cap refused it. Three different caps can raise this --
        # the day policy, the mission budget, the outer cascade -- and the
        # message only ever printed the day policy's numbers. A planner call
        # refused by a $5 mission cap reported "5684316 > 25000000", which is
        # arithmetic that does not support its own conclusion and sends the
        # reader looking in the wrong place.
        reason = rejection.get("reason") or "day_budget_exceeded"
        detail = (
            f"committed {rejection['day_committed_micros']} + reserved "
            f"{rejection['reserved_micros']} against day cap "
            f"{rejection['day_cap_micros']}"
        )
        binding = rejection.get("mission_binding")
        if isinstance(binding, Mapping) and binding.get("mission_ref"):
            detail += f"; mission {binding['mission_ref']}"
        super().__init__(f"thesis-impact budget refused the call ({reason}): {detail}")
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
        read_only: bool = False,
    ) -> None:
        self.path = str(path)
        self.read_only = read_only
        if not read_only and self.path != ":memory:":
            target = Path(self.path)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.touch(mode=0o600, exist_ok=True)
            os.chmod(target, 0o600)
        from .readonly_sqlite import connect_read_only
        self.connection = (connect_read_only(path) if read_only else
                           sqlite3.connect(self.path, isolation_level=None))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        if not read_only:
            self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        if not read_only and self.path != ":memory:":
            self.connection.execute("PRAGMA journal_mode=WAL")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def memory_snapshot(self) -> "ThesisImpactBudgetStore":
        """Disposable exact backup; never migrate the copied authority."""
        snapshot = ThesisImpactBudgetStore(clock=self.clock)
        try:
            self.connection.backup(snapshot.connection)
            snapshot.connection.execute("PRAGMA temp_store=MEMORY")
        except BaseException:
            snapshot.close()
            raise
        return snapshot

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ThesisImpactBudgetStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self.read_only:
            raise sqlite3.OperationalError("ThesisImpactBudgetStore is read_only")
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
                persisted = json.loads(existing["record_json"])
                comparable_fields = (
                    "schema_version", "policy_version_id", "day_cap_micros",
                    "currency", "prior_version_id",
                )
                if any(persisted[field] != wire[field] for field in comparable_fields):
                    raise ThesisImpactBudgetConflict(
                        "budget policy identity was reused with different semantics"
                    )
                return {**persisted, "status": "duplicate"}
            if prior_version_id is not None and cur.execute(
                "SELECT 1 FROM thesis_impact_budget_policies WHERE policy_version_id=?",
                (prior_version_id,),
            ).fetchone() is None:
                raise ThesisImpactBudgetConflict(
                    "budget policy prior version is not registered"
                )
            if prior_version_id is not None and cur.execute(
                "SELECT 1 FROM thesis_impact_budget_policies WHERE prior_version_id=?",
                (prior_version_id,),
            ).fetchone() is not None:
                raise ThesisImpactBudgetConflict(
                    "budget policy chain already advanced from the prior version"
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
        chain_rows = cur.execute(
            "WITH RECURSIVE policy_chain(policy_version_id) AS ("
            " SELECT ? UNION ALL "
            " SELECT p.prior_version_id FROM thesis_impact_budget_policies p "
            " JOIN policy_chain c ON p.policy_version_id=c.policy_version_id "
            " WHERE p.prior_version_id IS NOT NULL) "
            "SELECT policy_version_id FROM policy_chain",
            (policy_version_id,),
        ).fetchall()
        policy_ids = tuple(row["policy_version_id"] for row in chain_rows)
        placeholders = ",".join("?" for _ in policy_ids)
        settled = cur.execute(
            "SELECT COALESCE(SUM(s.actual_micros),0) AS total "
            "FROM thesis_impact_day_settlements s "
            "JOIN thesis_impact_day_admissions a ON a.admission_id=s.admission_id "
            f"WHERE a.policy_version_id IN ({placeholders}) AND a.day=?",
            (*policy_ids, day),
        ).fetchone()["total"]
        open_reserved = cur.execute(
            "SELECT COALESCE(SUM(a.reserved_micros),0) AS total "
            "FROM thesis_impact_day_admissions a "
            f"WHERE a.policy_version_id IN ({placeholders}) AND NOT EXISTS ("
            " SELECT 1 FROM thesis_impact_day_settlements s "
            " WHERE s.admission_id=a.admission_id)",
            policy_ids,
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
        mission_binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reserve against the day cap or persist a durable rejection.

        The rejection row commits in the same transaction that observed the
        over-cap committed total; the exception is raised only after that
        decision is durable.
        """

        if mission_binding is not None:
            mission_binding = dict(mission_binding)
            fields = {"mission_ref", "mission_version_ref", "mission_version_hash", "max_daily_paid_calls", "max_daily_cost_micros"}
            if not fields <= set(mission_binding) or set(mission_binding) - fields - {"outer_budget"}:
                raise ThesisImpactBudgetValidationError("invalid mission budget binding")
            for key in ("mission_ref", "mission_version_ref", "mission_version_hash"):
                _text(mission_binding[key], key)
            for key in ("max_daily_paid_calls", "max_daily_cost_micros"):
                _micros(mission_binding[key], key)
            outer = mission_binding.get("outer_budget")
            if outer is not None:
                ref_fields = {"mandate_ref", "mandate_version_ref", "mandate_version_hash", "governance_policy_ref", "governance_policy_version_ref", "governance_policy_version_hash"}
                if not isinstance(outer, Mapping) or set(outer) != ref_fields | {"max_daily_paid_calls", "max_daily_cost_micros"}:
                    raise ThesisImpactBudgetValidationError("invalid outer research budget binding")
                for key in ref_fields:
                    _text(outer[key], key)
                for key in ("max_daily_paid_calls", "max_daily_cost_micros"):
                    _micros(outer[key], key)
                    if mission_binding[key] > outer[key]:
                        raise ThesisImpactBudgetValidationError("mission budget exceeds bound outer authority")
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
                persisted = json.loads(existing["record_json"])
                comparable_fields = (
                    "schema_version", "admission_id", "policy_version_id", "day",
                    "work_order_ref", "attempt_number", "phase",
                    "route_decision_ref", "reserved_micros",
                )
                if any(persisted[field] != wire[field] for field in comparable_fields):
                    raise ThesisImpactBudgetConflict(
                        "admission identity was reused with different semantics"
                    )
                binding_row = cur.execute("SELECT record_json FROM model_mission_budget_bindings WHERE admission_id=?", (persisted["admission_id"],)).fetchone()
                saved_binding = None if binding_row is None else json.loads(binding_row["record_json"])
                if saved_binding != mission_binding:
                    raise ThesisImpactBudgetConflict("mission budget binding changed on replay")
                return {**persisted, "status": "duplicate"}
            prior_row = cur.execute(
                "SELECT record_json FROM thesis_impact_day_rejections "
                "WHERE work_order_ref=? AND attempt_number=? AND phase=?",
                (work_order_ref, attempt_number, phase),
            ).fetchone()
            if prior_row is not None:
                persisted = json.loads(prior_row["record_json"])
                comparable_fields = (
                    "schema_version", "policy_version_id", "day", "work_order_ref",
                    "attempt_number", "phase", "route_decision_ref", "reserved_micros",
                )
                if any(persisted[field] != wire[field] for field in comparable_fields):
                    raise ThesisImpactBudgetConflict(
                        "rejection identity was reused with different semantics"
                    )
                raise ThesisImpactBudgetConflict(
                    "a rejected admission cannot later be admitted"
                )
            else:
                if cur.execute(
                    "SELECT 1 FROM thesis_impact_budget_policies WHERE prior_version_id=?",
                    (policy_version_id,),
                ).fetchone() is not None:
                    raise ThesisImpactBudgetConflict(
                        "budget policy was superseded and cannot admit new spend"
                    )
                # A provider overrun has already been recorded in the existing
                # alert authority. Do not free the short reservation or admit
                # another call while owner reconciliation is outstanding.
                alerts = cur.execute("SELECT detail_json FROM thesis_impact_alerts WHERE kind='work_order_failed'").fetchall()
                if any(json.loads(a["detail_json"]).get("reason") == "model_reservation_overrun" for a in alerts):
                    raise ThesisImpactBudgetConflict("model reservation overrun requires owner reconciliation")
                committed = self._day_committed(cur, policy_version_id, day)
                mission_exceeded = False
                if mission_binding is not None:
                    # Same immutable paid-call ledger, atomic with the owner cap.
                    # Open reservations from older days remain charged after rollover.
                    rows = cur.execute(
                        "SELECT a.reserved_micros,s.actual_micros FROM thesis_impact_day_admissions a "
                        "LEFT JOIN model_mission_budget_bindings b ON a.admission_id=b.admission_id "
                        "LEFT JOIN thesis_impact_day_settlements s ON s.admission_id=a.admission_id "
                        "WHERE (b.mission_ref=? OR b.admission_id IS NULL) AND (a.day=? OR s.admission_id IS NULL)",
                        (mission_binding["mission_ref"], day),
                    ).fetchall()
                    mission_cost = sum(r["reserved_micros"] if r["actual_micros"] is None else r["actual_micros"] for r in rows)
                    mission_exceeded = (len(rows) + 1 > mission_binding["max_daily_paid_calls"] or
                                        mission_cost + reserved_micros > mission_binding["max_daily_cost_micros"])
                outer_exceeded = False
                if mission_binding is not None and mission_binding.get("outer_budget") is not None:
                    outer = mission_binding["outer_budget"]
                    # Conservative across ALL missions sharing this paid ledger,
                    # including unbound legacy entries and open prior-day calls.
                    # This cannot reset on mission/mandate/governance version changes.
                    outer_rows = cur.execute(
                        "SELECT a.reserved_micros,s.actual_micros FROM thesis_impact_day_admissions a "
                        "LEFT JOIN thesis_impact_day_settlements s ON s.admission_id=a.admission_id "
                        "WHERE a.day=? OR s.admission_id IS NULL", (day,),
                    ).fetchall()
                    outer_cost = sum(r["reserved_micros"] if r["actual_micros"] is None else r["actual_micros"] for r in outer_rows)
                    outer_exceeded = (len(outer_rows) + 1 > outer["max_daily_paid_calls"] or
                                      outer_cost + reserved_micros > outer["max_daily_cost_micros"])
                if mission_exceeded or outer_exceeded or committed + reserved_micros > policy["day_cap_micros"]:
                    rejection = {
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
                    if mission_binding is not None:
                        rejection["mission_binding"] = mission_binding
                        rejection["reason"] = ("mission_budget_exceeded" if mission_exceeded else
                                               "outer_research_budget_exceeded" if outer_exceeded else "owner_budget_exceeded")
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
                else:
                    rejection = None
            if rejection is None:
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
                if mission_binding is not None:
                    cur.execute("INSERT INTO model_mission_budget_bindings(admission_id,mission_ref,record_json) VALUES(?,?,?)",
                                (wire["admission_id"], mission_binding["mission_ref"], canonical_json(mission_binding)))
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
            admission = cur.execute(
                "SELECT reserved_micros FROM thesis_impact_day_admissions WHERE admission_id=?",
                (admission_id,),
            ).fetchone()
            if admission is None:
                raise ThesisImpactBudgetConflict("settlement references no admission")
            if actual_micros > int(admission["reserved_micros"]):
                raise ThesisImpactBudgetConflict(
                    "actual cost exceeds the admitted reservation"
                )
            existing = cur.execute(
                "SELECT record_json FROM thesis_impact_day_settlements "
                "WHERE admission_id=?",
                (admission_id,),
            ).fetchone()
            if existing is not None:
                persisted = json.loads(existing["record_json"])
                comparable_fields = (
                    "schema_version", "settlement_id", "admission_id",
                    "actual_micros", "usage_entry_ref",
                )
                if any(persisted[field] != wire[field] for field in comparable_fields):
                    raise ThesisImpactBudgetConflict(
                        "admission was already settled with different semantics"
                    )
                return {**persisted, "status": "duplicate"}
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
        created_at = _utc(self.clock())
        with self._transaction() as cur:
            row = cur.execute(
                "SELECT kind,severity,work_order_ref,phase,detail_json "
                "FROM thesis_impact_alerts WHERE alert_id=?",
                (alert_id,),
            ).fetchone()
            if row is not None:
                expected = (kind, severity, work_order_ref, phase, detail_json)
                actual = tuple(row[field] for field in (
                    "kind", "severity", "work_order_ref", "phase", "detail_json"
                ))
                if actual != expected:
                    raise ThesisImpactBudgetConflict(
                        "alert identity was reused with different semantics"
                    )
                return {"alert_id": alert_id, "status": "duplicate"}
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
        expires = (
            datetime.fromisoformat(now) + timedelta(seconds=claim_ttl_seconds)
        ).isoformat(timespec="microseconds")
        claims: list[dict[str, Any]] = []
        with self._transaction() as cur:
            rows = cur.execute(
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
            latest = cur.execute(
                "SELECT state FROM thesis_impact_alert_events WHERE alert_id=? "
                "ORDER BY event_seq DESC LIMIT 1",
                (alert_id,),
            ).fetchone()
            if latest is None or latest["state"] != "claimed":
                raise ThesisImpactBudgetConflict(
                    "delivery requires the alert's current claimed event"
                )
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
