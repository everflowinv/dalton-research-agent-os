"""Writer lane for already-admitted directed document research.

The planner/research-task producer writes the immutable admission.  This lane
only selects an unstarted admission and launches its admission-ref-only child.
An orphaned or failed child is re-entered only from persisted Scheduler and
typed recovery authority.  Timed safe recovery does not block later independent
admissions; unproved send state remains held.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    write_owner_only,
)
from .lane_registry import LaneSpec, register_lane
from .store import canonical_json, content_hash


LANE_CONFIG = "mission-document-research-lane.json"
LAUNCHER_KWARG = "mission_document_research_launcher"
DRIVER_KEY = "mission_document_research"
LATEST_FILE = "latest.json"
HOLDS_FILE = "holds.json"


def _sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


class MissionDocumentResearchLaneError(RuntimeError):
    pass


def _read_holds(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MissionDocumentResearchLaneError("document research hold ledger is invalid") from exc
    body = dict(record)
    asserted = body.pop("content_hash", None)
    if (
        set(record) != {"schema_version", "holds", "content_hash"}
        or record.get("schema_version") != "0.1"
        or not isinstance(record.get("holds"), dict)
        or asserted != content_hash(body)
    ):
        raise MissionDocumentResearchLaneError("document research hold ledger drifted")
    holds: dict[str, dict[str, Any]] = {}
    for admission_ref, value in record["holds"].items():
        if (
            not isinstance(admission_ref, str)
            or not admission_ref.startswith("mission-document-research-admission:")
            or not isinstance(value, Mapping)
            or set(value) != {
                "admission_hash", "ticket_ref", "reason", "disposition",
                "retry_at",
            }
            or not _sha256(value.get("admission_hash"))
            or (
                value.get("ticket_ref") is not None
                and (
                    not isinstance(value.get("ticket_ref"), str)
                    or not value["ticket_ref"].startswith(
                        "mission-document-research:"
                    )
                )
            )
            or not isinstance(value.get("reason"), str)
            or not value["reason"]
            or value.get("disposition") not in {
                "terminal_hold", "recovery_required", "recovery_wait",
            }
            or (
                value.get("disposition") == "recovery_wait"
                and (
                    not isinstance(value.get("retry_at"), str)
                    or not value["retry_at"]
                )
            )
            or (
                value.get("disposition") != "recovery_wait"
                and value.get("retry_at") is not None
            )
        ):
            raise MissionDocumentResearchLaneError(
                "document research hold ledger has an invalid entry"
            )
        if value["disposition"] == "recovery_wait":
            try:
                parsed = datetime.fromisoformat(value["retry_at"])
            except ValueError as exc:
                raise MissionDocumentResearchLaneError(
                    "document research hold ledger has an invalid retry_at"
                ) from exc
            if parsed.tzinfo is None:
                raise MissionDocumentResearchLaneError(
                    "document research hold ledger retry_at lacks timezone"
                )
        holds[admission_ref] = dict(value)
    return holds


def _write_holds(path: Path, holds: Mapping[str, Mapping[str, Any]]) -> None:
    body = {
        "schema_version": "0.1",
        "holds": {key: dict(holds[key]) for key in sorted(holds)},
    }
    write_owner_only(path, {**body, "content_hash": content_hash(body)})


class MissionDocumentResearchCoordinator:
    def __init__(
        self, *, store: Any, launcher: Any | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.launcher = launcher
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def latest_path(self) -> Path:
        return self.launcher.tickets_dir / LATEST_FILE

    @property
    def holds_path(self) -> Path:
        return self.launcher.tickets_dir / HOLDS_FILE

    def _latest(self) -> dict[str, Any] | None:
        if not self.latest_path.is_file():
            return None
        try:
            value = json.loads(self.latest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MissionDocumentResearchLaneError(
                "document research latest ticket pointer is invalid"
            ) from exc
        if not isinstance(value, Mapping):
            raise MissionDocumentResearchLaneError(
                "document research latest ticket pointer has an invalid shape"
            )
        body = dict(value)
        asserted = body.pop("content_hash", None)
        if (
            set(value) != {
                "ticket_ref", "admission_ref", "admission_hash", "content_hash",
            }
            or asserted != content_hash(body)
            or not isinstance(value.get("ticket_ref"), str)
            or not value["ticket_ref"].startswith("mission-document-research:")
            or not isinstance(value.get("admission_ref"), str)
            or not value["admission_ref"].startswith(
                "mission-document-research-admission:"
            )
            or not _sha256(value.get("admission_hash"))
        ):
            raise MissionDocumentResearchLaneError(
                "document research latest ticket pointer drifted"
            )
        return dict(value)

    def _admissions(self) -> list[dict[str, Any]]:
        try:
            has_outcomes = self.store.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='mission_document_research_outcomes'"
            ).fetchone() is not None
            query = (
                "SELECT a.* FROM mission_document_research_admissions a "
                + (
                    "LEFT JOIN mission_document_research_outcomes o "
                    "ON o.admission_ref=a.admission_id WHERE o.outcome_id IS NULL "
                    if has_outcomes else ""
                )
                + "ORDER BY a.created_at,a.admission_id"
            )
            rows = self.store.connection.execute(query).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise
        result = []
        for row in rows:
            try:
                wire = json.loads(row["record_json"])
            except (TypeError, ValueError, RecursionError) as exc:
                raise MissionDocumentResearchLaneError(
                    "document research admission record is invalid"
                ) from exc
            body = dict(wire)
            asserted = body.pop("content_hash", None)
            if (
                canonical_json(wire) != row["record_json"]
                or wire.get("id") != row["admission_id"]
                or asserted != row["content_hash"]
                or asserted != content_hash(body)
            ):
                raise MissionDocumentResearchLaneError(
                    "document research admission authority drifted"
                )
            result.append(wire)
        return result

    def _started(self, admission_ref: str) -> bool:
        try:
            return self.store.connection.execute(
                "SELECT 1 FROM mission_document_research_starts WHERE admission_ref=?",
                (admission_ref,),
            ).fetchone() is not None
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return False
            raise

    def _hold(
        self, holds: dict[str, dict[str, Any]], admission: Mapping[str, Any],
        *, reason: str, ticket_ref: str | None,
        disposition: str = "terminal_hold",
        retry_at: str | None = None,
    ) -> None:
        holds[admission["id"]] = {
            "admission_hash": admission["content_hash"],
            "ticket_ref": ticket_ref,
            "reason": reason,
            "disposition": disposition,
            "retry_at": retry_at,
        }
        _write_holds(self.holds_path, holds)

    def _effective_work_hints(
        self, admission: Mapping[str, Any],
    ) -> list[str]:
        """Read recovery replacements only as relaunch hints.

        The child executor revalidates the complete admission, route, budget,
        no-send proof, and recovery chain before it can claim or send.  This
        read prevents the Writer from permanently watching a superseded Work;
        it does not itself authorize a provider call.
        """

        refs = [
            "work:mission-document-research-" + content_hash({
                "admission_identity_hash": admission["identity_hash"],
                "ordinal": ordinal,
            })[:32]
            for ordinal in range(1, 5)
        ]
        try:
            rows = self.store.connection.execute(
                "SELECT * FROM mission_document_research_recovery_links "
                "WHERE admission_ref=? ORDER BY stage_ordinal,recovery_number",
                (admission["id"],),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return refs
            raise
        next_number = {2: 1, 3: 1}
        for row in rows:
            try:
                wire = json.loads(row["record_json"])
            except (TypeError, ValueError, RecursionError) as exc:
                raise MissionDocumentResearchLaneError(
                    "document research recovery hint is invalid"
                ) from exc
            body = dict(wire) if isinstance(wire, Mapping) else {}
            asserted = body.pop("content_hash", None)
            stage = wire.get("stage_ordinal") if isinstance(wire, Mapping) else None
            if (
                not isinstance(wire, Mapping)
                or canonical_json(wire) != row["record_json"]
                or asserted != row["content_hash"]
                or asserted != content_hash(body)
                or wire.get("id") != row["recovery_link_id"]
                or wire.get("admission_ref") != admission["id"]
                or wire.get("admission_hash") != admission["content_hash"]
                or stage not in {2, 3}
                or row["stage_ordinal"] != stage
                or wire.get("recovery_number") != next_number[stage]
                or row["recovery_number"] != next_number[stage]
                or wire.get("failed_work_order_ref") != refs[stage - 1]
                or row["failed_work_order_ref"] != refs[stage - 1]
                or wire.get("recovery_work_order_ref")
                != row["recovery_work_order_ref"]
            ):
                raise MissionDocumentResearchLaneError(
                    "document research recovery hint drifted"
                )
            refs[stage - 1] = wire["recovery_work_order_ref"]
            next_number[stage] += 1
        return refs

    def _typed_recovery_state(
        self, admission: Mapping[str, Any], work_ref: str,
    ) -> dict[str, Any] | None:
        mission_ref = admission.get("mission_version_ref")
        if not isinstance(mission_ref, str):
            return None
        from .mission_document_research_executor import (
            read_mission_document_research_observations,
        )

        observations = [
            item for item in read_mission_document_research_observations(
                self.store.connection, mission_version_ref=mission_ref,
            )
            if item["admission_ref"] == admission["id"]
            and item["work_order_ref"] == work_ref
            and item["outcome"] == "recovery_required"
        ]
        if not observations:
            return None
        by_status = {
            item["recovery"]["status"]: item for item in observations
            if isinstance(item.get("recovery"), Mapping)
        }
        selected = next(
            (by_status[key] for key in ("stopped", "eligible", "waiting")
             if key in by_status),
            None,
        )
        if selected is None:
            raise MissionDocumentResearchLaneError(
                "document research recovery observation has an invalid state"
            )
        recovery = selected["recovery"]
        if recovery["status"] == "stopped":
            return {
                "action": "recovery_required", "reason": recovery["reason"],
                "work_order_ref": work_ref,
            }
        retry_at = recovery.get("retry_at")
        if not isinstance(retry_at, str) or not retry_at:
            raise MissionDocumentResearchLaneError(
                "document research recovery observation lacks retry_at"
            )
        try:
            parsed = datetime.fromisoformat(retry_at)
        except ValueError as exc:
            raise MissionDocumentResearchLaneError(
                "document research recovery retry_at is invalid"
            ) from exc
        if parsed.tzinfo is None:
            raise MissionDocumentResearchLaneError(
                "document research recovery retry_at lacks timezone"
            )
        due = parsed.astimezone(timezone.utc)
        if self.clock().astimezone(timezone.utc) < due:
            return {
                "action": "waiting", "reason": recovery["reason"],
                "retry_at": retry_at, "work_order_ref": work_ref,
            }
        return {
            "action": "resume", "reason": "typed_recovery_due",
            "work_order_ref": work_ref,
        }

    def _execution_state(self, admission: Mapping[str, Any]) -> dict[str, Any]:
        """Classify only authority already persisted for this admission.

        A ready/missing node can be resumed because the executor reconstructs
        the exact Work and the adapter replays an existing route before any
        new send.  A formal failure is never relaunched here; typed local
        capacity and budget failures are exposed for the recovery graph, and
        every other formal failure stays terminal/unknown.
        """

        from .contracts import ResultEnvelope, WorkOrder

        for work_ref in self._effective_work_hints(admission):
            row = self.store.connection.execute(
                "SELECT work_order_json,work_order_hash FROM scheduler_work_orders "
                "WHERE work_order_id=?", (work_ref,),
            ).fetchone()
            if row is None:
                return {
                    "action": "resume",
                    "reason": "next_exact_work_not_yet_admitted",
                    "work_order_ref": work_ref,
                }
            try:
                work = WorkOrder.from_dict(json.loads(row["work_order_json"])).to_dict()
            except Exception as exc:
                raise MissionDocumentResearchLaneError(
                    "document research Scheduler Work authority is invalid"
                ) from exc
            if (
                canonical_json(work) != row["work_order_json"]
                or content_hash(work) != row["work_order_hash"]
                or work["id"] != work_ref
                or work["metadata"].get(
                    "mission_document_research_admission_ref"
                ) != admission["id"]
                or work["metadata"].get(
                    "mission_document_research_admission_hash"
                ) != admission["content_hash"]
            ):
                raise MissionDocumentResearchLaneError(
                    "document research Scheduler Work authority drifted"
                )
            formal = self.store.connection.execute(
                "SELECT * FROM scheduler_formal_results WHERE work_order_id=?",
                (work_ref,),
            ).fetchone()
            if formal is not None:
                try:
                    envelope = ResultEnvelope.from_dict(
                        json.loads(formal["result_envelope_json"])
                    ).to_dict()
                except Exception as exc:
                    raise MissionDocumentResearchLaneError(
                        "document research formal result is invalid"
                    ) from exc
                formal_body = {
                    "id": formal["result_record_id"],
                    "work_order_id": formal["work_order_id"],
                    "attempt_number": formal["attempt_number"],
                    "result_envelope_id": formal["result_envelope_id"],
                    "result_envelope_hash": formal["result_envelope_hash"],
                    "terminal_state": formal["terminal_state"],
                    "created_at": formal["created_at"],
                }
                if (
                    canonical_json(envelope) != formal["result_envelope_json"]
                    or envelope["work_order_ref"] != work_ref
                    or envelope["id"] != formal["result_envelope_id"]
                    or content_hash(envelope) != formal["result_envelope_hash"]
                    or content_hash(formal_body) != formal["content_hash"]
                ):
                    raise MissionDocumentResearchLaneError(
                        "document research formal result authority drifted"
                    )
                if formal["terminal_state"] == "succeeded":
                    continue
                typed = self._typed_recovery_state(admission, work_ref)
                return typed or {
                    "action": "resume",
                    "reason": "recovery_classification_not_yet_recorded",
                    "work_order_ref": work_ref,
                }
            event = self.store.connection.execute(
                "SELECT state,not_before,attempt_number FROM scheduler_attempt_events "
                "WHERE work_order_id=? ORDER BY event_seq DESC LIMIT 1",
                (work_ref,),
            ).fetchone()
            if event is None:
                raise MissionDocumentResearchLaneError(
                    "document research Scheduler Work has no attempt authority"
                )
            refusal = self._budget_refusal(work, int(event["attempt_number"]))
            if refusal is not None:
                return {
                    "action": "recovery_required", "reason": refusal,
                    "work_order_ref": work_ref,
                }
            if event["state"] == "ready":
                not_before = event["not_before"]
                if not_before is not None and self.clock().astimezone(
                    timezone.utc
                ) < datetime.fromisoformat(not_before).astimezone(timezone.utc):
                    return {
                        "action": "waiting", "reason": "scheduler_backoff",
                        "retry_at": not_before, "work_order_ref": work_ref,
                    }
                return {
                    "action": "resume", "reason": "scheduler_ready",
                    "work_order_ref": work_ref,
                }
            if event["state"] == "leased":
                lease = self.store.connection.execute(
                    "SELECT expires_at FROM scheduler_leases WHERE work_order_id=? "
                    "ORDER BY lease_version DESC LIMIT 1", (work_ref,),
                ).fetchone()
                if lease is None:
                    raise MissionDocumentResearchLaneError(
                        "document research leased Work lost its lease authority"
                    )
                expires_at = lease["expires_at"]
                if self.clock().astimezone(timezone.utc) < datetime.fromisoformat(
                    expires_at
                ).astimezone(timezone.utc):
                    return {
                        "action": "waiting", "reason": "existing_lease",
                        "retry_at": expires_at, "work_order_ref": work_ref,
                    }
                return {
                    "action": "resume", "reason": "expired_lease_replay",
                    "work_order_ref": work_ref,
                }
            typed = self._typed_recovery_state(admission, work_ref)
            return typed or {
                "action": "resume",
                "reason": "recovery_classification_not_yet_recorded",
                "work_order_ref": work_ref,
            }
        return {
            "action": "resume", "reason": "outcome_commit_not_yet_finished",
            "work_order_ref": None,
        }

    @staticmethod
    def _budget_refusal(
        work: Mapping[str, Any], attempt_number: int,
    ) -> str | None:
        stage = work.get("metadata", {}).get("stage")
        if stage not in {
            "qualitative_model_draft", "independent_qualitative_verifier",
        }:
            return None
        path = work["metadata"].get("budget_db")
        if not isinstance(path, str) or not Path(path).is_file():
            return None
        phase = (
            "verification"
            if stage == "independent_qualitative_verifier" else "assessment"
        )
        connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            candidates = []
            for table in (
                "thesis_impact_day_rejections", "model_budget_pool_rejections",
            ):
                try:
                    candidates.extend(connection.execute(
                        f"SELECT record_json,content_hash FROM {table} "
                        "WHERE work_order_ref=? AND attempt_number=? AND phase=?",
                        (work["id"], attempt_number, phase),
                    ).fetchall())
                except sqlite3.OperationalError as exc:
                    if "no such table" not in str(exc):
                        raise
            if not candidates:
                return None
            if len(candidates) != 1:
                raise MissionDocumentResearchLaneError(
                    "document research budget refusal is not unique"
                )
            row = candidates[0]
            try:
                wire = json.loads(row["record_json"])
            except (TypeError, ValueError, RecursionError) as exc:
                raise MissionDocumentResearchLaneError(
                    "document research budget refusal is invalid"
                ) from exc
            body = dict(wire) if isinstance(wire, Mapping) else {}
            asserted = body.pop("content_hash", None)
            if (
                not isinstance(wire, Mapping)
                or canonical_json(wire) != row["record_json"]
                or asserted != row["content_hash"]
                or asserted != content_hash(body)
                or wire.get("work_order_ref") != work["id"]
                or wire.get("attempt_number") != attempt_number
                or wire.get("phase") != phase
            ):
                raise MissionDocumentResearchLaneError(
                    "document research budget refusal authority drifted"
                )
            reason = wire.get("reason")
            return (
                "budget_or_pool_refused:"
                + (reason if isinstance(reason, str) and reason else "owner_budget_exceeded")
            )
        finally:
            connection.close()

    def _resume(
        self, admission: Mapping[str, Any], *, ticket_ref: str,
        recovery: Mapping[str, Any], settled: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        authorization = canonical_json({
            "schema_version": "0.1",
            "kind": "exact_scheduler_replay",
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "prior_ticket_ref": ticket_ref,
            "work_order_ref": recovery.get("work_order_ref"),
            "reason": recovery["reason"],
        })
        resumed = self.launcher.resume(
            admission_ref=admission["id"],
            admission_hash=admission["content_hash"],
            prior_ticket_ref=ticket_ref,
            authorization=authorization,
        )
        pointer = {
            "ticket_ref": resumed["id"],
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
        }
        write_owner_only(
            self.latest_path,
            {**pointer, "content_hash": content_hash(pointer)},
        )
        return {
            "status": "resumed", "ticket_ref": resumed["id"],
            "admission_ref": admission["id"], "last": settled,
        }

    def dispatch_once(self) -> dict[str, Any]:
        if self.launcher is None:
            return {"status": "unconfigured", "reason": "document research lane is absent"}
        holds = _read_holds(self.holds_path)
        admissions = self._admissions()
        by_ref = {item["id"]: item for item in admissions}
        latest = self._latest()
        settled = None
        if latest is not None and latest["admission_ref"] in by_ref:
            admission = by_ref[latest["admission_ref"]]
            if latest["admission_hash"] != admission["content_hash"]:
                raise MissionDocumentResearchLaneError("latest admission hash drifted")
            ticket = self.launcher.status(latest["ticket_ref"])
            if ticket["status"] == "running":
                return {
                    "status": "busy", "ticket_ref": ticket["id"],
                    "admission_ref": admission["id"],
                }
            summary = ticket.get("summary")
            child_status = None if summary is None else summary.get("status")
            settled = {
                "ticket_ref": ticket["id"],
                "admission_ref": admission["id"],
                "status": child_status or ticket["status"],
            }
            if child_status != "complete":
                recovery = (
                    self._execution_state(admission)
                    if self._started(admission["id"]) else {
                        "action": "terminal_hold",
                        "reason": child_status or ticket["status"],
                    }
                )
                settled["recovery"] = recovery
                if recovery["action"] == "waiting":
                    self._hold(
                        holds, admission, reason=recovery["reason"],
                        ticket_ref=ticket["id"], disposition="recovery_wait",
                        retry_at=recovery["retry_at"],
                    )
                    recovery = None
                elif recovery["action"] == "resume":
                    try:
                        result = self._resume(
                            admission, ticket_ref=ticket["id"],
                            recovery=recovery, settled=settled,
                        )
                    except LaneChildConflict as exc:
                        return {"status": "busy", "reason": str(exc), "last": settled}
                    except LaneChildRejected as exc:
                        self._hold(
                            holds, admission,
                            reason="controlled_reentry_unavailable:" + str(exc),
                            ticket_ref=ticket["id"],
                            disposition="recovery_required",
                        )
                        settled["recovery"] = {
                            "action": "recovery_required",
                            "reason": "controlled_reentry_unavailable",
                        }
                        recovery = settled["recovery"]
                    else:
                        if holds.pop(admission["id"], None) is not None:
                            _write_holds(self.holds_path, holds)
                        return result
                if recovery is not None:
                    disposition = (
                        "recovery_required"
                        if recovery["action"] == "recovery_required"
                        else "terminal_hold"
                    )
                    self._hold(
                        holds, admission, reason=recovery["reason"],
                        ticket_ref=ticket["id"], disposition=disposition,
                    )

        now = self.clock().astimezone(timezone.utc)
        for admission in admissions:
            held = holds.get(admission["id"])
            if held is None or held["disposition"] != "recovery_wait":
                continue
            if held["admission_hash"] != admission["content_hash"]:
                raise MissionDocumentResearchLaneError(
                    "document research hold admission hash drifted"
                )
            try:
                retry_at = datetime.fromisoformat(held["retry_at"]).astimezone(
                    timezone.utc
                )
            except ValueError as exc:
                raise MissionDocumentResearchLaneError(
                    "document research hold retry_at is invalid"
                ) from exc
            if now < retry_at:
                continue
            recovery = self._execution_state(admission)
            if recovery["action"] == "waiting":
                self._hold(
                    holds, admission, reason=recovery["reason"],
                    ticket_ref=held["ticket_ref"], disposition="recovery_wait",
                    retry_at=recovery["retry_at"],
                )
                continue
            if recovery["action"] != "resume":
                self._hold(
                    holds, admission, reason=recovery["reason"],
                    ticket_ref=held["ticket_ref"],
                    disposition=("recovery_required"
                                 if recovery["action"] == "recovery_required"
                                 else "terminal_hold"),
                )
                continue
            try:
                result = self._resume(
                    admission, ticket_ref=held["ticket_ref"],
                    recovery=recovery, settled=settled,
                )
            except LaneChildConflict as exc:
                return {"status": "busy", "reason": str(exc), "last": settled}
            except LaneChildRejected as exc:
                self._hold(
                    holds, admission,
                    reason="controlled_reentry_unavailable:" + str(exc),
                    ticket_ref=held["ticket_ref"],
                    disposition="recovery_required",
                )
                continue
            holds.pop(admission["id"], None)
            _write_holds(self.holds_path, holds)
            return result

        for admission in admissions:
            held = holds.get(admission["id"])
            if held is not None:
                if held["admission_hash"] != admission["content_hash"]:
                    raise MissionDocumentResearchLaneError(
                        "document research hold admission hash drifted"
                    )
                continue
            if self._started(admission["id"]):
                self._hold(
                    holds, admission,
                    reason="started_without_owned_live_ticket",
                    ticket_ref=None, disposition="recovery_required",
                )
                continue
            try:
                ticket = self.launcher.start(
                    admission_ref=admission["id"],
                    admission_hash=admission["content_hash"],
                )
            except LaneChildConflict as exc:
                return {"status": "busy", "reason": str(exc), "last": settled}
            except LaneChildRejected as exc:
                self._hold(
                    holds, admission, reason=str(exc), ticket_ref=None
                )
                continue
            pointer = {
                "ticket_ref": ticket["id"],
                "admission_ref": admission["id"],
                "admission_hash": admission["content_hash"],
            }
            write_owner_only(
                self.latest_path,
                {**pointer, "content_hash": content_hash(pointer)},
            )
            return {
                "status": "launched", "ticket_ref": ticket["id"],
                "admission_ref": admission["id"], "last": settled,
            }
        return {
            "status": (
                "recovery_required"
                if any(item["disposition"] == "recovery_required"
                       for item in holds.values())
                else "waiting"
                if any(item["disposition"] == "recovery_wait"
                       for item in holds.values())
                else "idle"
            ),
            "reason": "no unstarted document research admission",
            "held": len(holds), "last": settled,
        }


def dispatch(server: Any, _params: Mapping[str, Any]) -> dict[str, Any]:
    launcher = server.lane_launcher(LAUNCHER_KWARG)
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        coordinator = MissionDocumentResearchCoordinator(
            store=server.store, launcher=launcher
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument(
        "--mission-document-research-lane", type=Path, default=None,
        help="Enable admission-ref-only document research execution from this closed config.",
    )


def lane_configuration(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MissionDocumentResearchLaneError(
            "mission document research lane config cannot be read"
        ) from exc
    if value != {"schema_version": "0.1", "enabled": True}:
        raise MissionDocumentResearchLaneError(
            "mission document research lane config has an invalid closed shape"
        )
    return dict(value)


def build_launcher(args: Any) -> Any | None:
    path = getattr(args, "mission_document_research_lane", None)
    if path is None:
        return None
    lane_configuration(path)
    required = {
        "candidate staging": getattr(args, "candidate_staging", None),
        "planner Scheduler": getattr(args, "scheduler", None),
        "research planner model config": getattr(
            args, "research_planner_model_config", None
        ),
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise MissionDocumentResearchLaneError(
            "mission document research lane requires " + ", ".join(missing)
        )
    from .mission_document_research_launcher import MissionDocumentResearchLauncher

    state_dir = Path(args.db).expanduser().resolve().parent
    return MissionDocumentResearchLauncher(
        state_dir=state_dir,
        staging_path=required["candidate staging"],
        planner_scheduler_db=required["planner Scheduler"],
        planner_model_config_path=required["research planner model config"],
        document_config_path=state_dir / "document-research-config.json",
    )


def argv_fragment(context: Any) -> list[str]:
    path = context.state / LANE_CONFIG
    return [] if not path.is_file() else ["--mission-document-research-lane", str(path)]


LANE = register_lane(LaneSpec(
    operation="dispatch_mission_document_research",
    order=152,
    driver_key=DRIVER_KEY,
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="Execute exact source-neutral directed-document admissions out of process; "
         "only typed Scheduler recovery can re-enter an orphaned or failed child.",
))


__all__ = [
    "DRIVER_KEY", "LANE", "LANE_CONFIG", "LAUNCHER_KWARG",
    "MissionDocumentResearchCoordinator", "MissionDocumentResearchLaneError",
    "add_arguments", "argv_fragment", "build_launcher", "lane_configuration",
]
