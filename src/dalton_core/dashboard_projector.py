"""Rebuild the disposable dashboard projection from read-only authorities.

The projector is trusted ETL, not a new source of truth.  It opens Core and
Scheduler databases with SQLite ``mode=ro``, emits only the closed dashboard
row shapes, and delegates the atomic projection replacement to
``dashboard.ProjectionWriter``.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections import defaultdict
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

from .dashboard import ProjectionWriter, SCHEMA_VERSION
from .migrations.projection_indexes import WORK_ORDER_FOLD_COVER_INDEX
from .projection_cancellation import check_cancelled, install_cancel_handler
from .projection_folds import (
    _REGISTRY as _FOLD_REGISTRY,
    AppendOnlyFold,
    FoldRead,
    retire_keys,
    source_identity,
)


class DashboardProjectorError(Exception):
    pass


class ProjectionSourceError(DashboardProjectorError):
    pass


_CORE_TABLES = frozenset(
    {
        "model_invocations",
        "observability_workflow_versions",
        "observability_work_order_links",
        "observability_usage_entries",
        "observability_price_rate_versions",
        "observability_cost_entries",
        "observability_artifact_versions",
    }
)
_SCHEDULER_TABLES = frozenset(
    {
        "scheduler_work_orders",
        "scheduler_attempt_events",
        "scheduler_leases",
        "scheduler_formal_results",
        "scheduler_result_envelopes",
    }
)
_AGENDA_TABLES = frozenset(
    {
        "agenda_control_versions", "agenda_control_pointer", "agenda_policy_versions",
        "agenda_policy_pointer", "agenda_cycles", "agenda_cycle_events",
        "agenda_candidates", "agenda_decisions", "agenda_feedback",
        "agenda_outbox_messages", "agenda_outbox_events",
    }
)
_CONNECTOR_PROJECTION_TABLES = frozenset(
    {
        "connector_profile_versions", "connector_call_specs",
        "connector_invocations", "connector_rate_policy_versions",
        "connector_quota_reservations", "connector_physical_attempts",
        "connector_usage_entries", "connector_cost_entries",
        "connector_quota_settlements", "connector_incidents",
        "connector_incident_events", "connector_source_health_events",
    }
)
_CONNECTOR_AUTHORITY_TABLES = _CONNECTOR_PROJECTION_TABLES | frozenset(
    {
        "connector_rate_policy_activation_events",
        "connector_price_rate_versions",
        "connector_source_envelopes",
        "connector_idempotency_keys",
    }
)
_METADATA_SOURCE_PROJECTION_TABLES = frozenset(
    {
        "external_capability_source_registrations",
        "external_capability_active_source",
        "external_capability_source_heads",
        "external_capability_snapshot_ingest_events",
    }
)
_METADATA_SOURCE_AUTHORITY_TABLES = _METADATA_SOURCE_PROJECTION_TABLES | frozenset(
    {"external_capability_snapshot_chains"}
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DashboardProjectorError("projector clock must return an aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ProjectionSourceError(f"{name} is not RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectionSourceError(f"{name} is not RFC3339") from exc
    if parsed.tzinfo is None:
        raise ProjectionSourceError(f"{name} lacks timezone")
    return parsed.astimezone(timezone.utc)


def _open_read_only(path: str | Path, *,
                    cancel_requested: Callable[[], bool] | None = None) -> sqlite3.Connection:
    check_cancelled(cancel_requested)
    resolved = Path(path).resolve()
    uri = f"file:{quote(resolved.as_posix(), safe='/:')}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.Error as exc:
        raise ProjectionSourceError("authority database cannot be opened read-only") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    install_cancel_handler(conn, cancel_requested)
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _require_tables(
    conn: sqlite3.Connection, required: set[str] | frozenset[str], source: str
) -> None:
    missing = set(required) - _tables(conn)
    if missing:
        raise ProjectionSourceError(
            f"{source} authority is missing required table(s): {sorted(missing)}"
        )


def _json(value: Any, name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProjectionSourceError(f"{name} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ProjectionSourceError(f"{name} must be a JSON object")
    return parsed


def _json_array(value: Any, name: str) -> list[Any]:
    """A JSON array read out of SQLite by ``json_extract``, or a refusal.

    ``json_extract`` returns SQL NULL when the path is absent, which is a work
    order that requested no capability -- an empty list, not a broken row.
    Anything else that is not an array is a source the projection must not
    silently reinterpret.
    """

    if value is None:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProjectionSourceError(f"{name} is not valid JSON") from exc
    if not isinstance(parsed, list):
        raise ProjectionSourceError(f"{name} must be a JSON array")
    return parsed


#: B1-4 / 2026-09-29: what each round has already learned about the
#: authorities, kept per database file as incremental folds (see
#: ``projection_folds``).
#:
#: Reading the work orders used to cost 4.4 s of a 7.5 s round: ``SELECT *``
#: over rows whose ``work_order_json`` averages 12-26 KB, to reach one field
#: (``requested_capabilities``, which becomes a display label) and one column
#: (``created_at``).  The next fix kept a ``created_at >=`` watermark, but
#: ``created_at`` is stamped before ``BEGIN IMMEDIATE`` so a writer that waited
#: out a lock commits behind it, and the same shape of re-read remained for
#: every other table: every attempt event, lease revision, result envelope and
#: formal result, every model invocation, usage and cost entry, every
#: artifact and every connector attempt, per round, while one read
#: transaction pinned the scheduler WAL for the whole round.  Every one of
#: those tables is append-only (their schemas refuse UPDATE and DELETE), so a
#: ``rowid`` high-water mark is exact: a round reads only the rows appended
#: since the last one, through the primary key.
#:
#: ``_WORK_ORDER_CACHE`` is the fold registry; clearing it forgets every fold.
_WORK_ORDER_CACHE = _FOLD_REGISTRY

_WORK_ORDER_COLUMNS = (
    "work_order_id, created_at,"
    " json_extract(work_order_json,'$.requested_capabilities')"
    " AS requested_capabilities"
)


def _work_order_entry(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "created_at": row["created_at"],
        "wire": {
            "requested_capabilities": _json_array(
                row["requested_capabilities"], "scheduler work order capabilities"
            ),
        },
    }


def _keyed(key: Callable[[sqlite3.Row], Any]) -> Callable[[dict, sqlite3.Row], None]:
    """Reduce to the newest row per key (rowid order: later wins)."""

    def reduce(state: dict, row: sqlite3.Row) -> None:
        state[key(row)] = row

    return reduce


def _keyed_max(
    key: Callable[[sqlite3.Row], Any], order: Callable[[sqlite3.Row], Any]
) -> Callable[[dict, sqlite3.Row], None]:
    """Reduce to the row with the greatest ``order`` per key; ties: later."""

    def reduce(state: dict, row: sqlite3.Row) -> None:
        group = key(row)
        current = state.get(group)
        if current is None or order(row) >= order(current):
            state[group] = row

    return reduce


def _sorted_by(key: Callable[[sqlite3.Row], Any]) -> Callable[[list, sqlite3.Row], None]:
    """Keep every row, in ``key`` order (a total order on the table)."""

    def reduce(state: list, row: sqlite3.Row) -> None:
        bisect.insort(state, (key(row), row))

    return reduce


def _latest_record_fold(
    name: str, table: str, id_column: str, group: str, version: str,
    body: str = "record_json",
) -> AppendOnlyFold:
    """``_latest_records`` as a fold: newest version per group, raw JSON kept."""

    def reduce(state: dict, row: sqlite3.Row) -> None:
        current = state.get(row[group])
        if current is None or row[version] > current[0]:
            state[row[group]] = (
                row[version], row["created_at"], row["fold_rowid"], row[body])

    return AppendOnlyFold(
        name, table, f"{id_column}, {group}, {version}, created_at, {body}",
        key=id_column, init=dict, reduce=reduce,
    )


def _ordered_records(state: Mapping[Any, tuple], name: str) -> list[dict[str, Any]]:
    """A latest-record fold's wires, ``ORDER BY created_at`` as the SQL had it."""

    return [
        _json(item[3], name)
        for item in sorted(state.values(), key=lambda item: (item[1], item[2]))
    ]


_WORK_ORDER_FOLD = AppendOnlyFold(
    "scheduler.work_orders", "scheduler_work_orders", _WORK_ORDER_COLUMNS,
    key="work_order_id", init=dict,
    reduce=lambda state, row: state.__setitem__(row["work_order_id"], _work_order_entry(row)),
    cold_cover_index=WORK_ORDER_FOLD_COVER_INDEX.name,
    cold_cover_sql=WORK_ORDER_FOLD_COVER_INDEX.statement,
)
_ATTEMPT_EVENT_FOLD = AppendOnlyFold(
    "scheduler.latest_events", "scheduler_attempt_events",
    "event_seq, work_order_id, state, attempt_number, event_id, created_at",
    key="event_id", init=dict, reduce=_keyed(lambda row: row["work_order_id"]),
)
_FORMAL_RESULT_FOLD = AppendOnlyFold(
    "scheduler.formal_results", "scheduler_formal_results",
    "work_order_id, result_record_id, attempt_number, result_envelope_id,"
    " terminal_state, created_at",
    key="result_record_id", init=dict, reduce=_keyed(lambda row: row["work_order_id"]),
)
_RESULT_ENVELOPE_FOLD = AppendOnlyFold(
    "scheduler.latest_receipts", "scheduler_result_envelopes",
    "result_envelope_id, work_order_id, attempt_number, created_at",
    key="result_envelope_id", init=dict,
    reduce=_keyed_max(lambda row: row["work_order_id"],
                      lambda row: (row["attempt_number"], row["created_at"])),
)
_LEASE_FOLD = AppendOnlyFold(
    "scheduler.latest_leases", "scheduler_leases",
    "lease_revision_id, work_order_id, attempt_number, lease_version, expires_at,"
    " created_at",
    key="lease_revision_id", init=dict,
    reduce=_keyed_max(lambda row: (row["work_order_id"], row["attempt_number"]),
                      lambda row: (row["lease_version"], row["created_at"])),
)

