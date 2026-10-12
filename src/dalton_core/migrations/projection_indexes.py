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
    #: A partial index's predicate, when most rows would only be dead weight.
    where: str = ""

    @property
    def statement(self) -> str:
        return (
            f"CREATE INDEX IF NOT EXISTS {self.name} "
            f"ON {self.table}({self.columns})"
            + (f" WHERE {self.where}" if self.where else "")
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


#: The exact expressions ``forecast_reconciliation`` filters Claims by.  An
#: expression index serves a query only when the query spells the same
#: expressions, so both sides import this one string.
CLAIM_SUBJECT_METRIC_PERIOD = (
    "json_extract(claim_json,'$.subject_ref'), "
    "json_extract(claim_json,'$.metric_or_aspect'), "
    "json_extract(claim_json,'$.period')"
)
#: The same for the two scheduler work-order lookups by metadata.
WORK_ORDER_DOCUMENT_STAGE = (
    "json_extract(work_order_json,'$.metadata.mission_document_research_admission_ref'), "
    "json_extract(work_order_json,'$.metadata.stage')"
)
WORK_ORDER_REQUEST_ID = "json_extract(work_order_json,'$.metadata.request_id')"


#: Work-order JSON can be tens of KB, although the first projection needs only
#: these exact capabilities and identity columns. The rowid is implicit in the
#: index. Expression spelling must match the fold query exactly.
WORK_ORDER_FOLD_COVER_INDEX = IndexSpec(
    name="scheduler_work_orders_projection_fold_cover",
    table="scheduler_work_orders",
    columns="work_order_id, created_at, json_extract(work_order_json,'$.requested_capabilities')",
    reason="dashboard cold WorkOrder fold reads complete lightweight history without reading prompt bodies",
)
FORMAL_RESULT_FOLD_COVER_INDEX = IndexSpec(
    name="scheduler_formal_results_projection_fold_cover",
    table="scheduler_formal_results",
    columns="work_order_id, result_record_id, attempt_number, result_envelope_id, terminal_state, created_at",
    reason="dashboard cold formal fold reads complete terminal metadata without response JSON pages",
)
RESULT_ENVELOPE_FOLD_COVER_INDEX = IndexSpec(
    name="scheduler_result_envelopes_projection_fold_cover",
    table="scheduler_result_envelopes",
    columns="result_envelope_id, work_order_id, attempt_number, created_at",
    reason="dashboard cold receipt fold reads complete receipt metadata without response JSON pages",
)
SCHEDULER_FOLD_COVER_INDEXES = (
    WORK_ORDER_FOLD_COVER_INDEX, FORMAL_RESULT_FOLD_COVER_INDEX,
    RESULT_ENVELOPE_FOLD_COVER_INDEX,
)
# Deliberately separate from SCHEDULER_PROJECTION_INDEXES used by worker opens.
# apply_projection_indexes (also used by service startup) installs this index.
# Its first CREATE reads every old JSON body and holds a write lock: large
# existing authorities must prebuild in a drained quiet window. The open budget
# checks only before each CREATE; it is not a per-statement execution deadline.

#: Compact historical fold reads on wide connector authority tables. Only a
#: fresh fold forces these indexes; warm rowid deltas retain the primary-key
#: seek. No source rows, immutable hashes or quota values change.
CONNECTOR_FOLD_COVER_INDEXES: tuple[IndexSpec, ...] = (
    IndexSpec(name="connector_invocations_projection_fold_cover", table="connector_invocations",
              columns="connector_invocation_id, connector_profile_ref, call_spec_ref, created_at",
              reason="dashboard projector first fold covers lightweight historical columns"),
    IndexSpec(name="connector_call_specs_projection_fold_cover", table="connector_call_specs",
              columns="call_spec_id, operation, created_at",
              reason="dashboard projector first fold covers lightweight historical columns"),
    IndexSpec(name="connector_physical_attempts_projection_fold_cover", table="connector_physical_attempts",
              columns="physical_attempt_id, connector_invocation_ref, physical_attempt_number, reservation_ref, outcome, started_at, completed_at, retry_at, created_at",
              reason="dashboard projector first fold covers lightweight historical columns"),
    IndexSpec(name="connector_cost_entries_projection_fold_cover", table="connector_cost_entries",
              columns="cost_entry_id, usage_entry_ref, revision_number, amount_micros, currency, cost_status, created_at",
              reason="dashboard projector first fold covers lightweight historical columns"),
)

#: ``core.sqlite``.
CORE_PROJECTION_INDEXES: tuple[IndexSpec, ...] = (
    IndexSpec(name="idx_connector_call_query_hash", table="connector_call_specs",
        columns="query_hash", reason="Legacy SEC archive parents seek the exact original query before invocation/envelope joins"),
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
    # 2026-09-29: the writer's per-tick reads, not the projection's.
    IndexSpec(
        name="claim_versions_subject_metric_period",
        table="claim_versions",
        # ``claim_version_id`` last: with it the per-subject change key
        # (``lane_change_key.claim_change_keys``, twice per tick) is answered
        # from the index alone instead of parsing every Claim.
        columns=CLAIM_SUBJECT_METRIC_PERIOD + ", claim_version_id",
        reason=(
            "ForecastReconciliation._latest_claims runs once per latest "
            "forecast line on every controller tick, filtering by these three "
            "json_extract expressions; without the index each call parsed "
            "every Claim's JSON (48 lines x 22 MB = ~1 GB per tick on the "
            "legacy Core).  Its leading column also serves every per-company "
            "Claim read keyed by json_extract(claim_json,'$.subject_ref'), and "
            "the per-subject change key reads nothing but this index"
        ),
    ),
    IndexSpec(
        name="claim_versions_created_at",
        table="claim_versions",
        columns="created_at",
        reason=(
            "the dossier and industry-framework ledger signatures read "
            "MAX(created_at) from claim_versions every tick, and the claim "
            "review orders by it; created_at sits after claim_json in the row"
        ),
    ),
    IndexSpec(
        name="evidence_relations_claim_version",
        table="evidence_relations",
        columns="claim_version_id, relation",
        reason=(
            "a Claim's supporting evidence (forecast reconciliation, "
            "get_claim) is looked up by claim_version_id, which was indexed "
            "nowhere: one full scan of evidence_relations per Claim"
        ),
    ),
    # 2026-10-07 (legacy writer stall).  "The newest, uncorrected row" is
    # asked as ``NOT EXISTS (SELECT 1 FROM t n WHERE n.correction_of_ref=
    # t.id)``, and ``correction_of_ref`` was indexed nowhere, so every lookup
    # scanned the whole table (107k rows, 110-146 MB on legacy) once per row
    # it matched.  The document-research runtime re-verifies every fetched
    # page's receipts this way: 342 scans, 7.4 s warm, per owner door.  On a
    # /tmp copy of legacy: 200 usage+settlement lookups 8.7 s -> 0.016 s.
    # Partial: almost no row is a correction, so the index is near empty.
    *(
        IndexSpec(
            name=f"{table}_correction_of",
            table=table,
            columns="correction_of_ref",
            where="correction_of_ref IS NOT NULL",
            reason=(
                "public_web_extraction_source.verified_public_web_source and "
                "the receipt readers select the row no correction supersedes "
                "(NOT EXISTS ... n.correction_of_ref=...); a full scan per row "
                "without it"
            ),
        )
        for table in ("connector_usage_entries", "connector_quota_settlements",
                      "connector_cost_entries")
    ),
    # 2026-10-07: the document-research executor's Scheduler lives on the
    # Core (``MissionDocumentResearchRuntime`` builds it on the store's
    # connection), not on scheduler.sqlite where these two were built.  The
    # stage lookup and the owner's withdrawal check (``running_work_orders``)
    # therefore scanned and parsed every Core work order on every call.
    IndexSpec(
        name="scheduler_work_orders_document_stage",
        table="scheduler_work_orders",
        columns=WORK_ORDER_DOCUMENT_STAGE,
        where=(
            "json_extract(work_order_json,"
            "'$.metadata.mission_document_research_admission_ref') IS NOT NULL"
        ),
        reason=(
            "mission_document_research_executor._effective_stage and "
            "mission_document_research_withdrawal.running_work_orders find an "
            "admission's work orders on the Core's own scheduler tables"
        ),
    ),
    IndexSpec(
        name="scheduler_work_orders_request_id",
        table="scheduler_work_orders",
        columns=WORK_ORDER_REQUEST_ID,
        where=f"{WORK_ORDER_REQUEST_ID} IS NOT NULL",
        reason=(
            "controlled_lane_reentry._already_consumed on the Core's own "
            "scheduler tables"
        ),
    ),
) + CONNECTOR_FOLD_COVER_INDEXES + tuple(_watermark_index(table) for table in WATERMARK_CORE_TABLES)

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
    # 2026-09-29.  Both are partial: only the work orders that carry the
    # field are indexed, and a query's ``expr = ?`` implies ``expr IS NOT
    # NULL``, so SQLite uses them.  Building either reads every work order's
    # JSON once (0.85 GB on the legacy scheduler); after that a lookup is a
    # seek instead of that same read.
    IndexSpec(
        name="scheduler_work_orders_document_stage",
        table="scheduler_work_orders",
        columns=WORK_ORDER_DOCUMENT_STAGE,
        where=(
            "json_extract(work_order_json,"
            "'$.metadata.mission_document_research_admission_ref') IS NOT NULL"
        ),
        reason=(
            "mission_document_research_executor._effective_stage finds a "
            "stage's stored work orders by admission and stage; it was a "
            "json_extract scan of every work order per call"
        ),
    ),
    IndexSpec(
        name="scheduler_work_orders_request_id",
        table="scheduler_work_orders",
        columns=WORK_ORDER_REQUEST_ID,
        where=f"{WORK_ORDER_REQUEST_ID} IS NOT NULL",
        reason=(
            "controlled_lane_reentry._already_consumed asks whether a "
            "recovery request id was ever enqueued; it read and parsed every "
            "work order to answer"
        ),
    ),
) + tuple(_watermark_index(table) for table in WATERMARK_SCHEDULER_TABLES)


