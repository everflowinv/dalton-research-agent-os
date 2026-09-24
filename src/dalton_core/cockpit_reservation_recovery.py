"""Settle cockpit day-ledger reservations whose attempt is gone.

2026-09-24.  Until the settlement fix, a cockpit call whose actual cost was
above its reservation raised out of ``budget.settle`` before
``scheduler.complete``: the Scheduler lease expired two hours later, the next
ask got a new attempt and a new admission, and the old admission was never
settled.  An unsettled admission keeps counting its *full reservation* against
every later day (``ThesisImpactBudgetStore._day_committed`` sums open
reservations without a day filter), so the orphans are not only yesterday's
problem -- they shrink today's cap too.  Nothing in the running system settles
them: the fix stops new orphans, it does not reach back.

This module finds them and settles them, and nothing else:

* only ``work:cockpit-*`` admissions of the selected purposes, which are the
  ones whose attempts live in the cockpit Scheduler given here;
* only admissions whose Scheduler attempt is over -- expired, completed, or
  leased with a lease that has already run out.  An attempt still inside its
  lease is in flight and is never touched;
* at the broker journal's metered cost when every broker record inside the
  attempt's lease window carries an available cost, otherwise at
  ``unknown_cost``: the reservation (default -- the admission then counts on
  its own day only, which is never more than it counts today) or zero.

The ledger itself is append-only; this writes one ordinary settlement per
admission through :meth:`ThesisImpactBudgetStore.settle`, so an actual cost
above the reservation is booked and alerted exactly as a live call's is.
Planning opens every database read-only; only :func:`apply_plan` writes.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .readonly_sqlite import connect_cold_wal_snapshot, connect_read_only
from .thesis_impact_budget import ThesisImpactBudgetError, ThesisImpactBudgetStore


SCHEMA_VERSION = "dalton-cockpit-orphan-reservation-plan:0.1"
COCKPIT_PREFIX = "work:cockpit-"
DEFAULT_PURPOSES = ("event_judgement", "dossier", "dossier_verifier")
UNKNOWN_COST_POLICIES = ("reserved", "zero")
_OVER_STATES = frozenset({"expired", "succeeded", "failed", "retryable"})


class CockpitReservationRecoveryError(RuntimeError):
    """The inputs cannot support a safe plan."""


def purpose_of(work_order_ref: str) -> str | None:
    """``work:cockpit-<purpose>-<hash>`` -> ``<purpose>``."""

    if not work_order_ref.startswith(COCKPIT_PREFIX):
        return None
    body = work_order_ref[len(COCKPIT_PREFIX):]
    purpose, _, digest = body.rpartition("-")
    if not purpose or not digest:
        return None
    return purpose


@contextmanager
def _read_only(path: str | Path) -> Iterator[sqlite3.Connection]:
    """A live WAL database through its sidecars, a cold one as a snapshot."""

    target = Path(path)
    sidecars = [Path(str(target) + suffix) for suffix in ("-wal", "-shm")]
    if not target.is_file():
        raise CockpitReservationRecoveryError(f"database does not exist: {target}")
    if any(item.exists() for item in sidecars):
        connection = connect_read_only(target)
        try:
            connection.row_factory = sqlite3.Row
            yield connection
        finally:
            connection.close()
        return
    try:
        connection = connect_read_only(target)
    except sqlite3.OperationalError:
        with connect_cold_wal_snapshot(target) as connection:
            connection.row_factory = sqlite3.Row
            yield connection
        return
    try:
        connection.row_factory = sqlite3.Row
        yield connection
    finally:
        connection.close()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise CockpitReservationRecoveryError(f"timestamp without timezone: {value}")
    return parsed.astimezone(timezone.utc)


def _usd_micros(usd: Any) -> int | None:
    if isinstance(usd, bool) or not isinstance(usd, (int, float)) or usd < 0:
        return None
    return int((Decimal(str(usd)) * 1_000_000).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))


def load_broker_journal(path: str | Path | None) -> list[dict[str, Any]]:
    """The broker's retained records (it keeps only the most recent ones)."""

    if path is None:
        return []
    target = Path(path)
    if not target.is_file():
        return []
    data = json.loads(target.read_text(encoding="utf-8"))
    records = data.get("records") if isinstance(data, Mapping) else data
    if isinstance(records, Mapping):
        records = list(records.values())
    if not isinstance(records, list):
        raise CockpitReservationRecoveryError("broker journal has no record list")
    return [record for record in records if isinstance(record, Mapping)]


def _journal_cost(records: Iterable[Mapping[str, Any]], work_order_ref: str,
                  window: tuple[datetime, datetime]) -> dict[str, Any] | None:
    start_ms = window[0].timestamp() * 1000
    end_ms = window[1].timestamp() * 1000
    matched = []
    for record in records:
        response = record.get("response")
        if not isinstance(response, Mapping):
            continue
        if response.get("workOrderId") != work_order_ref:
            continue
        created = record.get("createdAtMs")
        if not isinstance(created, (int, float)) or not start_ms <= created < end_ms:
            continue
        matched.append((record, response))
    if not matched:
        return None
    total = 0
    for record, response in matched:
        cost = response.get("cost")
        micros = (_usd_micros(cost.get("usd"))
                  if isinstance(cost, Mapping) and cost.get("available") is True
                  else None)
        if record.get("state") != "completed" or micros is None:
            # One record in the window without a metered cost means the
            # attempt's spend is not known, whatever the others say.
            return None
        total += micros
    return {
        "actual_micros": total,
        "invocation_refs": sorted(str(r.get("invocationId")) for r, _ in matched),
    }