_INVOCATION_FOLD = AppendOnlyFold(
    "core.model_invocations", "model_invocations",
    "invocation_id, created_at, invocation_json",
    key="invocation_id", init=list,
    reduce=_sorted_by(lambda row: (row["created_at"], row["invocation_id"])),
)
_LINK_FOLD = AppendOnlyFold(
    "core.work_order_links", "observability_work_order_links",
    "link_id, created_at, record_json",
    key="link_id", init=list,
    reduce=_sorted_by(lambda row: (row["created_at"], row["link_id"])),
)
_WORKFLOW_FOLD = _latest_record_fold(
    "core.workflow_versions", "observability_workflow_versions",
    "version_id", "workflow_ref", "version_number",
)
_USAGE_FOLD = _latest_record_fold(
    "core.usage_entries", "observability_usage_entries",
    "usage_entry_id", "invocation_ref", "revision_number",
)
_COST_FOLD = _latest_record_fold(
    "core.cost_entries", "observability_cost_entries",
    "cost_entry_id", "usage_entry_ref", "revision_number",
)

#: The artifact fields any projection reads.  An artifact version's record is
#: ~1 KB and the dashboard shows eleven of its fields, so the fold keeps those
#: (and only the ones the record has, so a missing field still fails the way
#: it did when the whole record was read).
_ARTIFACT_FIELDS = tuple(sys.intern(name) for name in (
    "artifact_ref", "work_order_ref", "title", "kind", "media_type",
    "size_bytes", "artifact_content_hash", "access_class", "preview_status",
    "producer_execution_ref", "producer_invocation_ref", "created_at",
))


def _artifact_index_reduce(state: dict, row: sqlite3.Row) -> None:
    current = state.get(row["artifact_ref"])
    if current is None or row["version_number"] > current[0]:
        # The record is filled in by ``finish``, in the same transaction.
        state[row["artifact_ref"]] = (
            row["version_number"], row["version_id"], row["schema_version"], None)


_ARTIFACT_FOLD = AppendOnlyFold(
    "core.latest_artifacts", "observability_artifact_version_index",
    "version_id, artifact_ref, version_number, schema_version, created_at",
    key="version_id", init=dict, reduce=_artifact_index_reduce,
)

_CONNECTOR_HEALTH_FOLD = AppendOnlyFold(
    "core.connector_health", "connector_source_health_events",
    "event_id, connector_profile_ref, state, created_at",
    key="event_id", init=dict, reduce=_keyed(lambda row: row["connector_profile_ref"]),
)
_CONNECTOR_INCIDENT_EVENT_FOLD = AppendOnlyFold(
    "core.connector_incident_events", "connector_incident_events",
    "event_id, incident_ref, state, created_at",
    key="event_id", init=dict, reduce=_keyed(lambda row: row["incident_ref"]),
)
_CONNECTOR_INCIDENT_FOLD = AppendOnlyFold(
    "core.connector_incidents", "connector_incidents",
    "incident_id, connector_profile_ref, connector_invocation_ref, reservation_ref,"
    " incident_type, severity, created_at",
    key="incident_id", init=list,
    reduce=_sorted_by(lambda row: (row["created_at"], row["incident_id"])),
)
_CONNECTOR_USAGE_FOLD = AppendOnlyFold(
    "core.connector_usage", "connector_usage_entries",
    "usage_entry_id, physical_attempt_ref, revision_number, measurement_status,"
    " metering_source, created_at,"
    " json_extract(record_json,'$.metrics') AS metrics",
    key="usage_entry_id", init=dict,
    reduce=_keyed_max(lambda row: row["physical_attempt_ref"],
                      lambda row: (row["revision_number"], row["created_at"])),
)
_CONNECTOR_COST_FOLD = AppendOnlyFold(
    "core.connector_cost", "connector_cost_entries",
    "cost_entry_id, usage_entry_ref, revision_number, amount_micros, currency,"
    " cost_status, created_at",
    key="cost_entry_id", init=dict,
    reduce=_keyed_max(lambda row: row["usage_entry_ref"],
                      lambda row: (row["revision_number"], row["created_at"])),
    cold_cover_index="connector_cost_entries_projection_fold_cover",
)
_CONNECTOR_SETTLEMENT_FOLD = AppendOnlyFold(
    "core.connector_settlements", "connector_quota_settlements",
    "settlement_id, reservation_ref, revision_number, state, actual_json, created_at",
    key="settlement_id", init=dict,
    reduce=_keyed_max(lambda row: row["reservation_ref"],
                      lambda row: (row["revision_number"], row["created_at"])),
)
_CONNECTOR_INVOCATION_FOLD = AppendOnlyFold(
    "core.connector_invocations", "connector_invocations",
    "connector_invocation_id, connector_profile_ref, call_spec_ref, created_at",
    key="connector_invocation_id", init=dict,
    reduce=lambda state, row: state.__setitem__(
        row["connector_invocation_id"],
        (sys.intern(row["connector_profile_ref"]), row["call_spec_ref"])),
    cold_cover_index="connector_invocations_projection_fold_cover",
)
_CONNECTOR_CALL_SPEC_FOLD = AppendOnlyFold(
    "core.connector_call_specs", "connector_call_specs",
    "call_spec_id, operation, created_at",
    key="call_spec_id", init=dict,
    reduce=lambda state, row: state.__setitem__(
        row["call_spec_id"], sys.intern(row["operation"])),
    cold_cover_index="connector_call_specs_projection_fold_cover",
)
_CONNECTOR_ATTEMPT_FOLD = AppendOnlyFold(
    "core.connector_attempts", "connector_physical_attempts",
    "physical_attempt_id, connector_invocation_ref, physical_attempt_number,"
    " reservation_ref, outcome, started_at, completed_at, retry_at, created_at",
    key="physical_attempt_id", init=list,
    reduce=_sorted_by(lambda row: (row["started_at"], row["physical_attempt_id"])),
    cold_cover_index="connector_physical_attempts_projection_fold_cover",
)
_CONNECTOR_RESERVATION_FOLD = AppendOnlyFold(
    "core.connector_reservations", "connector_quota_reservations",
    "reservation_id, quota_scope_ref, window_started_at, window_ends_at,"
    " reserved_json, policy_version_ref, created_at",
    key="reservation_id", init=list,
    reduce=_sorted_by(lambda row: (row["window_started_at"], row["reservation_id"])),
)


def _scheduled_work_orders(
    scheduler: sqlite3.Connection, scheduler_db: Path | str
) -> dict[str, dict[str, Any]]:
    """Every work order, as the projection needs it, read incrementally."""

    return _WORK_ORDER_FOLD.read(scheduler, source_identity(scheduler_db)).state


def _retire_terminal(formal: Mapping[str, Any]) -> Callable[[dict], None]:
    """Drop a work order's event/receipt state once it has a formal result.

    A formal result is final and ``_scheduler_state`` answers from it alone,
    so the latest event, receipt and lease of a finished work order are never
    read again: keeping them would hold the whole history in memory.
    """

    def finish(state: dict) -> None:
        retire_keys(state, formal)

    return finish


def _retire_terminal_leases(formal: Mapping[str, Any]) -> Callable[[dict], None]:
    def finish(state: dict) -> None:
        doomed = [key for key in state if key[0] in formal]
        for key in doomed:
            del state[key]

    return finish


def _load_scheduler(
    scheduler: sqlite3.Connection, identity: tuple[str, int, int]
) -> dict[str, Any]:
    """Every scheduler fact the projection needs, from one read snapshot."""

    work_orders = _WORK_ORDER_FOLD.read(scheduler, identity)
    formal = _FORMAL_RESULT_FOLD.read(scheduler, identity)
    events = _ATTEMPT_EVENT_FOLD.read(
        scheduler, identity, finish=_retire_terminal(formal.state))
    receipts = _RESULT_ENVELOPE_FOLD.read(
        scheduler, identity, finish=_retire_terminal(formal.state))
    leases = _LEASE_FOLD.read(
        scheduler, identity, finish=_retire_terminal_leases(formal.state))
    return {
        "scheduled": work_orders.state,
        "formal_results": formal.state,
        "latest_events": events.state,
        "latest_receipts": receipts.state,
        "latest_leases": leases.state,
        "watermark": {
            "scheduler_work_orders": [work_orders.count, work_orders.max_created],
            "scheduler_attempt_events": [events.count, events.max_created],
            "scheduler_leases": [leases.count, leases.max_created],
            "scheduler_formal_results": [formal.count, formal.max_created],
            "scheduler_result_envelopes": [receipts.count, receipts.max_created],
        },
    }


def _safe_token(value: Any, fallback: str) -> str:
    """Keep compact identifiers while refusing control characters and prose."""
    if not isinstance(value, str) or not value:
        return fallback
    cleaned = "".join(char for char in value if ord(char) >= 32).strip()
    return cleaned[:160] or fallback


def _duration_ms(started_at: Any, completed_at: Any) -> int | None:
    if completed_at is None:
        return None
    try:
        delta = _parse_time(completed_at, "invocation.completed_at") - _parse_time(
            started_at, "invocation.started_at"
        )
    except ProjectionSourceError:
        return None
    return max(0, int(delta.total_seconds() * 1000))


_CONNECTOR_METRICS = ("calls", "bytes", "records", "cost_micros")


def _connector_metrics(value: Any, name: str) -> dict[str, int]:
    if isinstance(value, str):
        value = _json(value, name)
    if not isinstance(value, Mapping) or set(value) != set(_CONNECTOR_METRICS):
        raise ProjectionSourceError(f"{name} has an invalid closed metric shape")
    result: dict[str, int] = {}
    for metric in _CONNECTOR_METRICS:
        amount = value[metric]
        if type(amount) is not int or amount < 0:
            raise ProjectionSourceError(f"{name}.{metric} must be non-negative integer")
        result[metric] = amount
    return result


class _Warnings:
    def __init__(self) -> None:
        self.items: list[str] = []
        self.partial = False

    def add(self, message: str, *, partial: bool = True) -> None:
        if message not in self.items:
            self.items.append(message)
        self.partial = self.partial or partial


