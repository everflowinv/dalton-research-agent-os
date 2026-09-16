"""The indexes the dashboard projection and the scheduler sweep read through.

Every statement here exists because a real query was measured doing a full
scan on the live Core.  Each one names the query it serves, because an index
nobody can attribute is an index nobody dares drop.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

#: How long a build waits for the writer's lock before giving up for now.
#: Short on purpose: an index that cannot be built this minute is built the
#: next time something opens the database, and the caller is a projection
#: round or a start-up that must not stall behind a busy writer.
BUSY_TIMEOUT_MS = 5_000

#: How long a *store open* may spend building indexes before leaving the rest
#: for later.
#:
#: On an empty database -- every fresh install -- all of them build in
#: milliseconds and the budget never binds.  On the live 1.2 GB Core, building
#: the full set cold measured 78 s, and a writer that takes 78 s longer to
#: start is a worse problem than a projection that is slow for another
#: minute.  So an open builds what it can and the next one continues; the
#: operator who wants them all at once runs the command in a quiet window.
OPEN_BUDGET_SECONDS = 5.0


@dataclass(frozen=True)
class IndexSpec:
    """One index, the table it sits on, and the read it exists for."""

    name: str
    table: str
    columns: str
    reason: str

    @property
    def statement(self) -> str:
        return (
            f"CREATE INDEX IF NOT EXISTS {self.name} "
            f"ON {self.table}({self.columns})"
        )


def _watermark_index(table: str) -> IndexSpec:
    """A ``created_at`` index for one table the watermark fingerprints.

    ``DashboardProjector._watermark`` asks every source table for
    ``COUNT(*), MAX(created_at)`` -- roughly thirty statements per round.
    ``COUNT(*)`` is cheap because there is a primary key to walk; ``MAX`` is
    not, because ``created_at`` was indexed nowhere, so each one was a full
    scan of the table's real pages.  On the live Core that is 26k work orders
    at 26 KB each, and the fingerprint alone measured 14.9 s of a 37 s round.
    With these it measures 0.08 s.
    """

    return IndexSpec(
        name=f"{table}_created_at",
        table=table,
        columns="created_at",
        reason=(
            "DashboardProjector._watermark reads MAX(created_at) from this "
            "table on every projection round"
        ),
    )


#: Every table ``_watermark`` fingerprints, per database.  Repeated here
#: rather than imported, because ``migrations`` must be importable by the
#: store without pulling in the projector; the accompanying test asserts the
#: two lists have not drifted apart.
WATERMARK_CORE_TABLES: tuple[str, ...] = (
    "model_invocations",
    "observability_workflow_versions",
    "observability_work_order_links",
    "observability_usage_entries",
    "observability_price_rate_versions",
    "observability_cost_entries",
    "observability_artifact_versions",
    "agenda_control_versions", "agenda_policy_versions", "agenda_cycles",
    "agenda_cycle_events", "agenda_candidates", "agenda_decisions",
    "agenda_feedback", "agenda_outbox_messages", "agenda_outbox_events",
    "connector_profile_versions", "connector_call_specs",
    "connector_invocations", "connector_rate_policy_versions",
    "connector_quota_reservations", "connector_physical_attempts",
    "connector_usage_entries", "connector_cost_entries",
    "connector_quota_settlements", "connector_incidents",
    "connector_incident_events", "connector_source_health_events",
)
WATERMARK_SCHEDULER_TABLES: tuple[str, ...] = (
    "scheduler_work_orders",
    "scheduler_attempt_events",
    "scheduler_leases",
    "scheduler_formal_results",
    "scheduler_result_envelopes",
)


#: ``core.sqlite``.
CORE_PROJECTION_INDEXES: tuple[IndexSpec, ...] = (
    IndexSpec(
        name="model_invocations_created_at_invocation",
        table="model_invocations",
        columns="created_at, invocation_id",
        reason=(
            "dashboard_projector.build_snapshot reads every invocation "
            "ORDER BY created_at, invocation_id; without this SQLite builds a "
            "TEMP B-TREE over the whole table on every projection round"
        ),
    ),
    IndexSpec(
        name="observability_work_order_links_created_at_link",
        table="observability_work_order_links",
        columns="created_at, link_id",
        reason=(
            "the same round reads every work-order link in the same order, "
            "for the same reason"
        ),
    ),
) + tuple(_watermark_index(table) for table in WATERMARK_CORE_TABLES)

#: ``scheduler.sqlite``.  ``scheduler_formal_results`` is deliberately absent:
#: its ``work_order_id`` is already ``UNIQUE``, so SQLite has kept an index on
#: it since the table was created and a second one would only cost space.
SCHEDULER_PROJECTION_INDEXES: tuple[IndexSpec, ...] = (
    IndexSpec(
        name="scheduler_leases_work_order_attempt",
        table="scheduler_leases",
        columns="work_order_id, attempt_number",
        reason=(
            "DashboardProjector._latest_lease_rows partitions by "
            "(work_order_id, attempt_number) over every lease revision"
        ),
    ),
    IndexSpec(
        name="scheduler_result_envelopes_work_order_attempt",
        table="scheduler_result_envelopes",
        columns="work_order_id, attempt_number",
        reason=(
            "DashboardProjector._latest_rows partitions the result envelopes "
            "by (work_order_id, attempt_number)"
        ),
    ),
) + tuple(_watermark_index(table) for table in WATERMARK_SCHEDULER_TABLES)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _index_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    return row is not None


def ensure_indexes(
    connection: sqlite3.Connection,
    specs: Iterable[IndexSpec],
    *,
    busy_timeout_ms: int = BUSY_TIMEOUT_MS,
    budget_seconds: float | None = None,
) -> dict[str, Any]:
    """Build whichever of ``specs`` is missing, and report, never raise.

    Three outcomes per index and they are all normal.  ``present`` is the
    steady state and costs one lookup in ``sqlite_master``.  ``created`` is
    the first run after a deploy.  ``deferred`` is a database whose writer is
    busy right now, or a table this schema does not have -- both are answered
    by trying again next time, which is why nothing here escalates.
    """

    report: dict[str, Any] = {"created": [], "present": [], "deferred": {}}
    # ``busy_timeout`` is connection state, and this connection belongs to
    # whoever handed it over -- the store sets 15 s deliberately, and an index
    # build must not leave it at five.  Borrowed and given back.
    restore: int | None = None
    try:
        row = connection.execute("PRAGMA busy_timeout").fetchone()
        restore = None if row is None else int(row[0])
        connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    except (sqlite3.Error, TypeError, ValueError):
        restore = None
    try:
        _build_indexes(connection, specs, report, budget_seconds)
    finally:
        if restore is not None:
            try:
                connection.execute(f"PRAGMA busy_timeout = {restore}")
            except sqlite3.Error:  # pragma: no cover - nothing left to restore
                pass
    return report


def _build_indexes(
    connection: sqlite3.Connection,
    specs: Iterable[IndexSpec],
    report: dict[str, Any],
    budget_seconds: float | None = None,
) -> None:
    deadline = (
        None if budget_seconds is None else time.monotonic() + float(budget_seconds)
    )
    for spec in specs:
        try:
            if not _table_exists(connection, spec.table):
                report["deferred"][spec.name] = "table is absent"
                continue
            if _index_exists(connection, spec.name):
                report["present"].append(spec.name)
                continue
            if deadline is not None and time.monotonic() >= deadline:
                # Checked before starting one, never during: an index build
                # is not interruptible and a half-built one does not exist.
                report["deferred"][spec.name] = "not within this attempt's budget"
                continue
            started = time.monotonic()
            connection.execute(spec.statement)
            connection.commit()
            report["created"].append(
                {"name": spec.name, "seconds": round(time.monotonic() - started, 3)}
            )
        except sqlite3.Error as exc:
            # A locked database is the expected failure and the whole reason
            # this is not in schema.sql: the projection is slow until the next
            # attempt, and the writer starts.
            report["deferred"][spec.name] = f"{type(exc).__name__}: {exc}"[:200]


def _apply_one(path: Path | str | None, specs: Sequence[IndexSpec],
               *, busy_timeout_ms: int) -> dict[str, Any] | None:
    if path is None:
        return None
    candidate = Path(path)
    if not candidate.exists():
        return {"created": [], "present": [], "deferred": {"database": "absent"}}
    connection = sqlite3.connect(str(candidate), isolation_level=None)
    try:
        return ensure_indexes(connection, specs, busy_timeout_ms=busy_timeout_ms)
    finally:
        connection.close()


def apply_projection_indexes(
    *,
    core_db: Path | str | None = None,
    scheduler_db: Path | str | None = None,
    busy_timeout_ms: int = BUSY_TIMEOUT_MS,
) -> dict[str, Any]:
    """Ensure both databases carry the indexes the projection reads through."""

    return {
        "core": _apply_one(core_db, CORE_PROJECTION_INDEXES,
                           busy_timeout_ms=busy_timeout_ms),
        "scheduler": _apply_one(scheduler_db, SCHEDULER_PROJECTION_INDEXES,
                                busy_timeout_ms=busy_timeout_ms),
    }


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(
        description=(
            "Build the dashboard projection's indexes. Idempotent, and safe "
            "to run while the service is up: an index that cannot take the "
            "write lock right now is reported as deferred, not raised."
        )
    )
    parser.add_argument("--core-db")
    parser.add_argument("--scheduler-db")
    parser.add_argument("--busy-timeout-ms", type=int, default=BUSY_TIMEOUT_MS)
    args = parser.parse_args(argv)
    report = apply_projection_indexes(
        core_db=args.core_db, scheduler_db=args.scheduler_db,
        busy_timeout_ms=args.busy_timeout_ms,
    )
    import json as _json

    print(_json.dumps(report, ensure_ascii=False, indent=2))
    deferred = [
        name
        for section in report.values() if section
        for name in section["deferred"]
    ]
    return 1 if deferred else 0


if __name__ == "__main__":  # pragma: no cover - exercised as a command
    raise SystemExit(main())
