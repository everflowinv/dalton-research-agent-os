"""Rebuild the disposable dashboard projection from read-only authorities.

The projector is trusted ETL, not a new source of truth.  It opens Core and
Scheduler databases with SQLite ``mode=ro``, emits only the closed dashboard
row shapes, and delegates the atomic projection replacement to
``dashboard.ProjectionWriter``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

from .dashboard import ProjectionWriter, SCHEMA_VERSION


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


def _open_read_only(path: str | Path) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    uri = f"file:{quote(resolved.as_posix(), safe='/:')}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.Error as exc:
        raise ProjectionSourceError("authority database cannot be opened read-only") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
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
    ) -> None:
        self.core_db = Path(core_db)
        self.scheduler_db = Path(scheduler_db)
        self.capability_catalog_db = (
            None if capability_catalog_db is None else Path(capability_catalog_db)
        )
        self.model_router_db = None if model_router_db is None else Path(model_router_db)
        self.clock = clock or _utc_now

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
            with ProjectionWriter(temporary) as writer:
                writer.replace(snapshot)
            sync_fd = os.open(temporary, os.O_RDONLY)
            try:
                os.fsync(sync_fd)
            finally:
                os.close(sync_fd)
            os.replace(temporary, destination)
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()
        return snapshot

    def build_snapshot(self) -> dict[str, Any]:
        as_of_dt = self.clock()
        as_of = _timestamp(as_of_dt)
        as_of_dt = as_of_dt.astimezone(timezone.utc)
        warnings = _Warnings()

        with ExitStack() as stack:
            core = stack.enter_context(closing(_open_read_only(self.core_db)))
            scheduler = stack.enter_context(closing(_open_read_only(self.scheduler_db)))
            _require_tables(core, _CORE_TABLES, "Core/observability")
            _require_tables(scheduler, _SCHEDULER_TABLES, "Scheduler")
            core.execute("BEGIN")
            scheduler.execute("BEGIN")

            catalog = self._optional_source(
                stack,
                self.capability_catalog_db,
                core,
                "capability_descriptor_versions",
                "Capability Catalog 不可用；能力状态仅按实际调用推导",
                warnings,
            )
            router = self._optional_source(
                stack,
                self.model_router_db,
                core,
                "model_endpoint_profile_versions",
                "Model Router 不可用；模型状态仅按实际调用推导",
                warnings,
            )

            workflow_wires = self._latest_records(
                core,
                "observability_workflow_versions",
                "workflow_ref",
                "version_number",
            )
            link_wires = [
                _json(row["record_json"], "work-order link")
                for row in core.execute(
                    "SELECT record_json FROM observability_work_order_links "
                    "ORDER BY created_at, link_id"
                )
            ]
            usage_wires = self._latest_records(
                core,
                "observability_usage_entries",
                "invocation_ref",
                "revision_number",
            )
            latest_usage_ids = {wire["id"] for wire in usage_wires}
            cost_wires = [
                wire
                for wire in self._latest_records(
                    core,
                    "observability_cost_entries",
                    "usage_entry_ref",
                    "revision_number",
                )
                if wire["usage_entry_ref"] in latest_usage_ids
            ]
            artifact_wires = self._latest_artifact_records(core)
            invocation_wires = [
                _json(row["invocation_json"], "model invocation")
                for row in core.execute(
                    "SELECT invocation_json FROM model_invocations ORDER BY created_at, invocation_id"
                )
            ]

            scheduled = {
                row["work_order_id"]: {
                    "row": row,
                    "wire": _json(row["work_order_json"], "scheduler work order"),
                }
                for row in scheduler.execute("SELECT * FROM scheduler_work_orders")
            }
            latest_events = self._latest_rows(
                scheduler, "scheduler_attempt_events", "work_order_id", "event_seq"
            )
            formal_results = {
                row["work_order_id"]: row
                for row in scheduler.execute("SELECT * FROM scheduler_formal_results")
            }
            latest_receipts = self._latest_rows(
                scheduler,
                "scheduler_result_envelopes",
                "work_order_id",
                "attempt_number",
                secondary="created_at",
            )
            latest_leases = self._latest_lease_rows(scheduler)

            workflows, membership, parents, sequences = self._workflow_graph(
                workflow_wires, link_wires, warnings
            )
            all_work_refs = set(scheduled)
            all_work_refs.update(membership)
            all_work_refs.update(wire["work_order_ref"] for wire in invocation_wires)
            all_work_refs.update(wire["work_order_ref"] for wire in artifact_wires)
            self._assign_unowned_work(all_work_refs, workflows, membership, warnings, as_of)

            work_items, work_state = self._work_items(
                all_work_refs,
                workflows,
                membership,
                parents,
                sequences,
                scheduled,
                latest_events,
                formal_results,
                latest_receipts,
                latest_leases,
                invocation_wires,
                usage_wires,
                artifact_wires,
                as_of_dt,
                warnings,
            )
            invocation_rows = self._invocations(
                invocation_wires, usage_wires, membership, warnings
            )
            cost_rows = self._costs(cost_wires, usage_wires, membership, warnings)
            artifact_rows = self._artifacts(artifact_wires, membership)
            workflow_rows = self._workflow_summaries(
                workflows, work_items, work_state, invocation_rows, artifact_rows
            )
            capability_rows = self._capabilities(
                catalog, invocation_wires, as_of_dt, warnings
            )
            model_rows = self._models(router, invocation_rows, warnings)
            agenda_overview, agenda_cycles, agenda_questions = self._agenda(core, warnings)
            watermark = self._watermark(core, scheduler, catalog, router)

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
        }

    @staticmethod
    def _latest_artifact_records(core: sqlite3.Connection) -> list[dict[str, Any]]:
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
        rows = core.execute(
            "SELECT i.version_id,i.schema_version FROM observability_artifact_version_index i "
            "JOIN (SELECT artifact_ref,MAX(version_number) AS version_number "
            "      FROM observability_artifact_version_index GROUP BY artifact_ref) latest "
            "ON latest.artifact_ref=i.artifact_ref AND latest.version_number=i.version_number "
            "ORDER BY i.artifact_ref"
        ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            table = (
                "observability_artifact_versions"
                if row["schema_version"] == "0.1"
                else "observability_artifact_versions_v2"
            )
            record = core.execute(
                f"SELECT record_json FROM {table} WHERE version_id=?",
                (row["version_id"],),
            ).fetchone()
            if record is None:
                raise ProjectionSourceError(
                    "artifact generation index points to a missing authority row"
                )
            records.append(_json(record["record_json"], "artifact version"))
        return records

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
    ) -> sqlite3.Connection | None:
        if path is None:
            if table in _tables(core):
                return core
            warnings.add(warning)
            return None
        try:
            conn = stack.enter_context(closing(_open_read_only(path)))
        except ProjectionSourceError:
            warnings.add(warning)
            return None
        if table not in _tables(conn):
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
    def _latest_rows(
        conn: sqlite3.Connection,
        table: str,
        group_field: str,
        order_field: str,
        *,
        secondary: str | None = None,
    ) -> dict[str, sqlite3.Row]:
        order = f"{order_field} DESC"
        if secondary:
            order += f", {secondary} DESC"
        rows = conn.execute(
            f"SELECT * FROM (SELECT source.*, ROW_NUMBER() OVER ("
            f" PARTITION BY {group_field} ORDER BY {order}) AS projection_rank"
            f" FROM {table} AS source) WHERE projection_rank=1"
        ).fetchall()
        return {row[group_field]: row for row in rows}

    @staticmethod
    def _latest_lease_rows(conn: sqlite3.Connection) -> dict[tuple[str, int], sqlite3.Row]:
        rows = conn.execute(
            "SELECT * FROM (SELECT source.*, ROW_NUMBER() OVER ("
            " PARTITION BY work_order_id,attempt_number ORDER BY lease_version DESC,created_at DESC"
            ") AS projection_rank FROM scheduler_leases AS source) WHERE projection_rank=1"
        ).fetchall()
        return {(row["work_order_id"], row["attempt_number"]): row for row in rows}

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
                created_at = scheduled_entry["row"]["created_at"]
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
    def _watermark(
        core: sqlite3.Connection,
        scheduler: sqlite3.Connection,
        catalog: sqlite3.Connection | None,
        router: sqlite3.Connection | None,
    ) -> str:
        sources: dict[str, Any] = {"core": {}, "scheduler": {}}
        core_tables = _tables(core)
        for table in sorted(_CORE_TABLES | (_AGENDA_TABLES & core_tables)):
            if table.endswith("_pointer"):
                continue
            row = core.execute(
                f"SELECT COUNT(*) AS count,MAX(created_at) AS latest FROM {table}"
            ).fetchone()
            sources["core"][table] = [row["count"], row["latest"]]
        for table in sorted(_SCHEDULER_TABLES):
            row = scheduler.execute(
                f"SELECT COUNT(*) AS count,MAX(created_at) AS latest FROM {table}"
            ).fetchone()
            sources["scheduler"][table] = [row["count"], row["latest"]]
        if catalog is not None and "capability_descriptor_versions" in _tables(catalog):
            row = catalog.execute(
                "SELECT COUNT(*) AS count,MAX(created_at) AS latest FROM capability_descriptor_versions"
            ).fetchone()
            sources["catalog"] = [row["count"], row["latest"]]
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
) -> dict[str, Any]:
    """Convenience API for one complete read-only rebuild."""
    return DashboardProjector(
        core_db,
        scheduler_db,
        capability_catalog_db=capability_catalog_db,
        model_router_db=model_router_db,
        clock=clock,
    ).project(projection_db)