class DashboardProjector:
    """Build one consistent-enough, watermarked read model from two stores."""

    def __init__(
        self,
        core_db: str | Path,
        scheduler_db: str | Path,
        *,
        capability_catalog_db: str | Path | None = None,
        model_router_db: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        self.core_db = Path(core_db)
        self.scheduler_db = Path(scheduler_db)
        self.capability_catalog_db = (
            None if capability_catalog_db is None else Path(capability_catalog_db)
        )
        self.model_router_db = None if model_router_db is None else Path(model_router_db)
        self.clock = clock or _utc_now
        self.cancel_requested = cancel_requested

    def project(self, projection_db: str | Path) -> dict[str, Any]:
        destination = Path(projection_db).resolve()
        sources = {self.core_db.resolve(), self.scheduler_db.resolve()}
        if self.capability_catalog_db is not None:
            sources.add(self.capability_catalog_db.resolve())
        if self.model_router_db is not None:
            sources.add(self.model_router_db.resolve())
        aliases_authority = destination in sources or (
            destination.exists()
            and any(source.exists() and os.path.samefile(destination, source) for source in sources)
        )
        if aliases_authority:
            raise DashboardProjectorError(
                "projection destination cannot be an authority database"
            )
        snapshot = self.build_snapshot()
        check_cancelled(self.cancel_requested)
        # Do not open the caller-controlled destination for mutation.  A
        # same-UID process could swap it for a hard link to an authority DB
        # after the alias check.  Build a private sibling and atomically replace
        # the directory entry; this unlinks a hostile link instead of writing
        # through it.
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            with ProjectionWriter(temporary, cancel_requested=self.cancel_requested) as writer:
                writer.replace(snapshot)
            sync_fd = os.open(temporary, os.O_RDONLY)
            try:
                os.fsync(sync_fd)
            finally:
                os.close(sync_fd)
            check_cancelled(self.cancel_requested)
            os.replace(temporary, destination)
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()
            for suffix in ("-journal", "-wal", "-shm"):
                temporary.with_name(temporary.name + suffix).unlink(missing_ok=True)
        return snapshot

    def build_snapshot(self) -> dict[str, Any]:
        check_cancelled(self.cancel_requested)
        try:
            return self._build_snapshot()
        except sqlite3.Error:
            check_cancelled(self.cancel_requested)
            raise

    def _build_snapshot(self) -> dict[str, Any]:
        as_of_dt = self.clock()
        as_of = _timestamp(as_of_dt)
        as_of_dt = as_of_dt.astimezone(timezone.utc)
        # One sink per section, merged at the end in the order the sections
        # always ran: the reads now finish (and release their snapshots)
        # before the in-memory sections run, and the owner should not see the
        # warnings reshuffle because of that.
        sections = [
            "sources", "graph", "unowned", "items", "invocations", "costs",
            "capabilities", "models", "agenda", "metadata", "connectors",
        ]
        sink = {name: _Warnings() for name in sections}

        with ExitStack() as stack:
            core = stack.enter_context(closing(_open_read_only(self.core_db, cancel_requested=self.cancel_requested)))
            scheduler = stack.enter_context(closing(_open_read_only(self.scheduler_db, cancel_requested=self.cancel_requested)))
            _require_tables(core, _CORE_TABLES, "Core/observability")
            _require_tables(scheduler, _SCHEDULER_TABLES, "Scheduler")
            # 2026-09-29: the scheduler snapshot used to stay open until the
            # whole round finished (25 min on the live controller), and no
            # checkpoint can pass a reader's snapshot, so the WAL grew and
            # every scheduler read slowed.  Its facts are read up front, from
            # one snapshot, and the snapshot ends before anything else runs.
            scheduler.execute("BEGIN")
            try:
                loaded = _load_scheduler(scheduler, source_identity(self.scheduler_db))
            finally:
                scheduler.execute("COMMIT")
            core.execute("BEGIN")
            core_identity = source_identity(self.core_db)

            catalog = self._optional_source(
                stack,
                self.capability_catalog_db,
                core,
                "capability_descriptor_versions",
                "Capability Catalog 不可用；能力状态仅按实际调用推导",
                sink["sources"],
                partial_tables=_METADATA_SOURCE_AUTHORITY_TABLES,
                cancel_requested=self.cancel_requested,
            )
            router = self._optional_source(
                stack,
                self.model_router_db,
                core,
                "model_endpoint_profile_versions",
                "Model Router 不可用；模型状态仅按实际调用推导",
                sink["sources"],
                cancel_requested=self.cancel_requested,
            )

            check_cancelled(self.cancel_requested)
            folded: dict[str, FoldRead] = {}
            workflows_read = folded["observability_workflow_versions"] = (
                _WORKFLOW_FOLD.read(core, core_identity))
            workflow_wires = _ordered_records(
                workflows_read.state, "observability_workflow_versions")
            links_read = folded["observability_work_order_links"] = (
                _LINK_FOLD.read(core, core_identity))
            link_wires = [
                _json(row["record_json"], "work-order link") for _, row in links_read.state
            ]
            usage_read = folded["observability_usage_entries"] = (
                _USAGE_FOLD.read(core, core_identity))
            usage_wires = _ordered_records(usage_read.state, "observability_usage_entries")
            latest_usage_ids = {wire["id"] for wire in usage_wires}
            cost_read = folded["observability_cost_entries"] = (
                _COST_FOLD.read(core, core_identity))
            cost_wires = [
                wire
                for wire in _ordered_records(cost_read.state, "observability_cost_entries")
                if wire["usage_entry_ref"] in latest_usage_ids
            ]
            artifact_wires = self._latest_artifact_records(core, core_identity)
            invocations_read = folded["model_invocations"] = (
                _INVOCATION_FOLD.read(core, core_identity))
            invocation_wires = [
                _json(row["invocation_json"], "model invocation")
                for _, row in invocations_read.state
            ]

            # Cheap and in memory, but ``_models`` needs the invocation rows
            # while the router is still open.
            check_cancelled(self.cancel_requested)
            workflows, membership, parents, sequences = self._workflow_graph(
                workflow_wires, link_wires, sink["graph"]
            )
            all_work_refs = set(loaded["scheduled"])
            all_work_refs.update(membership)
            all_work_refs.update(wire["work_order_ref"] for wire in invocation_wires)
            all_work_refs.update(wire["work_order_ref"] for wire in artifact_wires)
            self._assign_unowned_work(
                all_work_refs, workflows, membership, sink["unowned"], as_of)
            invocation_rows = self._invocations(
                invocation_wires, usage_wires, membership, sink["invocations"]
            )
            capability_rows = self._capabilities(
                catalog, invocation_wires, as_of_dt, sink["capabilities"]
            )
            model_rows = self._models(router, invocation_rows, sink["models"])
            agenda_overview, agenda_cycles, agenda_questions = self._agenda(
                core, sink["agenda"])
            metadata_sources = self._metadata_sources(catalog, sink["metadata"])
            connector_sources = self._connector_sources(
                core, core_identity, sink["connectors"], folded)
            check_cancelled(self.cancel_requested)
            watermark = self._watermark(
                core, loaded["watermark"], folded, catalog, router)
        # Every authority connection is closed here: what follows reads only
        # memory, and holds no snapshot while it does.

        (
            connector_operations,
            connector_attempts,
            connector_quota_windows,
            connector_incidents,
        ) = self._connector_rows(connector_sources)
        check_cancelled(self.cancel_requested)
        work_items, work_state = self._work_items(
            all_work_refs,
            workflows,
            membership,
            parents,
            sequences,
            loaded["scheduled"],
            loaded["latest_events"],
            loaded["formal_results"],
            loaded["latest_receipts"],
            loaded["latest_leases"],
            invocation_wires,
            usage_wires,
            artifact_wires,
            as_of_dt,
            sink["items"],
        )
        check_cancelled(self.cancel_requested)
        cost_rows = self._costs(cost_wires, usage_wires, membership, sink["costs"])
        check_cancelled(self.cancel_requested)
        artifact_rows = self._artifacts(artifact_wires, membership)
        check_cancelled(self.cancel_requested)
        workflow_rows = self._workflow_summaries(
            workflows, work_items, work_state, invocation_rows, artifact_rows
        )

        check_cancelled(self.cancel_requested)
        warnings = _Warnings()
        for name in sections:
            for item in sink[name].items:
                warnings.add(item, partial=False)
            warnings.partial = warnings.partial or sink[name].partial

        return {
            "metadata": {
                "schema_version": SCHEMA_VERSION,
                "as_of": as_of,
                "source_watermark": watermark,
                "build_state": "ready",
                "partial_data": warnings.partial,
                "warnings": warnings.items,
            },
            "workflow_summaries": workflow_rows,
            "work_items": work_items,
            "invocation_slices": invocation_rows,
            "cost_slices": cost_rows,
            "artifact_index": artifact_rows,
            "capability_status": capability_rows,
            "model_status": model_rows,
            "agenda_supervision": agenda_overview,
            "agenda_cycle_summaries": agenda_cycles,
            "agenda_questions": agenda_questions,
            "metadata_source_status": metadata_sources,
            "connector_operation_status": connector_operations,
            "connector_attempt_slices": connector_attempts,
            "connector_quota_windows": connector_quota_windows,
            "connector_incident_status": connector_incidents,
        }

    @staticmethod
    def _latest_artifact_records(
        core: sqlite3.Connection, identity: tuple[str, int, int]
    ) -> list[dict[str, Any]]:
        """Read the newest immutable artifact across both schema generations."""

        tables = _tables(core)
        if not {
            "observability_artifact_version_index",
            "observability_artifact_versions_v2",
        }.issubset(tables):
            return DashboardProjector._latest_records(
                core,
                "observability_artifact_versions",
                "artifact_ref",
                "version_number",
            )

        def fill(state: dict) -> None:
            # Keep the complete latest set and its original processing order.
            # One cold snapshot can contain >100k artifacts. Read their exact
            # immutable PKs in bounded batches instead of one query per row.
            pending = [(ref, entry) for ref, entry in state.items() if entry[3] is None]
            for offset in range(0, len(pending), 256):
                batch = pending[offset:offset + 256]
                records = {}
                for schema, table in (
                    (True, "observability_artifact_versions"),
                    (False, "observability_artifact_versions_v2"),
                ):
                    refs = list(dict.fromkeys(entry[1] for _, entry in batch
                                             if (entry[2] == "0.1") == schema))
                    if not refs:
                        continue
                    placeholders = ",".join("?" for _ in refs)
                    rows = core.execute(
                        f"SELECT version_id,record_json FROM {table} "
                        f"WHERE version_id IN ({placeholders})", refs,
                    ).fetchall()
                    records[schema] = {row["version_id"]: row for row in rows}
                for artifact_ref, entry in batch:
                    record = records.get(entry[2] == "0.1", {}).get(entry[1])
                    if record is None:
                        raise ProjectionSourceError(
                            "artifact generation index points to a missing authority row"
                        )
                    wire = _json(record["record_json"], "artifact version")
                    state[artifact_ref] = entry[:3] + (
                        {name: wire[name] for name in _ARTIFACT_FIELDS if name in wire},
                    )

        read = _ARTIFACT_FOLD.read(core, identity, finish=fill)
        return [read.state[ref][3] for ref in sorted(read.state)]

    @staticmethod
    def _agenda(
        core: sqlite3.Connection, warnings: _Warnings
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        if not _AGENDA_TABLES.issubset(_tables(core)):
            warnings.add("Agenda authority 不可用；监督视图为空")
            return [], [], []
        control = core.execute(
            "SELECT v.version_id,v.paused,v.reason FROM agenda_control_pointer p "
            "JOIN agenda_control_versions v ON v.version_id=p.version_id WHERE p.pointer_id=1"
        ).fetchone()
        policy = core.execute(
            "SELECT v.version_id,v.policy_json FROM agenda_policy_pointer p "
            "JOIN agenda_policy_versions v ON v.version_id=p.version_id WHERE p.pointer_id=1"
        ).fetchone()
        policy_json: dict[str, Any] = {}
        if policy is not None:
            policy_json = _json(policy["policy_json"], "agenda policy")

        latest_cycle_events = {
            row["cycle_id"]: row
            for row in core.execute(
                "SELECT e.* FROM agenda_cycle_events e JOIN ("
                " SELECT cycle_id,MAX(event_seq) AS event_seq FROM agenda_cycle_events GROUP BY cycle_id"
                ") x ON x.event_seq=e.event_seq"
            )
        }
        decisions = {
            row["cycle_id"]: row for row in core.execute("SELECT * FROM agenda_decisions")
        }
        outbox_by_decision: dict[str, sqlite3.Row] = {}
        attempt_counts: dict[str, int] = defaultdict(int)
        for row in core.execute(
            "SELECT m.message_id,m.payload_json,e.* FROM agenda_outbox_messages m "
            "JOIN agenda_outbox_events e ON e.event_seq=("
            " SELECT MAX(x.event_seq) FROM agenda_outbox_events x WHERE x.message_id=m.message_id"
            ")"
        ):
            payload = _json(row["payload_json"], "agenda outbox payload")
            decision_ref = payload.get("decision_ref")
            if isinstance(decision_ref, str):
                outbox_by_decision[decision_ref] = row
        for row in core.execute(
            "SELECT message_id,COUNT(*) AS attempts FROM agenda_outbox_events "
            "WHERE state='claimed' GROUP BY message_id"
        ):
            attempt_counts[row["message_id"]] = int(row["attempts"])

        latest_feedback: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
        for row in core.execute(
            "SELECT * FROM agenda_feedback ORDER BY created_at,feedback_id"
        ):
            subject = row["subject_ref"] or row["actor_ref"]
            latest_feedback[row["decision_id"]][subject] = row

        candidates_by_cycle: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in core.execute("SELECT * FROM agenda_candidates ORDER BY candidate_id"):
            candidates_by_cycle[row["cycle_id"]].append(row)

        cycle_rows: list[dict[str, Any]] = []
        question_rows: list[dict[str, Any]] = []
        all_label_counts = {"agree": 0, "disagree": 0, "partial": 0}
        delivered_cards = 0
        pending_deliveries = 0
        labeled_decisions = 0
        auto_accepted_decisions = 0
        failed_cycles = 0
        decided_cycles = 0

        cycles = list(core.execute("SELECT * FROM agenda_cycles ORDER BY created_at,cycle_id"))
        for cycle in cycles:
            event = latest_cycle_events.get(cycle["cycle_id"])
            state = "unknown" if event is None else event["state"]
            if state == "failed":
                failed_cycles += 1
            if state in {"decided", "delivered"}:
                decided_cycles += 1
            decision = decisions.get(cycle["cycle_id"])
            decision_ref = None if decision is None else decision["decision_id"]
            selected: list[str] = []
            deferred: list[str] = []
            rejected: list[str] = []
            breakdown: dict[str, Any] = {}
            if decision is not None:
                selected = json.loads(decision["selected_candidate_refs_json"])
                deferred = json.loads(decision["deferred_candidate_refs_json"])
                rejected = json.loads(decision["rejected_candidate_refs_json"])
                breakdown = json.loads(decision["score_breakdown_json"])
            delivery = None if decision_ref is None else outbox_by_decision.get(decision_ref)
            delivery_state = "not_created" if delivery is None else delivery["state"]
            if delivery_state == "delivered":
                delivered_cards += 1
            elif delivery is not None:
                pending_deliveries += 1
            feedback_by_subject = {} if decision_ref is None else latest_feedback.get(decision_ref, {})
            labels = [
                value for subject, value in feedback_by_subject.items()
                if subject.startswith("human:")
            ]
            auto_accept_count = int("automation:timeout" in feedback_by_subject)
            counts = {"agree": 0, "disagree": 0, "partial": 0}
            for label in labels:
                verdict = label["verdict"]
                if verdict in counts:
                    counts[verdict] += 1
                    all_label_counts[verdict] += 1
            if labels:
                labeled_decisions += 1
            if auto_accept_count:
                auto_accepted_decisions += 1
            nonzero = [name for name, count in counts.items() if count]
            feedback_state = (
                "unlabeled" if not nonzero and not auto_accept_count else
                "auto_accept_timeout" if not nonzero else
                nonzero[0] if len(nonzero) == 1 else "mixed"
            )
            updated_at = cycle["created_at"] if event is None else event["created_at"]
            cycle_rows.append({
                "cycle_ref": cycle["cycle_id"], "cycle_key": cycle["cycle_key"],
                "company_ref": cycle["company_ref"], "state": state,
                "decision_ref": decision_ref, "selected_count": len(selected),
                "deferred_count": len(deferred), "rejected_count": len(rejected),
                "delivery_state": delivery_state,
                "delivery_attempts": 0 if delivery is None else attempt_counts[delivery["message_id"]],
                "feedback_state": feedback_state, "agree_count": counts["agree"],
                "disagree_count": counts["disagree"], "partial_count": counts["partial"],
                "auto_accept_count": auto_accept_count,
                "created_at": cycle["created_at"], "updated_at": updated_at,
            })
            ranks = {candidate_ref: index + 1 for index, candidate_ref in enumerate(selected)}
            for candidate in candidates_by_cycle.get(cycle["cycle_id"], []):
                candidate_ref = candidate["candidate_id"]
                selection_state = (
                    "selected" if candidate_ref in selected else
                    "deferred" if candidate_ref in deferred else
                    "rejected" if candidate_ref in rejected or not candidate["valid"] else
                    "unranked"
                )
                score = breakdown.get(candidate_ref, {}).get("total")
                question_rows.append({
                    "candidate_ref": candidate_ref, "cycle_ref": cycle["cycle_id"],
                    "decision_ref": decision_ref, "selection_state": selection_state,
                    "selection_rank": ranks.get(candidate_ref),
                    "question": candidate["proposed_question"],
                    "answer_criteria": candidate["answer_criteria"],
                    "rationale": candidate["rationale"], "total_score": score,
                    "features_json": candidate["features_json"],
                })

        total_labels = sum(all_label_counts.values())
        overview = [{
            "singleton": 1,
            "paused": bool(control["paused"]) if control is not None else True,
            "pause_reason": control["reason"] if control is not None else "control unavailable",
            "policy_version_ref": None if policy is None else policy["version_id"],
            "cutover_enabled": bool(policy_json.get("cutover_enabled", False)),
            "total_cycles": len(cycles), "decided_cycles": decided_cycles,
            "failed_cycles": failed_cycles, "pending_deliveries": pending_deliveries,
            "delivered_cards": delivered_cards, "labeled_decisions": labeled_decisions,
            "auto_accepted_decisions": auto_accepted_decisions,
            "agreement_rate": None if total_labels == 0 else all_label_counts["agree"] / total_labels,
            "last_cycle_at": None if not cycles else cycles[-1]["created_at"],
        }]
        return overview, cycle_rows, question_rows

    @staticmethod
    def _optional_source(
        stack: ExitStack,
        path: Path | None,
        core: sqlite3.Connection,
        table: str,
        warning: str,
        warnings: _Warnings,
        *,
        partial_tables: frozenset[str] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> sqlite3.Connection | None:
        if path is None:
            available = _tables(core)
            if table in available:
                return core
            if partial_tables is not None and partial_tables & available:
                raise ProjectionSourceError(
                    f"optional authority lacks {table} but contains partial tables: "
                    f"{sorted(partial_tables & available)}"
                )
            warnings.add(warning)
            return None
        try:
            conn = stack.enter_context(closing(_open_read_only(path, cancel_requested=cancel_requested)))
        except ProjectionSourceError:
            warnings.add(warning)
            return None
        available = _tables(conn)
        if table not in available:
            if partial_tables is not None and partial_tables & available:
                raise ProjectionSourceError(
                    f"optional authority lacks {table} but contains partial tables: "
                    f"{sorted(partial_tables & available)}"
                )
            warnings.add(warning)
            return None
        conn.execute("BEGIN")
        return conn

    @staticmethod
    def _latest_records(
        conn: sqlite3.Connection, table: str, group_field: str, version_field: str
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            f"SELECT source.record_json FROM {table} AS source JOIN ("
            f" SELECT {group_field}, MAX({version_field}) AS latest_version FROM {table}"
            f" GROUP BY {group_field}"
            f") AS latest ON source.{group_field}=latest.{group_field}"
            f" AND source.{version_field}=latest.latest_version"
            f" ORDER BY source.created_at"
        ).fetchall()
        return [_json(row["record_json"], table) for row in rows]

    @staticmethod
    def _workflow_graph(
        workflow_wires: list[dict[str, Any]],
        links: list[dict[str, Any]],
        warnings: _Warnings,
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, str],
        dict[tuple[str, str], str | None],
        dict[tuple[str, str], int],
    ]:
        workflows = {wire["workflow_ref"]: wire for wire in workflow_wires}
        candidates: dict[str, set[str]] = defaultdict(set)
        parents: dict[tuple[str, str], str | None] = {}
        sequences: dict[tuple[str, str], int] = {}
        for workflow_ref, wire in workflows.items():
            for root in wire["root_work_order_refs"]:
                candidates[root].add(workflow_ref)
                parents[(workflow_ref, root)] = None
                sequences[(workflow_ref, root)] = 0
        for link in links:
            workflow_ref = link["workflow_ref"]
            if workflow_ref not in workflows:
                warnings.add("存在无法对应最新 WorkflowRunVersion 的任务关系")
                continue
            parent = link["parent_work_order_ref"]
            child = link["child_work_order_ref"]
            candidates[parent].add(workflow_ref)
            candidates[child].add(workflow_ref)
            parents.setdefault((workflow_ref, parent), None)
            sequences.setdefault((workflow_ref, parent), 0)
            parents[(workflow_ref, child)] = parent
            sequences[(workflow_ref, child)] = int(link["sequence"])
        membership: dict[str, str] = {}
        for work_ref, refs in candidates.items():
            chosen = sorted(refs)[0]
            membership[work_ref] = chosen
            if len(refs) > 1:
                warnings.add(
                    "同一 WorkOrder 出现在多个 workflow；当前 projection 只能展示一个归属"
                )
        return workflows, membership, parents, sequences

    @staticmethod
    def _assign_unowned_work(
        work_refs: set[str],
        workflows: dict[str, dict[str, Any]],
        membership: dict[str, str],
        warnings: _Warnings,
        as_of: str,
    ) -> None:
        unowned = sorted(work_refs - set(membership))
        if not unowned:
            return
        workflow_ref = "workflow:projection-unassigned"
        if workflow_ref in workflows:
            workflow_ref = "workflow:projection-unassigned-system"
        workflows[workflow_ref] = {
            "id": workflow_ref,
            "workflow_ref": workflow_ref,
            "title": "未归入工作流的任务",
            "objective": "等待补齐 WorkflowRun/WorkOrderLink 归属",
            "created_at": as_of,
        }
        for work_ref in unowned:
            membership[work_ref] = workflow_ref
        warnings.add(f"有 {len(unowned)} 个任务没有 WorkflowRun/WorkOrderLink 归属")

    def _work_items(
        self,
        work_refs: set[str],
        workflows: Mapping[str, Mapping[str, Any]],
        membership: Mapping[str, str],
        parents: Mapping[tuple[str, str], str | None],
        sequences: Mapping[tuple[str, str], int],
        scheduled: Mapping[str, Mapping[str, Any]],
        latest_events: Mapping[str, sqlite3.Row],
        formal_results: Mapping[str, sqlite3.Row],
        latest_receipts: Mapping[str, sqlite3.Row],
        latest_leases: Mapping[tuple[str, int], sqlite3.Row],
        invocations: list[dict[str, Any]],
        usages: list[dict[str, Any]],
        artifacts: list[dict[str, Any]],
        as_of: datetime,
        warnings: _Warnings,
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        inv_by_work: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for wire in invocations:
            inv_by_work[wire["work_order_ref"]].append(wire)
        usage_by_inv = {wire["invocation_ref"]: wire for wire in usages}
        artifact_by_work: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for wire in artifacts:
            artifact_by_work[wire["work_order_ref"]].append(wire)
        rows: list[dict[str, Any]] = []
        states: dict[str, dict[str, Any]] = {}
        missing_usage = 0
        for work_ref in work_refs:
            workflow_ref = membership[work_ref]
            scheduled_entry = scheduled.get(work_ref)
            if scheduled_entry is None:
                state = {
                    "display_status": "调度记录缺失",
                    "source_state": "missing",
                    "source_ref": f"work-order:{work_ref}",
                    "status_reason": "workflow 或 invocation 引用了 Scheduler 中不存在的任务",
                    "attempt_number": 1,
                    "latest_result_ref": None,
                    "latest_error_summary": None,
                    "updated_at": workflows[workflow_ref]["created_at"],
                    "running": False,
                    "completed": False,
                    "failed": False,
                }
                work_wire: Mapping[str, Any] = {}
                created_at = workflows[workflow_ref]["created_at"]
                warnings.add("存在没有 Scheduler authority 记录的 WorkOrder 引用")
            else:
                work_wire = scheduled_entry["wire"]
                created_at = scheduled_entry["created_at"]
                state = self._scheduler_state(
                    work_ref,
                    latest_events.get(work_ref),
                    formal_results.get(work_ref),
                    latest_receipts.get(work_ref),
                    latest_leases,
                    as_of,
                    warnings,
                )
            work_invocations = inv_by_work.get(work_ref, [])
            known_totals = []
            models = set()
            for invocation in work_invocations:
                models.add((invocation["provider"], invocation["model"]))
                usage = usage_by_inv.get(invocation["id"])
                if usage is None:
                    missing_usage += 1
                elif usage["total_tokens"] is not None:
                    known_totals.append(usage["total_tokens"])
            capabilities = work_wire.get("requested_capabilities", [])
            capability_label = "、".join(_safe_token(item, "能力") for item in capabilities[:3])
            rows.append(
                {
                    "work_order_ref": work_ref,
                    "workflow_ref": workflow_ref,
                    "parent_work_order_ref": self._display_parent(
                        work_ref, workflow_ref, membership, parents, warnings
                    ),
                    "sequence": int(sequences.get((workflow_ref, work_ref), 0)),
                    "title": f"任务 · {capability_label}" if capability_label else "研究任务",
                    "question": f"执行 {capability_label}" if capability_label else "执行已登记任务",
                    "display_status": state["display_status"],
                    "source_state": state["source_state"],
                    "source_ref": state["source_ref"],
                    "status_reason": state["status_reason"],
                    "attempt_number": state["attempt_number"],
                    "model_count": len(models),
                    "total_tokens": sum(known_totals) if known_totals else None,
                    "artifact_count": len(artifact_by_work.get(work_ref, [])),
                    "latest_result_ref": state["latest_result_ref"],
                    "latest_error_summary": state["latest_error_summary"],
                    "created_at": created_at,
                    "updated_at": state["updated_at"],
                }
            )
            states[work_ref] = state
        if missing_usage:
            warnings.add(f"有 {missing_usage} 次模型调用没有 UsageEntry")
        rows.sort(key=lambda row: (self._depth(row, parents), row["sequence"], row["work_order_ref"]))
        return rows, states

    @staticmethod
    def _display_parent(
        work_ref: str,
        workflow_ref: str,
        membership: Mapping[str, str],
        parents: Mapping[tuple[str, str], str | None],
        warnings: _Warnings,
    ) -> str | None:
        parent = parents.get((workflow_ref, work_ref))
        if parent is not None and membership.get(parent) != workflow_ref:
            warnings.add(
                "任务父节点被投影到另一个 workflow；当前任务按 root 展示"
            )
            return None
        return parent

    @staticmethod
    def _depth(
        row: Mapping[str, Any], parents: Mapping[tuple[str, str], str | None]
    ) -> int:
        workflow_ref = row["workflow_ref"]
        node = row["work_order_ref"]
        depth = 0
        seen = set()
        while (workflow_ref, node) in parents and parents[(workflow_ref, node)] is not None:
            if node in seen:
                return 10_000
            seen.add(node)
            node = parents[(workflow_ref, node)]  # type: ignore[assignment]
            depth += 1
        return depth

    @staticmethod
    def _scheduler_state(
        work_ref: str,
        event: sqlite3.Row | None,
        formal: sqlite3.Row | None,
        receipt: sqlite3.Row | None,
        leases: Mapping[tuple[str, int], sqlite3.Row],
        as_of: datetime,
        warnings: _Warnings,
    ) -> dict[str, Any]:
        if formal is not None:
            source_state = formal["terminal_state"]
            failed = source_state == "failed"
            return {
                "display_status": "已失败" if failed else "已完成",
                "source_state": source_state,
                "source_ref": formal["result_record_id"],
                "status_reason": "Scheduler 已登记正式失败结果" if failed else "Scheduler 已登记正式结果",
                "attempt_number": int(formal["attempt_number"]),
                "latest_result_ref": formal["result_envelope_id"],
                "latest_error_summary": "任务返回错误；详细内容仅在权威日志中查看" if failed else None,
                "updated_at": formal["created_at"],
                "running": False,
                "completed": not failed,
                "failed": failed,
            }
        if event is None:
            warnings.add("Scheduler WorkOrder 缺少 attempt event")
            return {
                "display_status": "调度状态缺失",
                "source_state": "missing",
                "source_ref": f"work-order:{work_ref}",
                "status_reason": "Scheduler 未记录当前 attempt state",
                "attempt_number": 1,
                "latest_result_ref": None,
                "latest_error_summary": None,
                "updated_at": _timestamp(as_of),
                "running": False,
                "completed": False,
                "failed": False,
            }
        state = event["state"]
        attempt = int(event["attempt_number"])
        source_ref = event["event_id"]
        latest_result_ref = receipt["result_envelope_id"] if receipt is not None else None
        error_summary = None
        if state == "leased":
            lease = leases.get((work_ref, attempt))
            if lease is None:
                warnings.add("存在 leased 状态但找不到对应 lease revision")
                display, reason, running = "调度状态缺失", "leased event 没有对应租约", False
            elif _parse_time(lease["expires_at"], "lease.expires_at") <= as_of:
                warnings.add(f"任务 {work_ref} 的租约已过期，但 Scheduler 尚未 sweep")
                display = "等待调度回收"
                reason = "租约已过期，但 Scheduler 尚未追加 expired/ready 事件"
                running = False
                source_ref = lease["lease_revision_id"]
            else:
                display, reason, running = "执行中", "worker 持有有效租约", True
                source_ref = lease["lease_revision_id"]
        elif state == "ready":
            display, reason, running = "等待执行", "任务已入队，等待 worker 领取", False
        elif state in {"retryable", "expired"}:
            display, reason, running = "等待重试", "上一 attempt 未完成，等待 Scheduler 重试", False
        elif state == "failed":
            display, reason, running = "已失败", "Scheduler 已登记终态失败", False
            error_summary = "任务失败；详细内容仅在权威日志中查看"
        elif state == "succeeded":
            display, reason, running = "结果待确认", "succeeded event 缺少 formal result", False
            warnings.add("存在 succeeded event 但缺少 formal result")
        else:  # Schema should make this unreachable, but fail honest if corrupted.
            display, reason, running = "未知状态", "Scheduler state 无法识别", False
            warnings.add("存在无法识别的 Scheduler state")
        return {
            "display_status": display,
            "source_state": state,
            "source_ref": source_ref,
            "status_reason": reason,
            "attempt_number": attempt,
            "latest_result_ref": latest_result_ref,
            "latest_error_summary": error_summary,
            "updated_at": event["created_at"],
            "running": running,
            "completed": False,
            "failed": state == "failed",
        }

    @staticmethod
    def _invocations(
        invocations: list[dict[str, Any]],
        usages: list[dict[str, Any]],
        membership: Mapping[str, str],
        warnings: _Warnings,
    ) -> list[dict[str, Any]]:
        usage_by_inv = {wire["invocation_ref"]: wire for wire in usages}
        rows = []
        for invocation in invocations:
            usage = usage_by_inv.get(invocation["id"])
            work_ref = invocation["work_order_ref"]
            workflow_ref = membership.get(work_ref)
            if usage is not None and usage["workflow_ref"] not in {None, workflow_ref}:
                warnings.add("UsageEntry 的 workflow 归属与 WorkOrderLink 不一致")
            duration = usage["duration_ms"] if usage is not None else None
            if duration is None:
                duration = _duration_ms(invocation["started_at"], invocation["completed_at"])
            rows.append(
                {
                    "invocation_ref": invocation["id"],
                    "workflow_ref": workflow_ref,
                    "work_order_ref": work_ref,
                    "provider": invocation["provider"],
                    "model": invocation["model"],
                    "model_family": invocation["model_family"],
                    "profile_ref": invocation["profile_ref"],
                    "runtime_ref": invocation["runtime_ref"],
                    "capability": invocation["capability"],
                    "granularity": invocation["granularity"],
                    "started_at": invocation["started_at"],
                    "completed_at": invocation["completed_at"],
                    "duration_ms": duration,
                    "input_tokens": None if usage is None else usage["input_tokens"],
                    "output_tokens": None if usage is None else usage["output_tokens"],
                    "reasoning_tokens": None if usage is None else usage["reasoning_tokens"],
                    "cache_read_tokens": None if usage is None else usage["cache_read_tokens"],
                    "cache_write_tokens": None if usage is None else usage["cache_write_tokens"],
                    "total_tokens": None if usage is None else usage["total_tokens"],
                    "metering_source": "unknown" if usage is None else usage["metering_source"],
                    "measurement_status": "unavailable" if usage is None else usage["measurement_status"],
                }
            )
        return rows

    @staticmethod
    def _costs(
        costs: list[dict[str, Any]],
        usages: list[dict[str, Any]],
        membership: Mapping[str, str],
        warnings: _Warnings,
    ) -> list[dict[str, Any]]:
        usage_by_id = {wire["id"]: wire for wire in usages}
        rows = []
        unpriced = 0
        for cost in costs:
            usage = usage_by_id.get(cost["usage_entry_ref"])
            if usage is None:
                warnings.add("CostEntry 无法对应 latest UsageEntry")
                continue
            refs = cost["price_rate_refs"]
            price_ref = None if not refs else refs[0] if len(refs) == 1 else json.dumps(
                refs, ensure_ascii=False, separators=(",", ":")
            )
            if cost["cost_status"] == "unpriced":
                unpriced += 1
            rows.append(
                {
                    "cost_entry_ref": cost["id"],
                    "invocation_ref": usage["invocation_ref"],
                    "workflow_ref": membership.get(usage["work_order_ref"]),
                    "work_order_ref": usage["work_order_ref"],
                    "amount_micros": cost["amount_micros"],
                    "currency": cost["currency"],
                    "cost_status": cost["cost_status"],
                    "price_rate_ref": price_ref,
                    "created_at": cost["created_at"],
                }
            )
        if unpriced:
            warnings.add(f"有 {unpriced} 条 latest CostEntry 尚未定价")
        latest_cost_usage_refs = {cost["usage_entry_ref"] for cost in costs}
        missing_cost = sum(
            1 for usage in usages if usage["id"] not in latest_cost_usage_refs
        )
        if missing_cost:
            warnings.add(
                f"有 {missing_cost} 条 latest UsageEntry 没有 CostEntry（应显式记为 unpriced 或 waived）"
            )
        return rows

    @staticmethod
    def _artifacts(
        artifacts: list[dict[str, Any]], membership: Mapping[str, str]
    ) -> list[dict[str, Any]]:
        return [
            {
                "artifact_ref": wire["artifact_ref"],
                "workflow_ref": membership.get(wire["work_order_ref"]),
                "work_order_ref": wire["work_order_ref"],
                "title": wire["title"],
                "kind": wire["kind"],
                "media_type": wire["media_type"],
                "size_bytes": wire["size_bytes"],
                "content_hash": wire["artifact_content_hash"],
                "access_class": wire["access_class"],
                "preview_status": wire["preview_status"],
                "producer_execution_ref": wire.get(
                    "producer_execution_ref", wire.get("producer_invocation_ref")
                ),
                "created_at": wire["created_at"],
            }
            for wire in artifacts
        ]

    @staticmethod
    def _workflow_summaries(
        workflows: Mapping[str, Mapping[str, Any]],
        work_items: list[dict[str, Any]],
        states: Mapping[str, Mapping[str, Any]],
        invocations: list[dict[str, Any]],
        artifacts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        work_by_flow: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in work_items:
            work_by_flow[row["workflow_ref"]].append(row)
        inv_by_flow: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in invocations:
            inv_by_flow[row["workflow_ref"]].append(row)
        artifact_counts: dict[str, int] = defaultdict(int)
        for row in artifacts:
            artifact_counts[row["workflow_ref"]] += 1
        result = []
        for workflow_ref, wire in workflows.items():
            items = work_by_flow.get(workflow_ref, [])
            item_states = [states[row["work_order_ref"]] for row in items]
            if not items:
                display, source_state, reason = "尚未安排任务", "empty", "WorkflowRun 没有任务"
                source_ref = wire["id"]
            else:
                failed = next((item for item in item_states if item["failed"]), None)
                expired = next(
                    (item for item in item_states if item["display_status"] == "等待调度回收"),
                    None,
                )
                running = next((item for item in item_states if item["running"]), None)
                missing = next((item for item in item_states if item["source_state"] == "missing"), None)
                waiting_retry = next(
                    (item for item in item_states if item["source_state"] in {"retryable", "expired"}),
                    None,
                )
                ready = next((item for item in item_states if item["source_state"] == "ready"), None)
                if failed:
                    chosen, display = failed, "部分失败"
                elif expired:
                    chosen, display = expired, "等待调度回收"
                elif running:
                    chosen, display = running, "执行中"
                elif missing:
                    chosen, display = missing, "状态不完整"
                elif all(item["completed"] for item in item_states):
                    chosen, display = item_states[0], "已完成"
                elif waiting_retry:
                    chosen, display = waiting_retry, "等待重试"
                else:
                    chosen, display = ready or item_states[0], "等待执行"
                source_state = chosen["source_state"]
                source_ref = chosen["source_ref"]
                reason = chosen["status_reason"]
            known_tokens = [
                row["total_tokens"]
                for row in inv_by_flow.get(workflow_ref, [])
                if row["total_tokens"] is not None
            ]
            recent = max(
                [wire["created_at"], *(row["updated_at"] for row in items)]
            )
            result.append(
                {
                    "workflow_ref": workflow_ref,
                    "title": wire["title"],
                    "objective": wire["objective"],
                    "display_status": display,
                    "source_state": source_state,
                    "source_ref": source_ref,
                    "status_reason": reason,
                    "total_tasks": len(items),
                    "completed_tasks": sum(1 for item in item_states if item["completed"]),
                    "running_tasks": sum(1 for item in item_states if item["running"]),
                    "failed_tasks": sum(1 for item in item_states if item["failed"]),
                    "total_tokens": sum(known_tokens) if known_tokens else None,
                    "artifact_count": artifact_counts.get(workflow_ref, 0),
                    "recent_activity": recent,
                }
            )
        return result

    @staticmethod
    def _capabilities(
        catalog: sqlite3.Connection | None,
        invocations: list[dict[str, Any]],
        as_of: datetime,
        warnings: _Warnings,
    ) -> list[dict[str, Any]]:
        if catalog is not None and {
            "capability_current",
            "capability_descriptor_versions",
        }.issubset(_tables(catalog)):
            rows = []
            for row in catalog.execute(
                "SELECT current.capability_id,current.revision_ref,versions.descriptor_json "
                "FROM capability_current AS current JOIN capability_descriptor_versions AS versions "
                "ON versions.revision_ref=current.revision_ref ORDER BY current.capability_id"
            ):
                wire = _json(row["descriptor_json"], "capability descriptor")
                eligibility = wire["eligibility"]
                state = eligibility["state"]
                valid_until = eligibility.get("valid_until")
                if valid_until is not None and _parse_time(
                    valid_until, "capability.valid_until"
                ) <= as_of:
                    state = "expired"
                    warnings.add("Capability Catalog 含已过期的 current descriptor", partial=False)
                rows.append(
                    {
                        "capability_id": row["capability_id"],
                        "label": wire["label"],
                        "kind": wire["kind"],
                        "source_type": wire["source"]["type"],
                        "eligibility_state": state,
                        "active_revision_ref": row["revision_ref"],
                        "decision_state": "catalog_current",
                        "updated_at": wire["created_at"],
                    }
                )
            if rows or not invocations:
                return rows
            warnings.add("Capability Catalog 为空；能力状态按实际调用推导")
        observed: dict[str, dict[str, Any]] = {}
        for wire in invocations:
            capability = wire["capability"]
            row = observed.setdefault(
                capability,
                {
                    "capability_id": capability,
                    "label": capability,
                    "kind": "observed",
                    "source_type": capability.split(":", 1)[0] if ":" in capability else "runtime",
                    "eligibility_state": "observed",
                    "active_revision_ref": None,
                    "decision_state": "unknown",
                    "updated_at": wire["created_at"],
                },
            )
            row["updated_at"] = max(row["updated_at"], wire["created_at"])
        return list(observed.values())

    @staticmethod
    def _models(
        router: sqlite3.Connection | None,
        invocations: list[dict[str, Any]],
        warnings: _Warnings,
    ) -> list[dict[str, Any]]:
        totals: dict[tuple[str, str], int] = defaultdict(int)
        last_used: dict[tuple[str, str], str] = {}
        for row in invocations:
            key = (row["provider"], row["model"])
            if row["total_tokens"] is not None:
                totals[key] += row["total_tokens"]
            last_used[key] = max(last_used.get(key, row["started_at"]), row["started_at"])
        if router is not None and "model_endpoint_profile_versions" in _tables(router):
            profiles = router.execute(
                "SELECT source.profile_json FROM model_endpoint_profile_versions AS source JOIN ("
                " SELECT profile_id,MAX(version) AS latest_version FROM model_endpoint_profile_versions GROUP BY profile_id"
                ") AS latest ON source.profile_id=latest.profile_id AND source.version=latest.latest_version "
                "ORDER BY source.profile_id"
            ).fetchall()
            if profiles:
                warnings.add(
                    "Model Router 不保存真实凭据状态；auth_state 显示 unknown",
                    partial=True,
                )
                result = []
                for row in profiles:
                    wire = _json(row["profile_json"], "model profile")
                    key = (wire["provider"], wire["model"])
                    result.append(
                        {
                            "profile_ref": wire["id"],
                            "provider": wire["provider"],
                            "model": wire["model"],
                            "model_family": wire["family"],
                            "availability": wire["availability"]["state"],
                            "auth_state": "unknown",
                            "capabilities_json": json.dumps(
                                wire["capabilities"], ensure_ascii=False, separators=(",", ":")
                            ),
                            "context_window": wire["context"]["max_context_tokens"],
                            "cost_class": "configured" if wire.get("cost") else "unknown",
                            "last_used_at": last_used.get(key),
                            "total_tokens": totals.get(key) if key in totals else None,
                        }
                    )
                return result
            warnings.add("Model Router 为空；模型状态按实际调用推导")
        observed: dict[str, dict[str, Any]] = {}
        for row in invocations:
            profile_ref = row["profile_ref"]
            item = observed.setdefault(
                profile_ref,
                {
                    "profile_ref": profile_ref,
                    "provider": row["provider"],
                    "model": row["model"],
                    "model_family": row["model_family"],
                    "availability": "observed",
                    "auth_state": "unknown",
                    "capabilities_json": "[]",
                    "context_window": None,
                    "cost_class": "unknown",
                    "last_used_at": row["started_at"],
                    "total_tokens": 0,
                    "_capabilities": set(),
                    "_known_tokens": False,
                },
            )
            item["last_used_at"] = max(item["last_used_at"], row["started_at"])
            item["_capabilities"].add(row["capability"])
            if row["total_tokens"] is not None:
                item["total_tokens"] += row["total_tokens"]
                item["_known_tokens"] = True
        result = []
        for item in observed.values():
            item["capabilities_json"] = json.dumps(
                sorted(item.pop("_capabilities")), ensure_ascii=False, separators=(",", ":")
            )
            if not item.pop("_known_tokens"):
                item["total_tokens"] = None
            result.append(item)
        return result

    @staticmethod
    def _metadata_sources(
        catalog: sqlite3.Connection | None, warnings: _Warnings
    ) -> list[dict[str, Any]]:
        if catalog is None:
            return []
        available = _tables(catalog)
        present = _METADATA_SOURCE_AUTHORITY_TABLES & available
        if not present:
            warnings.add("Metadata source authority 尚未升级到 snapshot wire 0.2")
            return []
        missing = _METADATA_SOURCE_AUTHORITY_TABLES - available
        if missing:
            raise ProjectionSourceError(
                f"Metadata source authority is missing table(s): {sorted(missing)}"
            )

        active_row = catalog.execute(
            "SELECT source_instance_ref FROM external_capability_active_source "
            "WHERE singleton=1"
        ).fetchone()
        active_ref = None if active_row is None else active_row["source_instance_ref"]
        heads = {
            row["source_instance_ref"]: row
            for row in catalog.execute(
                "SELECT source_instance_ref,catalog_generation,snapshot_ref,"
                "snapshot_hash,updated_at FROM external_capability_source_heads"
            )
        }
        latest_ingest = {
            row["source_instance_ref"]: row
            for row in catalog.execute(
                "SELECT * FROM (SELECT source.source_instance_ref,source.outcome,"
                "source.created_at,ROW_NUMBER() OVER (PARTITION BY source_instance_ref "
                "ORDER BY source.rowid DESC) AS projection_rank "
                "FROM external_capability_snapshot_ingest_events source) "
                "WHERE projection_rank=1"
            )
        }
        latest_reject = {
            row["source_instance_ref"]: row
            for row in catalog.execute(
                "SELECT * FROM (SELECT source.source_instance_ref,source.outcome,"
                "source.created_at,ROW_NUMBER() OVER (PARTITION BY source_instance_ref "
                "ORDER BY source.rowid DESC) AS projection_rank "
                "FROM external_capability_snapshot_ingest_events source "
                "WHERE source.outcome NOT IN ('accepted','duplicate')) "
                "WHERE projection_rank=1"
            )
        }
        source_refs = {
            row[0]
            for row in catalog.execute(
                "SELECT source_instance_ref FROM external_capability_source_registrations"
            )
        }
        source_refs.update(latest_ingest)
        rows: list[dict[str, Any]] = []
        for source_ref in sorted(source_refs):
            head = heads.get(source_ref)
            ingest = latest_ingest.get(source_ref)
            rejected = latest_reject.get(source_ref)
            active = source_ref == active_ref
            freshness = (
                "current" if active and head is not None else
                "awaiting_first_snapshot" if active else
                "superseded"
            )
            rows.append(
                {
                    "source_instance_ref": source_ref,
                    "active": active,
                    "catalog_generation": (
                        None if head is None else int(head["catalog_generation"])
                    ),
                    "snapshot_ref": None if head is None else head["snapshot_ref"],
                    "snapshot_hash": None if head is None else head["snapshot_hash"],
                    "head_updated_at": None if head is None else head["updated_at"],
                    "freshness_state": freshness,
                    "latest_ingest_outcome": (
                        None if ingest is None else ingest["outcome"]
                    ),
                    "latest_ingest_at": (
                        None if ingest is None else ingest["created_at"]
                    ),
                    "latest_reject_outcome": (
                        None if rejected is None else rejected["outcome"]
                    ),
                    "latest_reject_at": (
                        None if rejected is None else rejected["created_at"]
                    ),
                }
            )
        return rows

    @staticmethod
    def _connector_sources(
        core: sqlite3.Connection,
        identity: tuple[str, int, int],
        warnings: _Warnings,
        folded: dict[str, FoldRead],
    ) -> dict[str, Any] | None:
        """Every connector fact the projection needs, read inside the snapshot.

        Each table is an append-only fold; the one small, versioned table
        (profiles, rate policies) is still read whole.
        """

        available = _tables(core)
        present = _CONNECTOR_AUTHORITY_TABLES & available
        if not present:
            warnings.add("Connector authority 尚未进入 Core；connector 投影为空")
            return None
        missing = _CONNECTOR_AUTHORITY_TABLES - available
        if missing:
            raise ProjectionSourceError(
                f"Connector authority is missing table(s): {sorted(missing)}"
            )
        loaded: dict[str, Any] = {}
        for name, fold in (
            ("connector_source_health_events", _CONNECTOR_HEALTH_FOLD),
            ("connector_incident_events", _CONNECTOR_INCIDENT_EVENT_FOLD),
            ("connector_incidents", _CONNECTOR_INCIDENT_FOLD),
            ("connector_usage_entries", _CONNECTOR_USAGE_FOLD),
            ("connector_cost_entries", _CONNECTOR_COST_FOLD),
            ("connector_quota_settlements", _CONNECTOR_SETTLEMENT_FOLD),
            ("connector_invocations", _CONNECTOR_INVOCATION_FOLD),
            ("connector_call_specs", _CONNECTOR_CALL_SPEC_FOLD),
            ("connector_physical_attempts", _CONNECTOR_ATTEMPT_FOLD),
            ("connector_quota_reservations", _CONNECTOR_RESERVATION_FOLD),
        ):
            read = folded[name] = fold.read(core, identity)
            loaded[name] = read.state
        loaded["profiles"] = core.execute(
            "SELECT source.record_json FROM connector_profile_versions source JOIN ("
            " SELECT connector_ref,MAX(version_number) AS latest_version "
            " FROM connector_profile_versions GROUP BY connector_ref"
            ") latest ON latest.connector_ref=source.connector_ref "
            "AND latest.latest_version=source.version_number "
            "ORDER BY source.connector_ref"
        ).fetchall()
        loaded["policy_currency"] = {
            row["policy_version_id"]: row["quota_currency"]
            for row in core.execute(
                "SELECT policy_version_id,quota_currency FROM connector_rate_policy_versions"
            )
        }
        return loaded

    @staticmethod
    def _connector_rows(
        loaded: dict[str, Any] | None,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        if loaded is None:
            return [], [], [], []
        latest_health = loaded["connector_source_health_events"]
        latest_incident_events = loaded["connector_incident_events"]
        incident_rows: list[dict[str, Any]] = []
        blocking_counts: dict[str, int] = defaultdict(int)
        for _, incident in loaded["connector_incidents"]:
            event = latest_incident_events.get(incident["incident_id"])
            if event is None:
                raise ProjectionSourceError("connector incident lacks an authority event")
            if event["state"] == "opened" and incident["severity"] == "blocking":
                blocking_counts[incident["connector_profile_ref"]] += 1
            incident_rows.append(
                {
                    "incident_ref": incident["incident_id"],
                    "connector_profile_ref": incident["connector_profile_ref"],
                    "connector_invocation_ref": incident["connector_invocation_ref"],
                    "reservation_ref": incident["reservation_ref"],
                    "incident_type": incident["incident_type"],
                    "severity": incident["severity"],
                    "state": event["state"],
                    "opened_at": incident["created_at"],
                    "updated_at": event["created_at"],
                }
            )

        operation_rows: list[dict[str, Any]] = []
        for row in loaded["profiles"]:
            profile = _json(row["record_json"], "connector profile")
            source = profile["source_identity"]
            health = latest_health.get(profile["id"])
            health_state = None if health is None else health["state"]
            circuit_state = {
                None: "unknown",
                "open_circuit": "open",
                "recovered": "recovering",
                "healthy": "closed",
                "degraded": "closed",
            }.get(health_state)
            if circuit_state is None:
                raise ProjectionSourceError("connector health state is invalid")
            for operation in sorted(profile["allowed_operations"]):
                operation_rows.append(
                    {
                        "connector_profile_ref": profile["id"],
                        "operation": _safe_token(operation, "unknown-operation"),
                        "connector_ref": profile["connector_ref"],
                        "profile_version": int(profile["version"]),
                        "capability_id": profile["capability_id"],
                        "source_ref": _safe_token(source["source_ref"], "unknown-source"),
                        "source_type": _safe_token(source["source_type"], "unknown"),
                        "source_version": _safe_token(source["source_version"], "unknown"),
                        "auth_mode": _safe_token(profile["auth_mode"], "unknown"),
                        "health_state": health_state,
                        "health_updated_at": (
                            None if health is None else health["created_at"]
                        ),
                        "circuit_state": circuit_state,
                        "open_blocking_incidents": blocking_counts.get(profile["id"], 0),
                    }
                )

        latest_usage = loaded["connector_usage_entries"]
        latest_cost = loaded["connector_cost_entries"]
        latest_settlement = loaded["connector_quota_settlements"]
        connector_invocations = loaded["connector_invocations"]
        call_specs = loaded["connector_call_specs"]

        attempt_rows: list[dict[str, Any]] = []
        for _, attempt in loaded["connector_physical_attempts"]:
            # The inner join the SQL made: an invocation whose call spec is
            # missing is no authority at all.
            invocation = connector_invocations.get(attempt["connector_invocation_ref"])
            operation = None if invocation is None else call_specs.get(invocation[1])
            if operation is None:
                raise ProjectionSourceError("connector attempt lacks invocation authority")
            authority = {"connector_profile_ref": invocation[0], "operation": operation}
            usage = latest_usage.get(attempt["physical_attempt_id"])
            usage_metrics = None
            cost = None
            if usage is not None:
                usage_metrics = _connector_metrics(
                    usage["metrics"], "connector usage metrics"
                )
                cost = latest_cost.get(usage["usage_entry_id"])
            settlement = latest_settlement.get(attempt["reservation_ref"])
            actual = (
                None
                if settlement is None
                else _connector_metrics(settlement["actual_json"], "quota settlement")
            )
            attempt_rows.append(
                {
                    "physical_attempt_ref": attempt["physical_attempt_id"],
                    "connector_invocation_ref": attempt["connector_invocation_ref"],
                    "connector_profile_ref": authority["connector_profile_ref"],
                    "operation": authority["operation"],
                    "physical_attempt_number": int(attempt["physical_attempt_number"]),
                    "outcome": attempt["outcome"],
                    "started_at": attempt["started_at"],
                    "completed_at": attempt["completed_at"],
                    "retry_at": attempt["retry_at"],
                    "usage_entry_ref": None if usage is None else usage["usage_entry_id"],
                    "usage_revision": None if usage is None else int(usage["revision_number"]),
                    "measurement_status": (
                        None if usage is None else usage["measurement_status"]
                    ),
                    "metering_source": None if usage is None else usage["metering_source"],
                    "usage_calls": None if usage_metrics is None else usage_metrics["calls"],
                    "usage_bytes": None if usage_metrics is None else usage_metrics["bytes"],
                    "usage_records": None if usage_metrics is None else usage_metrics["records"],
                    "cost_entry_ref": None if cost is None else cost["cost_entry_id"],
                    "cost_revision": None if cost is None else int(cost["revision_number"]),
                    "amount_micros": None if cost is None else cost["amount_micros"],
                    "currency": None if cost is None else cost["currency"],
                    "cost_status": None if cost is None else cost["cost_status"],
                    "settlement_ref": (
                        None if settlement is None else settlement["settlement_id"]
                    ),
                    "settlement_revision": (
                        None if settlement is None else int(settlement["revision_number"])
                    ),
                    "settlement_state": None if settlement is None else settlement["state"],
                    "actual_calls": None if actual is None else actual["calls"],
                    "actual_bytes": None if actual is None else actual["bytes"],
                    "actual_records": None if actual is None else actual["records"],
                    "actual_cost_micros": (
                        None if actual is None else actual["cost_micros"]
                    ),
                }
            )

        quota_groups: dict[tuple[str, str], dict[str, Any]] = {}
        policy_currency = loaded["policy_currency"]
        for _, row in loaded["connector_quota_reservations"]:
            if row["policy_version_ref"] not in policy_currency:
                continue  # the inner join the SQL made
            reservation = {
                "reservation_id": row["reservation_id"],
                "quota_scope_ref": row["quota_scope_ref"],
                "window_started_at": row["window_started_at"],
                "window_ends_at": row["window_ends_at"],
                "reserved_json": row["reserved_json"],
                "quota_currency": policy_currency[row["policy_version_ref"]],
            }
            key = (reservation["quota_scope_ref"], reservation["window_started_at"])
            group = quota_groups.setdefault(
                key,
                {
                    "quota_scope_ref": reservation["quota_scope_ref"],
                    "window_started_at": reservation["window_started_at"],
                    "window_ends_at": reservation["window_ends_at"],
                    "currency": reservation["quota_currency"],
                    "reservations": 0,
                    "pending_reservations": 0,
                    "consumed_reservations": 0,
                    "released_reservations": 0,
                    "indeterminate_reservations": 0,
                    "reserved_calls": 0,
                    "reserved_bytes": 0,
                    "reserved_records": 0,
                    "reserved_cost_micros": 0,
                    "consumed_calls": 0,
                    "consumed_bytes": 0,
                    "consumed_records": 0,
                    "consumed_cost_micros": 0,
                    "indeterminate_calls": 0,
                    "indeterminate_bytes": 0,
                    "indeterminate_records": 0,
                    "indeterminate_cost_micros": 0,
                },
            )
            if (
                group["window_ends_at"] != reservation["window_ends_at"]
                or group["currency"] != reservation["quota_currency"]
            ):
                raise ProjectionSourceError("quota window authority is inconsistent")
            reserved = _connector_metrics(
                reservation["reserved_json"], "quota reservation"
            )
            group["reservations"] += 1
            for metric in _CONNECTOR_METRICS:
                group[f"reserved_{metric}"] += reserved[metric]
            settlement = latest_settlement.get(reservation["reservation_id"])
            if settlement is None:
                group["pending_reservations"] += 1
                continue
            state = settlement["state"]
            group[f"{state}_reservations"] += 1
            if state in {"consumed", "indeterminate"}:
                actual = _connector_metrics(
                    settlement["actual_json"], "quota settlement"
                )
                for metric in _CONNECTOR_METRICS:
                    group[f"{state}_{metric}"] += actual[metric]
        quota_rows = [quota_groups[key] for key in sorted(quota_groups)]
        return operation_rows, attempt_rows, quota_rows, incident_rows

    @staticmethod
    def _watermark(
        core: sqlite3.Connection,
        scheduler_sources: Mapping[str, list[Any]],
        folded: Mapping[str, FoldRead],
        catalog: sqlite3.Connection | None,
        router: sqlite3.Connection | None,
    ) -> str:
        """A fingerprint of every source: ``[COUNT(*), MAX(created_at)]`` each.

        The folds already know both for the tables they read (and computing
        them again is what cost ``COUNT(*)`` a walk of every table's index per
        round); the rest are asked, as before.
        """

        sources: dict[str, Any] = {"core": {}, "scheduler": {}}
        core_tables = _tables(core)

        def fingerprint(table: str) -> list[Any]:
            read = folded.get(table)
            if read is not None:
                return [read.count, read.max_created]
            row = core.execute(
                f"SELECT COUNT(*) AS count,MAX(created_at) AS latest FROM {table}"
            ).fetchone()
            return [row["count"], row["latest"]]

        for table in sorted(_CORE_TABLES | (_AGENDA_TABLES & core_tables)):
            if table.endswith("_pointer"):
                continue
            sources["core"][table] = fingerprint(table)
        for table in sorted(_CONNECTOR_PROJECTION_TABLES & core_tables):
            sources["core"][table] = fingerprint(table)
        for table in sorted(_SCHEDULER_TABLES):
            sources["scheduler"][table] = list(scheduler_sources[table])
        if catalog is not None and "capability_descriptor_versions" in _tables(catalog):
            row = catalog.execute(
                "SELECT COUNT(*) AS count,MAX(created_at) AS latest FROM capability_descriptor_versions"
            ).fetchone()
            sources["catalog"] = {"capability_descriptor_versions": [row["count"], row["latest"]]}
            catalog_tables = _tables(catalog)
            for table, time_field in (
                ("external_capability_source_registrations", "registered_at"),
                ("external_capability_source_heads", "updated_at"),
                ("external_capability_snapshot_ingest_events", "created_at"),
            ):
                if table not in catalog_tables:
                    continue
                source_row = catalog.execute(
                    f"SELECT COUNT(*) AS count,MAX({time_field}) AS latest FROM {table}"
                ).fetchone()
                sources["catalog"][table] = [
                    source_row["count"], source_row["latest"]
                ]
        if router is not None and "model_endpoint_profile_versions" in _tables(router):
            row = router.execute(
                "SELECT COUNT(*) AS count,MAX(created_at) AS latest FROM model_endpoint_profile_versions"
            ).fetchone()
            sources["router"] = [row["count"], row["latest"]]
        encoded = json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


def project_dashboard(
    core_db: str | Path,
    scheduler_db: str | Path,
    projection_db: str | Path,
    *,
    capability_catalog_db: str | Path | None = None,
    model_router_db: str | Path | None = None,
    clock: Callable[[], datetime] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Convenience API for one complete read-only rebuild."""
    return DashboardProjector(
        core_db,
        scheduler_db,
        capability_catalog_db=capability_catalog_db,
        model_router_db=model_router_db,
        clock=clock,
        cancel_requested=cancel_requested,
    ).project(projection_db)