def plan_orphan_settlements(
    *,
    budget_db: str | Path,
    scheduler_db: str | Path,
    now: datetime,
    broker_journal: str | Path | None = None,
    purposes: Iterable[str] | None = DEFAULT_PURPOSES,
    unknown_cost: str = "reserved",
) -> dict[str, Any]:
    """Read-only: which open cockpit admissions may be settled, and at what."""

    if unknown_cost not in UNKNOWN_COST_POLICIES:
        raise CockpitReservationRecoveryError("unknown_cost must be reserved or zero")
    if now.tzinfo is None:
        raise CockpitReservationRecoveryError("now must include a timezone")
    now = now.astimezone(timezone.utc)
    wanted = None if purposes is None else frozenset(purposes)
    records = load_broker_journal(broker_journal)
    with _read_only(budget_db) as budget, _read_only(scheduler_db) as scheduler:
        open_rows = budget.execute(
            "SELECT a.admission_id,a.work_order_ref,a.attempt_number,a.day,"
            "a.reserved_micros,a.created_at FROM thesis_impact_day_admissions a "
            "WHERE a.work_order_ref LIKE 'work:cockpit-%' AND NOT EXISTS ("
            " SELECT 1 FROM thesis_impact_day_settlements s "
            " WHERE s.admission_id=a.admission_id) "
            "ORDER BY a.created_at",
        ).fetchall()
        settle: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in open_rows:
            item = {
                "admission_id": row["admission_id"],
                "work_order_ref": row["work_order_ref"],
                "attempt_number": row["attempt_number"],
                "day": row["day"],
                "reserved_micros": row["reserved_micros"],
                "purpose": purpose_of(row["work_order_ref"]),
            }
            if wanted is not None and item["purpose"] not in wanted:
                continue
            event = scheduler.execute(
                "SELECT state,lease_revision_id FROM scheduler_attempt_events "
                "WHERE work_order_id=? AND attempt_number=? "
                "ORDER BY event_seq DESC LIMIT 1",
                (row["work_order_ref"], row["attempt_number"]),
            ).fetchone()
            if event is None:
                skipped.append({**item, "reason": "attempt_not_in_scheduler"})
                continue
            leases = scheduler.execute(
                "SELECT issued_at,expires_at FROM scheduler_leases "
                "WHERE work_order_id=? AND attempt_number=?",
                (row["work_order_ref"], row["attempt_number"]),
            ).fetchall()
            if not leases:
                skipped.append({**item, "reason": "attempt_never_leased"})
                continue
            window = (min(_parse(lease["issued_at"]) for lease in leases),
                      max(_parse(lease["expires_at"]) for lease in leases))
            state = event["state"]
            if state == "leased" and window[1] > now:
                skipped.append({**item, "reason": "attempt_in_flight",
                                "lease_expires_at": window[1].isoformat()})
                continue
            if state != "leased" and state not in _OVER_STATES:
                skipped.append({**item, "reason": f"attempt_state_{state}"})
                continue
            metered = _journal_cost(records, row["work_order_ref"], window)
            if metered is not None:
                settle.append({**item, "attempt_state": state,
                               "actual_micros": metered["actual_micros"],
                               "source": "broker_journal",
                               "invocation_refs": metered["invocation_refs"]})
            else:
                settle.append({**item, "attempt_state": state,
                               "actual_micros": (row["reserved_micros"]
                                                 if unknown_cost == "reserved" else 0),
                               "source": f"unknown_cost_{unknown_cost}"})
    return {
        "schema_version": SCHEMA_VERSION,
        "planned_at": now.isoformat(timespec="microseconds"),
        "budget_db": str(budget_db),
        "scheduler_db": str(scheduler_db),
        "broker_journal": None if broker_journal is None else str(broker_journal),
        "broker_journal_records": len(records),
        "purposes": None if wanted is None else sorted(wanted),
        "unknown_cost": unknown_cost,
        "settle": settle,
        "skipped": skipped,
        "totals": {
            "settle_count": len(settle),
            "reserved_micros_released": sum(i["reserved_micros"] for i in settle),
            "settled_micros": sum(i["actual_micros"] for i in settle),
            "from_broker_journal": sum(i["source"] == "broker_journal" for i in settle),
            "skipped_count": len(skipped),
        },
    }


def apply_plan(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Write one settlement per planned admission (idempotent per admission)."""

    results = []
    with ThesisImpactBudgetStore(plan["budget_db"]) as budget:
        for item in plan["settle"]:
            try:
                settled = budget.settle(item["admission_id"],
                                        actual_micros=int(item["actual_micros"]))
            except ThesisImpactBudgetError as exc:
                # Settled by someone else since the plan was read (a late
                # live process, say) with a different amount: theirs stands.
                results.append({"admission_id": item["admission_id"],
                                "status": "refused", "error": str(exc)})
                continue
            results.append({
                "admission_id": item["admission_id"],
                "settlement_id": settled["settlement_id"],
                "status": settled["status"],
                "actual_micros": settled["actual_micros"],
                **({"overrun_micros": settled["overrun_micros"]}
                   if "overrun_micros" in settled else {}),
            })
    return results
