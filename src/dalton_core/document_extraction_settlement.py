"""Preserve accounted extraction costs when the day ledger is locked."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

from .pending_settlement_registry import PendingSettlements
from .sqlite_contention import (LEASE_RELEASE_RETRY_SECONDS,
                               is_sqlite_lock_error, retry_on_sqlite_lock)

SETTLEMENT_RETRY_SECONDS = LEASE_RELEASE_RETRY_SECONDS


def settle_accounted_window(budget: Any, admission: Mapping[str, Any], *,
                            actual_micros: int, usage_entry_ref: str,
                            retry_seconds: float | None = None) -> dict | None:
    """Defer only a proved lock, retaining the exact cost and usage binding.

    The model result still undergoes the worker's normal validation/completion.
    A durable deferred settlement does not assert that the ledger settled.
    Non-lock errors and an unavailable registry propagate to the caller.
    """
    deadline = SETTLEMENT_RETRY_SECONDS if retry_seconds is None else retry_seconds
    connection = budget.connection
    original_busy = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    # Bound each SQLite attempt as well as the retry loop. This is the worker's
    # private connection, not a global database setting.
    connection.execute(f"PRAGMA busy_timeout={min(original_busy, 1000, max(0, int(deadline * 1000)))}")
    try:
        try:
            return retry_on_sqlite_lock(
                lambda: budget.settle(admission["admission_id"],
                    actual_micros=actual_micros, usage_entry_ref=usage_entry_ref),
                deadline_seconds=deadline)
        except sqlite3.OperationalError as exc:
            if not is_sqlite_lock_error(exc):
                raise
            recorded = PendingSettlements.for_budget(budget).record_settlement(
                admission_id=admission["admission_id"], actual_micros=actual_micros,
                usage_entry_ref=usage_entry_ref,
                work_order_ref=admission.get("work_order_ref"),
                attempt_number=admission.get("attempt_number"),
                reason=f"{type(exc).__name__}: {exc}")
            if not recorded:
                raise
            return None
    finally:
        connection.execute(f"PRAGMA busy_timeout={original_busy}")


def retry_within_lease(scheduler: Any, work_ref: str, attempt: int, now: datetime) -> float:
    """Leave five seconds for the worker's existing completion/late-result gate."""
    row = scheduler.connection.execute(
        "SELECT l.expires_at FROM scheduler_attempt_events e "
        "JOIN scheduler_leases l ON l.lease_revision_id=e.lease_revision_id "
        "WHERE e.work_order_id=? AND e.attempt_number=? AND e.state='leased' "
        "ORDER BY e.event_seq DESC LIMIT 1", (work_ref, attempt)).fetchone()
    if row is None:
        return 0.0
    expiry = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
    return max(0.0, min(SETTLEMENT_RETRY_SECONDS,
                       (expiry - now.astimezone(timezone.utc)).total_seconds() - 5.0))


def drain_accounted_windows(budget: Any) -> list[dict]:
    """A bounded probe of deferred settlements, using the original exact record."""
    connection = budget.connection
    original_busy = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    connection.execute(f"PRAGMA busy_timeout={min(original_busy, 1000)}")
    try:
        outcomes = PendingSettlements.for_budget(budget).drain(budget)
    finally:
        connection.execute(f"PRAGMA busy_timeout={original_busy}")
    for outcome in outcomes:
        if outcome.get("status") == "kept":
            raise RuntimeError("pending extraction settlement requires reconciliation: "
                               + str(outcome.get("error", "unknown ledger conflict")))
    return outcomes
