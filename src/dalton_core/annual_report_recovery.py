"""Append-only effective suffixes for bounded annual-report unknown results."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable
from types import SimpleNamespace

from .contracts import ExecutionInvocation, ModelInvocation, ResultEnvelope, WorkOrder
from .provider_retry import returned_provider_failure_proof
from .research_plan import (
    _formal_result_from_cursor,
    _plan_work_orders,
    _resolve_qualitative_child_work_order,
)
from .research_plan_coordinator import (
    ResearchPlanCoordinatorConflict,
    _attempt_chain,
    _reverify_formal_result,
    _reverify_result_envelope,
    _reverify_scheduler_work_order,
    _upstream_outcome,
)
from .store import canonical_json, content_hash


class AnnualReportRecoveryError(RuntimeError):
    """Recovery authority is missing or has drifted."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _utc(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AnnualReportRecoveryError(
            "recovery_clock_invalid", "recovery clock must include timezone"
        )
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _time(value: Any, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AnnualReportRecoveryError(
            "recovery_authority_drift", f"{name} is not RFC3339"
        ) from exc
    if parsed.tzinfo is None:
        raise AnnualReportRecoveryError(
            "recovery_authority_drift", f"{name} lacks timezone"
        )
    return parsed.astimezone(timezone.utc)


def _phase(index: int) -> str:
    if index == 1:
        return "assessment"
    if index == 2:
        return "verification"
    raise AnnualReportRecoveryError(
        "unknown_result_ineligible", "only annual model nodes can be recovered"
    )


class AnnualReportUnknownRecovery:
    """Derive and verify fresh physical WorkOrders from one approved plan.

    The approved plan, its original WorkOrders and every original formal result
    remain untouched.  Recovery links merely select an effective suffix, and
    every read reconstructs each WorkOrder from the original plan plus the
    exact successful predecessor.
    """

    def __init__(
        self,
        *,
        plan: Any,
        scheduler: Any,
        draft_worker: Any,
        verifier_worker: Any,
        clock: Callable[[], datetime],
        actor_ref: str,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.plan = plan
        self.scheduler = scheduler
        self.workers = {1: draft_worker, 2: verifier_worker}
        self.clock = clock
        self.actor_ref = actor_ref
        self.fault_injector = fault_injector

    def _rows(self, plan_ref: str) -> list[Any]:
        return self.plan.connection.execute(
            "SELECT * FROM research_plan_recovery_links WHERE plan_version_ref=? "
            "ORDER BY version_number", (plan_ref,),
        ).fetchall()

    @staticmethod
    def _formal(cursor: Any, work: Mapping[str, Any]) -> dict[str, Any] | None:
        return _formal_result_from_cursor(cursor, work["id"])

    def _succeeded_formal(
        self, cursor: Any, work: Mapping[str, Any]
    ) -> dict[str, Any]:
        outcome = _upstream_outcome(cursor, work["id"], work)
        if outcome["state"] != "succeeded":
            raise AnnualReportRecoveryError(
                "recovery_upstream_not_succeeded",
                "recovery suffix requires an exact successful predecessor",
            )
        return outcome["formal"]

    def _resolved_base(
        self,
        plan_wire: Mapping[str, Any],
        blueprints: Sequence[Mapping[str, Any]],
        effective: Sequence[Mapping[str, Any]],
        index: int,
    ) -> dict[str, Any]:
        upstream = effective[index - 1]
        formal = self._succeeded_formal(self.plan.connection.cursor(), upstream)
        question_row = self.plan.connection.execute(
            "SELECT record_json FROM backlog_question_versions WHERE version_id=?",
            (plan_wire["question_version_ref"],),
        ).fetchone()
        if question_row is None:
            raise AnnualReportRecoveryError(
                "recovery_authority_drift", "plan question version is unavailable"
            )
        question_wire = json.loads(question_row["record_json"])
        resolved = _resolve_qualitative_child_work_order(
            plan_wire, blueprints[index], upstream, formal,
            question=question_wire["question"],
        )
        original_upstream = blueprints[index]["metadata"]["upstream_work_order_ref"]
        resolved["input_refs"] = [
            upstream["id"] if item == original_upstream else item
            for item in resolved["input_refs"]
        ]
        resolved["input_refs"] = list(dict.fromkeys(resolved["input_refs"]))
        resolved["metadata"]["upstream_work_order_ref"] = upstream["id"]
        return resolved

    @staticmethod
    def _identity(
        *, plan_wire: Mapping[str, Any], version: int, index: int, kind: str,
        failed: Mapping[str, Any] | None, upstream: Mapping[str, Any],
        policy: Mapping[str, Any], recovery_number: int,
        prior: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "plan_version_ref": plan_wire["id"], "version": version,
            "ordinal": index + 1, "kind": kind,
            "failed_work_order_ref": None if failed is None else failed["id"],
            "failed_work_order_hash": None if failed is None else content_hash(failed),
            "upstream_work_order_ref": upstream["id"],
            "upstream_work_order_hash": content_hash(upstream),
            "approved_plan_policy_hash": content_hash(policy),
            "recovery_number": recovery_number,
            "prior_recovery_link_ref": None if prior is None else prior["id"],
        }

    @staticmethod
    def _derive_work(
        base: Mapping[str, Any], *, identity: Mapping[str, Any], link_ref: str,
        kind: str, policy: Mapping[str, Any], window_started_at: str,
    ) -> dict[str, Any]:
        work_ref = "work:annual-recovery:" + content_hash(identity)[:32]
        metadata = dict(base["metadata"])
        metadata["unknown_recovery_derivation"] = {
            "schema_version": "0.1",
            "recovery_link_ref": link_ref,
            "derivation_kind": kind,
            "approved_plan_policy_hash": content_hash(policy),
            "recovery_window_started_at": window_started_at,
        }
        return WorkOrder.from_dict({
            **dict(base),
            "id": work_ref,
            "idempotency_key": "research-plan-recovery:" + link_ref,
            "input_refs": list(dict.fromkeys([
                *base["input_refs"],
                *([identity["failed_work_order_ref"]]
                  if identity.get("failed_work_order_ref") else []),
            ])),
            "metadata": metadata,
        }).to_dict()

    def _read_link(
        self,
        row: Any,
        *,
        plan_wire: Mapping[str, Any],
        start_wire: Mapping[str, Any],
        expected_version: int,
        prior: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        try:
            wire = json.loads(row["record_json"])
        except (TypeError, ValueError) as exc:
            raise AnnualReportRecoveryError(
                "recovery_authority_drift", "recovery link is not JSON"
            ) from exc
        columns = {
            "id": row["recovery_link_id"],
            "plan_version_ref": row["plan_version_ref"],
            "version": row["version_number"],
            "prior_recovery_link_ref": row["prior_recovery_link_ref"],
            "ordinal": row["ordinal"],
            "kind": row["link_kind"],
            "failed_work_order_ref": row["failed_work_order_ref"],
            "upstream_work_order_ref": row["upstream_work_order_ref"],
            "recovery_work_order_ref": row["recovery_work_order_ref"],
            "recovery_work_order_hash": row["recovery_work_order_hash"],
            "created_at": row["created_at"],
        }
        if (
            canonical_json(wire) != row["record_json"]
            or content_hash({k: v for k, v in wire.items() if k != "content_hash"})
            != wire.get("content_hash")
            or wire.get("content_hash") != row["content_hash"]
            or any(wire.get(key) != value for key, value in columns.items())
            or wire.get("schema_version") != "0.1"
            or wire.get("plan_version_hash") != plan_wire["content_hash"]
            or wire.get("plan_start_ref") != start_wire["id"]
            or wire.get("plan_start_hash") != start_wire["content_hash"]
            or wire.get("approval_ref") != start_wire["approval_ref"]
            or wire.get("approval_hash") != start_wire["approval_hash"]
            or wire.get("authorization_kind") != "approved_plan_policy_derivation"
            or wire.get("version") != expected_version
            or wire.get("prior_recovery_link_ref")
            != (None if prior is None else prior["id"])
            or wire.get("prior_recovery_link_hash")
            != (None if prior is None else prior["content_hash"])
        ):
            raise AnnualReportRecoveryError(
                "recovery_authority_drift", "recovery link authority drifted"
            )
        return wire

    def _unknown_proof(
        self,
        *,
        work: Mapping[str, Any],
        index: int,
        policy: Mapping[str, Any],
    ) -> dict[str, Any]:
        cursor = self.plan.connection.cursor()
        try:
            _reverify_scheduler_work_order(cursor, work["id"], work)
            events = _attempt_chain(cursor, work["id"])
            latest = events[-1]
            if latest["state"] != "failed" or latest["result_envelope_ref"] is None:
                raise AnnualReportRecoveryError(
                    "unknown_result_ineligible",
                    "recovery requires a terminal failed result",
                )
            formal = _reverify_formal_result(
                cursor, work["id"], attempt_number=latest["attempt_number"],
                result_envelope_id=latest["result_envelope_ref"],
                result_envelope_hash=latest["result_envelope_hash"],
            )
            if formal["terminal_state"] != "failed":
                raise AnnualReportRecoveryError(
                    "unknown_result_ineligible", "failed Work lacks failed formal authority"
                )
            envelope_row = cursor.execute(
                "SELECT * FROM scheduler_result_envelopes WHERE result_envelope_id=?",
                (formal["result_envelope_id"],),
            ).fetchone()
            if envelope_row is None:
                raise AnnualReportRecoveryError(
                    "unknown_result_unproven", "failed result envelope is unavailable"
                )
            envelope = _reverify_result_envelope(envelope_row)
            invocation_ref = envelope.get("invocation_ref")
            invocation_row = cursor.execute(
                "SELECT invocation_json FROM model_invocations WHERE invocation_id=?",
                (invocation_ref,),
            ).fetchone()
            if invocation_row is None:
                raise AnnualReportRecoveryError(
                    "unknown_result_unproven", "failed model invocation is unavailable"
                )
            saved_invocation = json.loads(invocation_row["invocation_json"])
            alias = saved_invocation.pop("invocation_id", None)
            try:
                invocation_object = ModelInvocation.from_dict(saved_invocation)
            except Exception as exc:
                raise AnnualReportRecoveryError(
                    "recovery_authority_drift", "failed model invocation is invalid"
                ) from exc
            invocation = invocation_object.to_dict()
            execution = ExecutionInvocation.from_model(invocation_object).to_dict()
            execution_row = cursor.execute(
                "SELECT e.execution_json,e.content_hash,l.content_hash AS link_hash "
                "FROM execution_invocations e JOIN execution_invocation_model_links l "
                "ON l.execution_ref=e.execution_id "
                "WHERE l.model_invocation_ref=?", (invocation_ref,),
            ).fetchone()
            if (alias != invocation_ref
                    or canonical_json({**invocation, "invocation_id": invocation_ref})
                    != invocation_row["invocation_json"]
                    or invocation["id"] != invocation_ref
                    or invocation["work_order_ref"] != work["id"]
                    or invocation["completed_at"] is None
                    or execution_row is None
                    or canonical_json(execution) != execution_row["execution_json"]
                    or content_hash(execution) != execution_row["content_hash"]
                    or content_hash({"execution_ref": invocation_ref,
                                     "model_invocation_ref": invocation_ref})
                    != execution_row["link_hash"]):
                raise AnnualReportRecoveryError(
                    "recovery_authority_drift", "failed model invocation drifted"
                )
            usage = invocation.get("usage")
            telemetry = (
                usage.get("raw_provider_telemetry")
                if isinstance(usage, Mapping) else None
            )
            cost = telemetry.get("cost") if isinstance(telemetry, Mapping) else None
            envelope_metadata = envelope.get("metadata")
            error = envelope.get("error")
            returned_failure = returned_provider_failure_proof(
                invocation_object, ResultEnvelope.from_dict(envelope)
            )
            unknown_classification = {
                "authority": "openclaw-model-adapter",
                "classification": "provider_completed_failure_unknown_metering",
                "returned_failure": returned_failure,
                "version": "0.1",
            }
            if (
                returned_failure is None
                or envelope.get("status") != "failed"
                or not isinstance(error, Mapping)
                or error.get("source") != "openclaw-model-broker"
                or not isinstance(envelope_metadata, Mapping)
                or envelope_metadata.get("broker_request_mode") != "execute"
                or envelope_metadata.get("dispatch_proof") != {
                    "authority": "openclaw-model-adapter",
                    "state": "provider_completed_failure", "version": "0.1",
                }
                or not isinstance(envelope_metadata.get("broker_response_hash"), str)
                or len(envelope_metadata["broker_response_hash"]) != 64
                or any(character not in "0123456789abcdef"
                       for character in envelope_metadata["broker_response_hash"])
                or not isinstance(usage, Mapping)
                or usage.get("measurement_status") != "unavailable"
                or any(usage.get(key) is not None for key in (
                    "input_tokens", "output_tokens", "total_tokens",
                    "cache_read_tokens", "cache_write_tokens",
                ))
                or not isinstance(cost, Mapping)
                or cost.get("available") is not False
            ):
                raise AnnualReportRecoveryError(
                    "unknown_result_ineligible",
                    "failed Work lacks a proved post-send unknown-metering classification",
                )
        except ResearchPlanCoordinatorConflict as exc:
            raise AnnualReportRecoveryError(
                "recovery_authority_drift", str(exc)
            ) from exc

        worker = self.workers[index]
        budget_store = getattr(worker, "budget_store", None)
        if budget_store is None or not hasattr(budget_store, "admission"):
            raise AnnualReportRecoveryError(
                "unknown_result_unproven",
                "historical failed Work has no exact budget admission authority",
            )
        overrun = budget_store.connection.execute(
            "SELECT 1 FROM thesis_impact_alerts WHERE kind='work_order_failed' "
            "AND work_order_ref=? AND json_extract(detail_json,'$.reason')="
            "'model_reservation_overrun' LIMIT 1", (work["id"],),
        ).fetchone()
        if overrun is not None:
            raise AnnualReportRecoveryError(
                "unknown_result_ineligible",
                "reservation overrun is frozen for owner reconciliation",
            )
        try:
            budget = budget_store.admission(
                work_order_ref=work["id"],
                attempt_number=int(formal["attempt_number"]),
                phase=_phase(index),
            )
        except Exception as exc:
            raise AnnualReportRecoveryError(
                "unknown_result_unproven", "budget admission authority is unavailable"
            ) from exc
        admission = budget.get("admission") if isinstance(budget, Mapping) else None
        mission_binding = budget.get("mission_binding") if isinstance(budget, Mapping) else None
        settlement = budget.get("settlement") if isinstance(budget, Mapping) else None
        ceiling = int(Decimal(str(work["budget"]["max_cost_usd"])) * 1_000_000)
        route_ref = envelope.get("metadata", {}).get("route_decision_ref")
        if (
            not isinstance(admission, Mapping)
            or admission.get("work_order_ref") != work["id"]
            or admission.get("attempt_number") != formal["attempt_number"]
            or admission.get("phase") != _phase(index)
            or admission.get("route_decision_ref") != route_ref
            or admission.get("policy_version_id") != work["metadata"]["budget_policy_ref"]
            or admission.get("reserved_micros") != ceiling
            or settlement is not None
            or not isinstance(mission_binding, Mapping)
        ):
            raise AnnualReportRecoveryError(
                "unknown_result_unproven",
                "failed Work does not retain one full unsettled reservation",
            )
        mission_ref = work["metadata"]["retrieval_proof"]["registration"][
            "mission_version_ref"
        ]
        resolver = getattr(worker, "mission_resolver", None)
        if not callable(resolver):
            raise AnnualReportRecoveryError(
                "recovery_mission_invalid", "mission permission authority is unavailable"
            )
        try:
            company_ref = work["metadata"]["retrieval_proof"]["registration"][
                "company_ref"
            ]
            mission = resolver(mission_ref, company_ref)
        except Exception as exc:
            raise AnnualReportRecoveryError(
                "recovery_mission_invalid", "mission is no longer active and permitted"
            ) from exc
        resolved_ref = (
            mission.get("id", mission.get("mission_version_ref"))
            if isinstance(mission, Mapping) else None
        )
        if (
            not isinstance(mission, Mapping)
            or resolved_ref != mission_ref
            or mission_binding.get("mission_version_ref") != mission_ref
        ):
            raise AnnualReportRecoveryError(
                "recovery_mission_invalid", "mission authority does not bind the failed Work"
            )
        return {
            "formal": formal,
            "envelope": envelope,
            "invocation_ref": invocation_ref,
            "invocation_hash": content_hash({**invocation, "invocation_id": invocation_ref}),
            "budget_admission_ref": admission["admission_id"],
            "budget_admission_hash": admission["content_hash"],
            "mission_binding_hash": content_hash(mission_binding),
            "unknown_classification": unknown_classification,
            "policy": policy,
        }

    def budget_refusal_reason(
        self, work: Mapping[str, Any], index: int
    ) -> str | None:
        """Return a proved atomic refusal for a recovery Work, if present."""

        if work.get("metadata", {}).get("unknown_recovery_derivation") is None:
            return None
        formal = self.scheduler.formal_result(work["id"])
        if formal is None or formal["terminal_state"] != "failed":
            return None
        worker = self.workers.get(index)
        store = None if worker is None else getattr(worker, "budget_store", None)
        if store is None:
            return None
        params = (work["id"], formal["attempt_number"], _phase(index))
        row = store.connection.execute(
            "SELECT * FROM thesis_impact_day_rejections WHERE work_order_ref=? "
            "AND attempt_number=? AND phase=?", params,
        ).fetchone()
        if row is not None:
            try:
                wire = json.loads(row["record_json"])
            except (TypeError, ValueError) as exc:
                raise AnnualReportRecoveryError(
                    "recovery_authority_drift", "budget rejection is not JSON"
                ) from exc
            base = dict(wire)
            asserted = base.pop("content_hash", None)
            columns = {
                "rejection_id": row["rejection_id"],
                "policy_version_id": row["policy_version_id"],
                "day": row["day"], "work_order_ref": row["work_order_ref"],
                "attempt_number": row["attempt_number"], "phase": row["phase"],
                "route_decision_ref": row["route_decision_ref"],
                "reserved_micros": row["reserved_micros"],
                "day_committed_micros": row["day_committed_micros"],
                "day_cap_micros": row["day_cap_micros"],
                "created_at": row["created_at"],
            }
            if (
                canonical_json(wire) != row["record_json"]
                or asserted != row["content_hash"]
                or asserted != content_hash(base)
                or any(wire.get(key) != value for key, value in columns.items())
                or wire.get("policy_version_id")
                != work["metadata"]["budget_policy_ref"]
            ):
                raise AnnualReportRecoveryError(
                    "recovery_authority_drift", "budget rejection authority drifted"
                )
            return "recovery_budget_refused"
        row = store.connection.execute(
            "SELECT * FROM model_budget_pool_rejections WHERE work_order_ref=? "
            "AND attempt_number=? AND phase=? ORDER BY created_at DESC LIMIT 1",
            params,
        ).fetchone()
        if row is None:
            return None
        try:
            wire = json.loads(row["record_json"])
        except (TypeError, ValueError) as exc:
            raise AnnualReportRecoveryError(
                "recovery_authority_drift", "pool rejection is not JSON"
            ) from exc
        base = dict(wire)
        asserted = base.pop("content_hash", None)
        columns = {
            "rejection_id": row["rejection_id"], "day": row["day"],
            "mission_ref": row["mission_ref"], "pool": row["pool"],
            "pool_lane": row["pool_lane"], "work_order_ref": row["work_order_ref"],
            "attempt_number": row["attempt_number"], "phase": row["phase"],
            "reserved_micros": row["reserved_micros"],
            "spent": row["pool_spent_micros"], "cap": row["pool_cap_micros"],
            "borrowable_micros": row["borrowable_micros"],
            "created_at": row["created_at"],
        }
        if (
            canonical_json(wire) != row["record_json"]
            or asserted != row["content_hash"]
            or asserted != content_hash(base)
            or any(wire.get(key) != value for key, value in columns.items())
            or wire.get("reason") != "pool_exhausted"
        ):
            raise AnnualReportRecoveryError(
                "recovery_authority_drift", "pool rejection authority drifted"
            )
        return "recovery_budget_refused"

    def effective_work_orders(
        self, plan_wire: Mapping[str, Any], start_wire: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        blueprints = _plan_work_orders(plan_wire)
        cursor = self.plan.connection.cursor()
        effective = [blueprints[0]]
        for index in range(1, len(blueprints)):
            prior_formal = self._formal(cursor, effective[index - 1])
            if prior_formal is None or prior_formal["terminal_state"] != "succeeded":
                effective.extend(blueprints[index:])
                break
            effective.append(self._resolved_base(
                plan_wire, blueprints, effective, index
            ))
        links: list[dict[str, Any]] = []
        prior = None
        root_policy: Mapping[str, Any] | None = None
        active_root: Mapping[str, Any] | None = None
        roots_by_ordinal: dict[int, list[dict[str, Any]]] = {}
        for version, row in enumerate(self._rows(plan_wire["id"]), start=1):
            wire = self._read_link(
                row, plan_wire=plan_wire, start_wire=start_wire,
                expected_version=version, prior=prior,
            )
            index = wire["ordinal"] - 1
            failed_work: Mapping[str, Any] | None = None
            if wire["kind"] == "unknown_recovery":
                if index not in (1, 2) or effective[index]["id"] != wire["failed_work_order_ref"]:
                    raise AnnualReportRecoveryError(
                        "recovery_authority_drift", "recovery root replaced the wrong Work"
                    )
                policy = effective[index]["metadata"].get("provider_retry")
                if (
                    not isinstance(policy, Mapping)
                    or policy.get("unknown_recovery") is None
                    or content_hash(policy) != wire["approved_plan_policy_hash"]
                ):
                    raise AnnualReportRecoveryError(
                        "recovery_authority_drift", "recovery policy drifted from the plan"
                    )
                stage_roots = roots_by_ordinal.setdefault(wire["ordinal"], [])
                expected_number = len(stage_roots) + 1
                maximum = policy["unknown_recovery"]["max_fresh_work_orders"]
                if expected_number > maximum:
                    raise AnnualReportRecoveryError(
                        "recovery_authority_drift",
                        "recovery link exceeds the approved fresh-Work bound",
                    )
                proof = self._unknown_proof(
                    work=effective[index], index=index, policy=policy
                )
                for key in (
                    "invocation_ref", "invocation_hash", "budget_admission_ref",
                    "budget_admission_hash", "mission_binding_hash",
                    "unknown_classification",
                ):
                    if wire.get(key) != proof[key]:
                        raise AnnualReportRecoveryError(
                            "recovery_authority_drift", "recovery proof no longer verifies"
                        )
                formal_hash = content_hash({
                    key: proof["formal"][key] for key in (
                        "id", "work_order_id", "attempt_number",
                        "result_envelope_id", "result_envelope_hash",
                        "terminal_state", "created_at",
                    )
                })
                if (
                    wire.get("failed_formal_result_ref") != proof["formal"]["id"]
                    or wire.get("failed_formal_result_hash") != formal_hash
                    or canonical_json(wire.get("approved_plan_policy"))
                    != canonical_json(policy)
                ):
                    raise AnnualReportRecoveryError(
                        "recovery_authority_drift",
                        "recovery failed-result/policy proof drifted",
                    )
                expected_window = (
                    proof["formal"]["created_at"]
                    if not stage_roots
                    else stage_roots[0]["recovery_window_started_at"]
                )
                if wire.get("recovery_window_started_at") != expected_window:
                    raise AnnualReportRecoveryError(
                        "recovery_authority_drift", "recovery window start drifted"
                    )
                created = _time(wire["created_at"], "recovery link creation")
                failed_at = _time(proof["formal"]["created_at"], "failed formal time")
                recovery_policy = policy["unknown_recovery"]
                if (
                    created < failed_at + timedelta(
                        seconds=recovery_policy["retry_backoff_seconds"]
                    )
                    or created >= _time(expected_window, "recovery window start")
                    + timedelta(seconds=recovery_policy["max_elapsed_seconds"])
                ):
                    raise AnnualReportRecoveryError(
                        "recovery_authority_drift",
                        "recovery link was created outside its approved time window",
                    )
                failed_work = effective[index]
                effective = effective[:index]
                root_policy = policy
            else:
                if root_policy is None or active_root is None or index != len(effective):
                    raise AnnualReportRecoveryError(
                        "recovery_authority_drift", "recovery suffix is out of order"
                    )
                expected_number = active_root["recovery_number"]
                expected_window = active_root["recovery_window_started_at"]
                recovery_policy = root_policy["unknown_recovery"]
                if (
                    wire.get("recovery_window_started_at") != expected_window
                    or _time(wire["created_at"], "recovery suffix creation")
                    >= _time(expected_window, "recovery window start")
                    + timedelta(seconds=recovery_policy["max_elapsed_seconds"])
                ):
                    raise AnnualReportRecoveryError(
                        "recovery_authority_drift",
                        "recovery suffix was created outside its approved window",
                    )
            if effective[index - 1]["id"] != wire["upstream_work_order_ref"]:
                raise AnnualReportRecoveryError(
                    "recovery_authority_drift", "recovery predecessor drifted"
                )
            base = self._resolved_base(plan_wire, blueprints, effective, index)
            identity = self._identity(
                plan_wire=plan_wire, version=version, index=index,
                kind=wire["kind"], failed=failed_work,
                upstream=effective[index - 1], policy=root_policy,
                recovery_number=expected_number, prior=prior,
            )
            expected_link_ref = (
                "research-plan-recovery-link:" + content_hash(identity)[:32]
            )
            repeated = {
                "id": expected_link_ref,
                "ordinal": index + 1,
                "kind": wire["kind"],
                "failed_work_order_ref": identity["failed_work_order_ref"],
                "failed_work_order_hash": identity["failed_work_order_hash"],
                "upstream_work_order_ref": identity["upstream_work_order_ref"],
                "upstream_work_order_hash": identity["upstream_work_order_hash"],
                "approved_plan_policy_hash": identity["approved_plan_policy_hash"],
                "recovery_number": expected_number,
            }
            if (
                canonical_json(wire.get("derivation_identity"))
                != canonical_json(identity)
                or any(wire.get(key) != value for key, value in repeated.items())
            ):
                raise AnnualReportRecoveryError(
                    "recovery_authority_drift",
                    "recovery derivation identity was not recomputed exactly",
                )
            expected = self._derive_work(
                base, identity=identity, link_ref=wire["id"], kind=wire["kind"],
                policy=root_policy, window_started_at=wire["recovery_window_started_at"],
            )
            if (expected["id"] != wire["recovery_work_order_ref"]
                    or content_hash(expected) != wire["recovery_work_order_hash"]):
                raise AnnualReportRecoveryError(
                    "recovery_authority_drift", "recovery Work derivation drifted"
                )
            _reverify_scheduler_work_order(cursor, expected["id"], expected)
            effective.append(expected)
            links.append(wire)
            if wire["kind"] == "unknown_recovery":
                roots_by_ordinal[wire["ordinal"]].append(wire)
                active_root = wire
            prior = wire
        return effective, links

    def recover(
        self,
        *,
        plan_wire: Mapping[str, Any],
        start_wire: Mapping[str, Any],
        effective: Sequence[Mapping[str, Any]],
        links: Sequence[Mapping[str, Any]],
        index: int,
    ) -> dict[str, Any]:
        failed = effective[index]
        policy = failed["metadata"].get("provider_retry")
        recovery = policy.get("unknown_recovery") if isinstance(policy, Mapping) else None
        if recovery is None:
            return {"status": "blocked", "reason": "unknown_recovery_not_approved"}
        try:
            proof = self._unknown_proof(work=failed, index=index, policy=policy)
        except AnnualReportRecoveryError as exc:
            return {"status": "blocked", "reason": exc.reason, "detail": str(exc)}
        roots = [
            item for item in links
            if item["kind"] == "unknown_recovery" and item["ordinal"] == index + 1
        ]
        if len(roots) >= recovery["max_fresh_work_orders"]:
            return {"status": "blocked", "reason": "unknown_recovery_exhausted"}
        started_at = (
            proof["formal"]["created_at"] if not roots
            else roots[0]["recovery_window_started_at"]
        )
        started = _time(started_at, "recovery window start")
        now = self.clock().astimezone(timezone.utc)
        deadline = started + timedelta(seconds=recovery["max_elapsed_seconds"])
        eligible_at = _time(proof["formal"]["created_at"], "failed formal time") + timedelta(
            seconds=recovery["retry_backoff_seconds"]
        )
        if now >= deadline or eligible_at >= deadline:
            return {"status": "blocked", "reason": "unknown_recovery_deadline_exceeded"}
        if now < eligible_at:
            return {
                "status": "waiting", "reason": "unknown_recovery_backoff",
                "retry_at": _utc(eligible_at),
            }
        return self._append(
            plan_wire=plan_wire, start_wire=start_wire, effective=effective,
            links=links, index=index, kind="unknown_recovery", failed=failed,
            policy=policy, proof=proof, window_started_at=_utc(started),
            recovery_number=len(roots) + 1,
        )

    def admit_suffix(
        self,
        *,
        plan_wire: Mapping[str, Any],
        start_wire: Mapping[str, Any],
        effective: Sequence[Mapping[str, Any]],
        links: Sequence[Mapping[str, Any]],
        index: int,
    ) -> dict[str, Any]:
        roots = [item for item in links if item["kind"] == "unknown_recovery"]
        if not roots:
            raise AnnualReportRecoveryError(
                "recovery_authority_drift", "suffix has no recovery root"
            )
        root = roots[-1]
        policy = effective[root["ordinal"] - 1]["metadata"]["provider_retry"]
        deadline = _time(
            root["recovery_window_started_at"], "recovery window start"
        ) + timedelta(seconds=policy["unknown_recovery"]["max_elapsed_seconds"])
        if self.clock().astimezone(timezone.utc) >= deadline:
            return {
                "status": "blocked",
                "reason": "unknown_recovery_deadline_exceeded",
            }
        return self._append(
            plan_wire=plan_wire, start_wire=start_wire, effective=effective,
            links=links, index=index, kind="recovery_suffix", failed=None,
            policy=policy, proof=None,
            window_started_at=root["recovery_window_started_at"],
            recovery_number=root["recovery_number"],
        )

    def _append(
        self, *, plan_wire: Mapping[str, Any], start_wire: Mapping[str, Any],
        effective: Sequence[Mapping[str, Any]], links: Sequence[Mapping[str, Any]],
        index: int, kind: str, failed: Mapping[str, Any] | None,
        policy: Mapping[str, Any], proof: Mapping[str, Any] | None,
        window_started_at: str, recovery_number: int,
    ) -> dict[str, Any]:
        blueprints = _plan_work_orders(plan_wire)
        upstream = effective[index - 1]
        base = self._resolved_base(plan_wire, blueprints, effective, index)
        version = len(links) + 1
        prior = None if not links else links[-1]
        identity = self._identity(
            plan_wire=plan_wire, version=version, index=index, kind=kind,
            failed=failed, upstream=upstream, policy=policy,
            recovery_number=recovery_number, prior=prior,
        )
        link_ref = "research-plan-recovery-link:" + content_hash(identity)[:32]
        work = self._derive_work(
            base, identity=identity, link_ref=link_ref, kind=kind,
            policy=policy, window_started_at=window_started_at,
        )
        enqueued = self.scheduler.enqueue(work)
        if enqueued["status"] == "conflict":
            raise AnnualReportRecoveryError(
                "recovery_authority_drift", "recovery Work enqueue conflicted"
            )
        if self.fault_injector is not None:
            self.fault_injector("after_recovery_enqueue")
        created_at = _utc(self.clock())
        wire = {
            "schema_version": "0.1", "id": link_ref,
            "plan_version_ref": plan_wire["id"],
            "plan_version_hash": plan_wire["content_hash"],
            "plan_start_ref": start_wire["id"],
            "plan_start_hash": start_wire["content_hash"],
            "approval_ref": start_wire["approval_ref"],
            "approval_hash": start_wire["approval_hash"],
            "authorization_kind": "approved_plan_policy_derivation",
            "version": version,
            "prior_recovery_link_ref": None if prior is None else prior["id"],
            "prior_recovery_link_hash": None if prior is None else prior["content_hash"],
            "ordinal": index + 1, "stage": blueprints[index]["metadata"]["stage"],
            "kind": kind,
            "failed_work_order_ref": None if failed is None else failed["id"],
            "failed_work_order_hash": None if failed is None else content_hash(failed),
            "failed_formal_result_ref": None if proof is None else proof["formal"]["id"],
            "failed_formal_result_hash": None if proof is None else content_hash({
                key: proof["formal"][key] for key in (
                    "id", "work_order_id", "attempt_number", "result_envelope_id",
                    "result_envelope_hash", "terminal_state", "created_at",
                )
            }),
            "invocation_ref": None if proof is None else proof["invocation_ref"],
            "invocation_hash": None if proof is None else proof["invocation_hash"],
            "budget_admission_ref": None if proof is None else proof["budget_admission_ref"],
            "budget_admission_hash": None if proof is None else proof["budget_admission_hash"],
            "mission_binding_hash": None if proof is None else proof["mission_binding_hash"],
            "unknown_classification": (
                None if proof is None else dict(proof["unknown_classification"])
            ),
            "approved_plan_policy": dict(policy),
            "approved_plan_policy_hash": content_hash(policy),
            "recovery_number": recovery_number,
            "recovery_window_started_at": window_started_at,
            "upstream_work_order_ref": upstream["id"],
            "upstream_work_order_hash": content_hash(upstream),
            "recovery_work_order_ref": work["id"],
            "recovery_work_order_hash": content_hash(work),
            "derivation_identity": identity,
            "actor_ref": self.actor_ref,
            "created_at": created_at,
        }
        wire["content_hash"] = content_hash(wire)
        with self.plan.store._transaction() as cur:
            existing = cur.execute(
                "SELECT record_json FROM research_plan_recovery_links "
                "WHERE recovery_link_id=?", (link_ref,),
            ).fetchone()
            if existing is None:
                cur.execute(
                    "INSERT INTO research_plan_recovery_links("
                    "recovery_link_id,plan_version_ref,version_number,"
                    "prior_recovery_link_ref,ordinal,link_kind,failed_work_order_ref,"
                    "upstream_work_order_ref,recovery_work_order_ref,"
                    "recovery_work_order_hash,record_json,content_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (link_ref, plan_wire["id"], version,
                     wire["prior_recovery_link_ref"], index + 1, kind,
                     wire["failed_work_order_ref"], upstream["id"], work["id"],
                     wire["recovery_work_order_hash"], canonical_json(wire),
                     wire["content_hash"], created_at),
                )
            elif existing["record_json"] != canonical_json(wire):
                # This is reachable only if the enqueue/link crash seam was
                # replayed with a different authority timestamp.
                saved = json.loads(existing["record_json"])
                if any(saved.get(key) != wire.get(key) for key in (
                    "id", "plan_version_ref", "version", "ordinal", "kind",
                    "recovery_work_order_ref", "recovery_work_order_hash",
                )):
                    raise AnnualReportRecoveryError(
                        "recovery_authority_drift", "recovery link identity conflicted"
                    )
        return {
            "status": "admitted", "reason": kind,
            "admitted_work_order_ref": work["id"],
            "admitted_work_order_hash": content_hash(work),
            "recovery_link_ref": link_ref,
        }


def read_effective_annual_recovery_work_orders(
    *, connection: Any, plan_wire: Mapping[str, Any],
    start_wire: Mapping[str, Any], clock: Callable[[], datetime],
    mission_resolver: Callable[[str, str], Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read the effective annual suffix without opening any write authority.

    The process supervisor uses this only to decide whether the same approved
    plan is resumable.  With no recovery links it preserves the original plan
    reader exactly.  Once links exist, both model stages open their frozen
    budget databases in SQLite read-only mode and re-run the same link,
    invocation, admission and mission proof used by the executor.
    """

    rows = connection.execute(
        "SELECT 1 FROM research_plan_recovery_links WHERE plan_version_ref=? LIMIT 1",
        (plan_wire["id"],),
    ).fetchone()
    if rows is None:
        from .research_plan import _resolved_plan_work_orders

        return _resolved_plan_work_orders(plan_wire, connection.cursor()), []
    blueprints = _plan_work_orders(plan_wire)
    from .thesis_impact_budget import ThesisImpactBudgetStore

    stores: dict[str, ThesisImpactBudgetStore] = {}
    try:
        workers: dict[int, Any] = {}
        for index in (1, 2):
            path = blueprints[index]["metadata"]["budget_db"]
            if path not in stores:
                stores[path] = ThesisImpactBudgetStore(path, read_only=True)
            workers[index] = SimpleNamespace(
                budget_store=stores[path], mission_resolver=mission_resolver,
            )
        reader = AnnualReportUnknownRecovery(
            plan=SimpleNamespace(connection=connection), scheduler=None,
            draft_worker=workers[1], verifier_worker=workers[2],
            clock=clock, actor_ref="supervisor:annual-read-only",
        )
        return reader.effective_work_orders(plan_wire, start_wire)
    finally:
        for store in stores.values():
            store.close()


__all__ = [
    "AnnualReportRecoveryError", "AnnualReportUnknownRecovery",
    "read_effective_annual_recovery_work_orders",
]