#: ``model-router.sqlite``.  2026-09-30 (cockpit review P1-1): the three reads
#: ``ModelRouter.chain_link_digest`` makes for the model and sources pages.
#: Each build reads the link table once (~60 MB, 48k links on legacy): all
#: three took ~70 ms on a compact copy of legacy's links -- expect a second or
#: two cold on the live file -- and add ~3 MB together.
MODEL_ROUTER_INDEXES: tuple[IndexSpec, ...] = (
    IndexSpec(
        name="model_route_chain_links_served_tier",
        table="model_route_chain_links",
        columns="served, tier, link_sequence",
        reason=(
            "chain_link_digest: SELECT tier, MAX(link_sequence) ... WHERE "
            "served=1 GROUP BY tier, the newest skipped links of a tier "
            "(served=0 AND tier=? ORDER BY link_sequence DESC LIMIT n) and "
            "SELECT DISTINCT tier -- each a scan of every link without it"
        ),
    ),
    IndexSpec(
        name="model_route_chain_links_served_purpose",
        table="model_route_chain_links",
        columns="served, purpose, link_sequence",
        reason=(
            "chain_link_digest: SELECT purpose, MAX(link_sequence) ... WHERE "
            "served=1 GROUP BY purpose (last served link per calling stage)"
        ),
    ),
    IndexSpec(
        name="model_route_chain_links_served_tier_reason",
        table="model_route_chain_links",
        columns="served, tier, skip_reason",
        reason=(
            "chain_link_digest: a tier's skip counts, SELECT skip_reason, "
            "COUNT(*) ... WHERE served=0 AND tier=? GROUP BY skip_reason"
        ),
    ),
)


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
                if spec.name == "idx_connector_call_query_hash":
                    owner = connection.execute("SELECT tbl_name,sql FROM sqlite_master WHERE type='index' AND name=?", (spec.name,)).fetchone()
                    columns = tuple((row[2],row[3],row[4]) for row in connection.execute("PRAGMA index_xinfo(idx_connector_call_query_hash)") if row[5])
                    flags = next((row for row in connection.execute("PRAGMA index_list(connector_call_specs)") if row[1] == spec.name), None)
                    if flags is None or owner[0] != spec.table or columns != (("query_hash",0,"BINARY"),) or flags[2] or flags[4]:
                        report["deferred"][spec.name] = "existing index has wrong shape; owner review required"
                        continue
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
    model_router_db: Path | str | None = None,
) -> dict[str, Any]:
    """Ensure the databases carry the indexes their read paths go through.

    ``model_router_db`` is reported only when asked for, so the projection's
    own callers see exactly the two sections they always did. The wide-JSON
    WorkOrder expression index is installed here, not by every worker open.
    First upgrade of a large existing authority requires a drained quiet window;
    busy_timeout bounds lock acquisition, not CREATE INDEX execution.
    """

    report = {
        "core": _apply_one(core_db, CORE_PROJECTION_INDEXES,
                           busy_timeout_ms=busy_timeout_ms),
        "scheduler": _apply_one(
            scheduler_db, SCHEDULER_PROJECTION_INDEXES + SCHEDULER_FOLD_COVER_INDEXES,
            busy_timeout_ms=busy_timeout_ms),
    }
    if model_router_db is not None:
        report["model_router"] = _apply_one(
            model_router_db, MODEL_ROUTER_INDEXES, busy_timeout_ms=busy_timeout_ms)
    return report


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(
        description=(
            "Build the dashboard projection's indexes idempotently. Prebuild "
            "on large existing authorities in a drained quiet window: CREATE "
            "INDEX scans history while holding a write lock. Lock acquisition "
            "failure is deferred; the lock timeout does not bound build time."
        )
    )
    parser.add_argument("--core-db")
    parser.add_argument("--scheduler-db")
    parser.add_argument("--model-router-db")
    parser.add_argument("--busy-timeout-ms", type=int, default=BUSY_TIMEOUT_MS)
    args = parser.parse_args(argv)
    report = apply_projection_indexes(
        core_db=args.core_db, scheduler_db=args.scheduler_db,
        busy_timeout_ms=args.busy_timeout_ms,
        model_router_db=args.model_router_db,
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
