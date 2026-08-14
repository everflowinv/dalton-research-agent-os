"""Bounded multi-page recorded reference shadows for CNINFO and SEC."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .connector import source_envelope_content_hash
from .connector_inventory import load_packaged_connector_inventory
from .connector_runner import (
    ConnectorRunnerAdmissionGate,
    ValidatedRunnerAdmission,
    validate_adapter_transport_observation,
    validate_connector_adapter_request,
    validate_connector_runner_request,
    validate_connector_runner_response,
)
from .connector_transport_executor import (
    AdapterDeadlineExceeded,
    ConnectorTransportError,
    invoke_adapter_with_deadline,
)
from .contracts import ExecutionInvocation, ResultEnvelope, WorkOrder
from .raw_spool import RawObject, RawSpool, RawSpoolCapacityError, RawSpoolLimitExceeded
from .runner_journal import RunnerJournal, RunnerJournalNotFound
from .scheduler import LeaseExpired
from .recorded_source_adapter import (
    RecordedSourceError,
    RecordedSourceFixtureAdapter,
    load_recorded_source_fixture,
    validate_recorded_source_fixture,
)
from .store import canonical_json, content_hash


class RecordedReferenceShadowError(ConnectorTransportError):
    pass


@dataclass(frozen=True, slots=True)
class _ShadowCommitContext:
    request: Mapping[str, Any]
    work_order: WorkOrder
    invocation: Mapping[str, Any]
    execution: ExecutionInvocation
    profile: Mapping[str, Any]
    call_spec: Mapping[str, Any]


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RecordedReferenceShadowError(f"{name} must be an object")
    unknown = set(value) - fields
    missing = fields - set(value)
    if unknown or missing:
        raise RecordedReferenceShadowError(
            f"{name} has invalid closed shape; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise RecordedReferenceShadowError(f"{name} must be finite JSON") from exc


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecordedReferenceShadowError(f"{name} must be a non-empty string")
    return value.strip()


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise RecordedReferenceShadowError("timestamp must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise RecordedReferenceShadowError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _wire_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RecordedReferenceShadowError("runner clock must return aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _derived_id(prefix: str, key: str) -> str:
    return f"{prefix}:{content_hash({'kind': prefix, 'idempotency_key': key})}"


def _validate_schema_instance(
    value: Any, schema: Mapping[str, Any], *, path: str = "structured_output"
) -> None:
    expected = schema.get("type")
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        checks = {
            "null": lambda item: item is None,
            "object": lambda item: isinstance(item, Mapping),
            "array": lambda item: isinstance(item, list),
            "string": lambda item: isinstance(item, str),
            "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        }
        if not all(name in checks for name in names) or not any(
            checks[name](value) for name in names
        ):
            raise RecordedReferenceShadowError(f"{path} does not match its frozen type")
    if isinstance(value, Mapping):
        required = set(schema.get("required", ()))
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping) or not required.issubset(value):
            raise RecordedReferenceShadowError(f"{path} lacks frozen required fields")
        if schema.get("additionalProperties") is False and set(value) != set(properties):
            raise RecordedReferenceShadowError(f"{path} is not a closed frozen object")
        for name, item in value.items():
            child = properties.get(name)
            if isinstance(child, Mapping):
                _validate_schema_instance(item, child, path=f"{path}.{name}")
    if isinstance(value, list):
        if schema.get("uniqueItems"):
            encoded = [canonical_json(item) for item in value]
            if len(encoded) != len(set(encoded)):
                raise RecordedReferenceShadowError(f"{path} contains duplicate items")
        item_schema = schema.get("items", {})
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate_schema_instance(item, item_schema, path=f"{path}[{index}]")
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            raise RecordedReferenceShadowError(f"{path} is shorter than its frozen schema")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            raise RecordedReferenceShadowError(f"{path} does not match its frozen pattern")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < int(schema["minimum"]):
            raise RecordedReferenceShadowError(f"{path} is below its frozen minimum")


def validate_recorded_reference_shadow_plan(spec: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "schema_version", "id", "created_at", "connector_profile_ref",
        "connector_profile_hash", "inventory_template_ref",
        "inventory_template_hash", "inventory_fixture_manifest_ref",
        "inventory_fixture_manifest_hash", "recorded_fixture_ref",
        "recorded_fixture_hash", "transport_target_ref", "transport_target_hash",
        "transport_host_policy", "adapter_ref", "adapter_hash",
        "package_manifest_ref", "package_manifest_hash", "source_ref", "operation",
        "recorded_scenario", "recorded_scenario_ref", "recorded_scenario_hash",
        "recorded_behavior",
        "pagination_mode", "cursor_field", "max_pages",
        "bounded_window_fields", "completeness_target", "content_hash",
    }
    wire = _closed(spec, fields, "RecordedReferenceShadowPlan")
    if wire["schema_version"] != "0.1":
        raise RecordedReferenceShadowError("unsupported reference shadow plan version")
    for name in (
        "id", "created_at", "connector_profile_ref", "inventory_template_ref",
        "inventory_fixture_manifest_ref", "recorded_fixture_ref",
        "transport_target_ref", "transport_host_policy", "adapter_ref",
        "package_manifest_ref", "source_ref", "operation", "pagination_mode",
        "cursor_field", "completeness_target", "recorded_scenario",
        "recorded_scenario_ref", "recorded_behavior",
    ):
        wire[name] = _text(wire[name], name)
    for name in (
        "connector_profile_hash", "inventory_template_hash",
        "inventory_fixture_manifest_hash", "recorded_fixture_hash",
        "transport_target_hash", "adapter_hash", "package_manifest_hash",
        "recorded_scenario_hash",
    ):
        if len(wire[name]) != 64 or any(
            char not in "0123456789abcdef" for char in wire[name]
        ):
            raise RecordedReferenceShadowError(f"{name} must be SHA-256")
    expected = {
        "source:cninfo": (
            "cninfo", "list_announcements", "page", "page"
        ),
        "source:sec-edgar": (
            "sec", "list_filings", "cursor", "cursor"
        ),
    }.get(wire["source_ref"])
    if expected is None:
        raise RecordedReferenceShadowError("reference source plan identity is invalid")
    slug, operation, pagination_mode, cursor_field = expected
    if (
        wire["inventory_template_ref"]
        != f"connector-profile-template:{slug}:0.1"
        or wire["recorded_fixture_ref"]
        != f"recorded-source-fixture:{slug}:0.1"
        or (wire["operation"], wire["pagination_mode"], wire["cursor_field"])
        != (operation, pagination_mode, cursor_field)
    ):
        raise RecordedReferenceShadowError("reference source plan graph is invalid")
    if wire["completeness_target"] != "enumerated":
        raise RecordedReferenceShadowError("reference shadow target must be enumerated")
    if wire["recorded_scenario"] not in {
        "success", "empty", "pagination", "partial", "schema_drift",
        "rate_limited", "timeout", "malformed",
    }:
        raise RecordedReferenceShadowError("recorded scenario is invalid")
    expected_behavior = {
        "success": "return", "empty": "return", "pagination": "return",
        "partial": "return", "schema_drift": "normalize_error",
        "rate_limited": "rate_limited", "timeout": "timeout",
        "malformed": "normalize_error",
    }[wire["recorded_scenario"]]
    if wire["recorded_behavior"] != expected_behavior:
        raise RecordedReferenceShadowError("recorded scenario behavior is not exact")
    if wire["recorded_scenario_ref"] != (
        f"recorded-source-scenario:{slug}:{wire['recorded_scenario']}:0.1"
    ):
        raise RecordedReferenceShadowError("recorded scenario ref is not authority-derived")
    if isinstance(wire["max_pages"], bool) or not isinstance(wire["max_pages"], int) or not 1 <= wire["max_pages"] <= 20:
        raise RecordedReferenceShadowError("max_pages must be between 1 and 20")
    if wire["bounded_window_fields"] != ["date_from", "date_to"]:
        raise RecordedReferenceShadowError(
            "reference shadow requires the exact bounded filing window"
        )
    declared = wire.pop("content_hash")
    if declared != content_hash(wire):
        raise RecordedReferenceShadowError("reference shadow plan content_hash mismatch")
    wire["content_hash"] = declared
    return wire


class RecordedReferenceShadowCoordinator:
    """Record every simulated page as an independent physical attempt."""

    def __init__(
        self,
        *,
        plan: Mapping[str, Any],
        gate: ConnectorRunnerAdmissionGate,
        journal: RunnerJournal,
        spool: RawSpool,
        authority: Any,
        clock: Callable[[], datetime] | None = None,
        retry_backoff_seconds: float = 1.0,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.plan = validate_recorded_reference_shadow_plan(plan)
        self.gate = gate
        self.journal = journal
        self.spool = spool
        self.authority = authority
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fault_hook = fault_hook
        if retry_backoff_seconds <= 0:
            raise RecordedReferenceShadowError("retry backoff must be positive")
        self.retry_backoff_seconds = float(retry_backoff_seconds)

    def _commit_context(
        self, admission: ValidatedRunnerAdmission
    ) -> _ShadowCommitContext:
        return _ShadowCommitContext(
            request=admission.request,
            work_order=admission.work_order,
            invocation=admission.invocation,
            execution=admission.execution,
            profile=admission.profile,
            call_spec=admission.call_spec,
        )

    def _commit_context_wire(
        self, context: ValidatedRunnerAdmission | _ShadowCommitContext
    ) -> dict[str, Any]:
        work_wire = context.work_order.to_dict()
        execution_wire = context.execution.to_dict()
        return {
            "schema_version": "0.1",
            "plan_hash": self.plan["content_hash"],
            "request": dict(context.request),
            "work_order": work_wire,
            "work_order_hash": content_hash(work_wire),
            "invocation": dict(context.invocation),
            "execution": execution_wire,
            "execution_hash": content_hash(execution_wire),
            "profile": dict(context.profile),
            "call_spec": dict(context.call_spec),
        }

    def _commit_context_from_wire(
        self, value: Mapping[str, Any]
    ) -> _ShadowCommitContext:
        wire = _closed(
            value,
            {
                "schema_version", "plan_hash", "request", "work_order",
                "work_order_hash", "invocation", "execution", "execution_hash",
                "profile", "call_spec",
            },
            "recorded page commit_context",
        )
        if wire["schema_version"] != "0.1" or wire["plan_hash"] != self.plan["content_hash"]:
            raise RecordedReferenceShadowError("page commit context plan authority is stale")
        request = validate_connector_runner_request(wire["request"])
        work = WorkOrder.from_dict(wire["work_order"])
        execution = ExecutionInvocation.from_dict(wire["execution"])
        invocation = wire["invocation"]
        profile = wire["profile"]
        call = wire["call_spec"]
        if (
            wire["work_order_hash"] != content_hash(work.to_dict())
            or wire["execution_hash"] != content_hash(execution.to_dict())
            or request["work_order_hash"] != wire["work_order_hash"]
            or request["execution_hash"] != wire["execution_hash"]
            or request["connector_invocation_hash"] != invocation.get("content_hash")
            or request["connector_profile_hash"] != profile.get("content_hash")
            or request["call_spec_hash"] != call.get("content_hash")
        ):
            raise RecordedReferenceShadowError("page commit context hashes are invalid")
        stored_execution = self.authority.get_execution(execution.id)
        stored_work = self.authority.get_scheduler_work_order(work.id)
        if (
            self.authority.get_invocation(invocation["id"]) != invocation
            or self.authority.get_profile(profile["id"]) != profile
            or self.authority.get_call_spec(call["id"]) != call
            or stored_execution is None
            or stored_execution["execution"] != execution.to_dict()
            or stored_execution["content_hash"] != wire["execution_hash"]
            or stored_work is None
            or stored_work["work_order"] != work.to_dict()
            or stored_work["content_hash"] != wire["work_order_hash"]
        ):
            raise RecordedReferenceShadowError(
                "page commit context differs from immutable authority"
            )
        return _ShadowCommitContext(
            request=request, work_order=work, invocation=invocation,
            execution=execution, profile=profile, call_spec=call,
        )

    def _validate_page_receipt(
        self,
        value: Mapping[str, Any],
        context: _ShadowCommitContext,
        *,
        ordinal: int,
        prior_receipt: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        fields = {
            "idempotency_status", "runner_request_ref", "runner_request_hash",
            "reservation_ref", "physical_attempt_ref", "physical_attempt_hash",
            "adapter_request_hash", "usage_entry_ref", "usage_entry_hash",
            "cost_entry_ref", "cost_entry_hash", "quota_settlement_ref",
            "quota_settlement_hash", "attempt_outcome", "retry_at",
            "observation", "raw_object", "error", "raw_artifact_version_ref",
            "raw_artifact_version_hash", "page_result_envelope",
            "page_result_envelope_hash",
            "completion_event_ref", "completion_event_hash", "completion_event_at",
        }
        receipt = _closed(value, fields, f"page_receipts[{ordinal - 1}]")
        expected_request = (
            dict(context.request)
            if context.request["id"] == receipt["runner_request_ref"]
            else self._page_request(context.request, ordinal)
        )
        completion_event = self.journal.event(receipt["completion_event_ref"])
        if (
            receipt["idempotency_status"] not in {"fresh", "duplicate"}
            or receipt["runner_request_ref"] != expected_request["id"]
            or receipt["runner_request_hash"] != expected_request["content_hash"]
            or completion_event["runner_request_ref"] != expected_request["id"]
            or completion_event["content_hash"] != receipt["completion_event_hash"]
            or completion_event["event_at"] != receipt["completion_event_at"]
            or completion_event["state"] not in {"transport_started", "observed"}
        ):
            raise RecordedReferenceShadowError("page receipt request binding is invalid")
        attempt = self.authority.get_physical_attempt(receipt["physical_attempt_ref"])
        usage = self.authority.get_usage_entry(receipt["usage_entry_ref"])
        cost = self.authority.get_cost_entry(receipt["cost_entry_ref"])
        settlement = self.authority.get_quota_settlement(
            receipt["quota_settlement_ref"]
        )
        reservation = self.authority.get_reservation(receipt["reservation_ref"])
        if (
            attempt is None or usage is None or cost is None or settlement is None
            or attempt["content_hash"] != receipt["physical_attempt_hash"]
            or usage["content_hash"] != receipt["usage_entry_hash"]
            or cost["content_hash"] != receipt["cost_entry_hash"]
            or settlement["content_hash"] != receipt["quota_settlement_hash"]
            or reservation["connector_invocation_ref"] != context.invocation["id"]
            or int(reservation["physical_attempt_number"]) != ordinal
            or attempt["connector_invocation_ref"] != context.invocation["id"]
            or attempt["reservation_ref"] != reservation["id"]
            or int(attempt["physical_attempt_number"]) != ordinal
            or attempt["outcome"] != receipt["attempt_outcome"]
            or usage["physical_attempt_ref"] != attempt["id"]
            or cost["usage_entry_ref"] != usage["id"]
            or settlement["reservation_ref"] != reservation["id"]
            or settlement["usage_entry_ref"] != usage["id"]
            or settlement["cost_entry_ref"] != cost["id"]
        ):
            raise RecordedReferenceShadowError(
                "page receipt differs from immutable Connector authority"
            )
        observation = receipt["observation"]
        if observation is not None:
            observation = validate_adapter_transport_observation(observation)
            if observation["request_hash"] != receipt["adapter_request_hash"]:
                raise RecordedReferenceShadowError(
                    "page receipt observation does not bind AdapterRequest"
                )
            self._validate_output_observation(context, observation)
            receipt["observation"] = observation
        successful = receipt["attempt_outcome"] == "succeeded"
        artifact_pair = (
            receipt["raw_artifact_version_ref"],
            receipt["raw_artifact_version_hash"],
        )
        result_pair = (
            receipt["page_result_envelope"],
            receipt["page_result_envelope_hash"],
        )
        if successful != all(item is not None for item in artifact_pair + result_pair):
            raise RecordedReferenceShadowError(
                "page receipt success/artifact authority is inconsistent"
            )
        if successful:
            raw = receipt["raw_object"]
            if not isinstance(raw, Mapping) or not self.spool.object_exists(
                raw.get("content_hash", "")
            ):
                raise RecordedReferenceShadowError("page receipt raw object is missing")
            page_result = ResultEnvelope.from_dict(receipt["page_result_envelope"])
            if (
                content_hash(page_result.to_dict()) != receipt["page_result_envelope_hash"]
                or page_result.status != "succeeded"
                or page_result.work_order_ref != context.work_order.id
                or page_result.invocation_ref != context.execution.id
            ):
                raise RecordedReferenceShadowError("page result authority is invalid")
            artifact = self.authority.get_artifact_version(
                receipt["raw_artifact_version_ref"]
            )
            expected_prior = (
                None if prior_receipt is None
                else prior_receipt["raw_artifact_version_ref"]
            )
            if (
                artifact["content_hash"] != receipt["raw_artifact_version_hash"]
                or artifact["artifact_content_hash"] != raw["content_hash"]
                or artifact["producer_execution_ref"] != context.execution.id
                or artifact["result_envelope_ref"] != page_result.id
                or artifact["result_envelope_hash"]
                != receipt["page_result_envelope_hash"]
                or artifact["prior_version_ref"] != expected_prior
            ):
                raise RecordedReferenceShadowError("page artifact chain is invalid")
        elif any(item is not None for item in artifact_pair + result_pair):
            raise RecordedReferenceShadowError("failed page fabricated artifact authority")
        return receipt

    def _validate_parent_completion_payload(
        self,
        parent_request: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> tuple[_ShadowCommitContext, ResultEnvelope, dict[str, Any], list[dict[str, Any]], str | None]:
        journaled = _closed(
            payload,
            {
                "reservation_ref", "response", "result_envelope",
                "result_envelope_hash", "retry_at", "page_receipts",
                "commit_context",
            },
            "parent responded payload",
        )
        context = self._commit_context_from_wire(journaled["commit_context"])
        if context.request != parent_request:
            raise RecordedReferenceShadowError(
                "parent completion context identifies another request"
            )
        result = ResultEnvelope.from_dict(journaled["result_envelope"])
        result_hash = content_hash(result.to_dict())
        response = validate_connector_runner_response(journaled["response"])
        expected_outcome = result.status
        if (
            journaled["result_envelope_hash"] != result_hash
            or response["outcome"] != expected_outcome
            or response["retry_at"] != journaled["retry_at"]
            or response["runner_request_ref"] != parent_request["id"]
            or response["runner_request_hash"] != parent_request["content_hash"]
            or response["connector_invocation_ref"] != context.invocation["id"]
            or response["connector_invocation_hash"]
            != context.invocation["content_hash"]
            or response["result_envelope_ref"] != result.id
            or response["result_envelope_hash"] != result_hash
            or result.work_order_ref != context.work_order.id
            or result.invocation_ref != context.execution.id
        ):
            raise RecordedReferenceShadowError(
                "parent response and ResultEnvelope authority diverge"
            )
        raw_receipts = journaled["page_receipts"]
        if not isinstance(raw_receipts, list) or not raw_receipts:
            raise RecordedReferenceShadowError("parent completion lacks page receipts")
        receipts: list[dict[str, Any]] = []
        for ordinal, item in enumerate(raw_receipts, start=1):
            receipts.append(
                self._validate_page_receipt(
                    item, context, ordinal=ordinal,
                    prior_receipt=None if not receipts else receipts[-1],
                )
            )
        last = receipts[-1]
        expected_key = f"runner-shadow:{context.invocation['id']}:aggregate"
        retained_artifacts = any(
            item["raw_artifact_version_ref"] is not None for item in receipts
        )
        source = None
        if response["source_envelope_ref"] is not None:
            source = self.authority.get_source_envelope(response["source_envelope_ref"])
        if (
            journaled["reservation_ref"] != receipts[0]["reservation_ref"]
            or response["id"]
            != _derived_id(
                "connector-runner-response",
                f"runner-shadow:{context.invocation['id']}:response",
            )
            or result.id != _derived_id("result-envelope", f"{expected_key}:result")
            or response["created_at"] != result.created_at
            or response["physical_attempt_ref"] != last["physical_attempt_ref"]
            or response["physical_attempt_hash"] != last["physical_attempt_hash"]
            or response["usage_entry_ref"] != last["usage_entry_ref"]
            or response["usage_entry_hash"] != last["usage_entry_hash"]
            or response["cost_entry_ref"] != last["cost_entry_ref"]
            or response["cost_entry_hash"] != last["cost_entry_hash"]
            or response["quota_settlement_ref"] != last["quota_settlement_ref"]
            or response["quota_settlement_hash"] != last["quota_settlement_hash"]
            or result.outputs.get("physical_attempt_refs")
            != [item["physical_attempt_ref"] for item in receipts]
            or result.outputs.get("result_physical_attempt_ref")
            != last["physical_attempt_ref"]
            or result.outputs.get("quota_settlement_refs")
            != [item["quota_settlement_ref"] for item in receipts]
            or tuple(result.usage_refs)
            != tuple(item["usage_entry_ref"] for item in receipts)
            or result.outputs.get("connector_invocation_ref")
            != context.invocation["id"]
            or tuple(result.artifact_refs)
            != (
                (context.execution.output_refs[0],)
                if result.status == "succeeded" or retained_artifacts
                else ()
            )
        ):
            raise RecordedReferenceShadowError(
                "parent completion does not bind the exact page authority chain"
            )
        if result.status == "succeeded":
            if (
                source is None
                or source["content_hash"] != response["source_envelope_hash"]
                or source["physical_attempt_refs"]
                != [item["physical_attempt_ref"] for item in receipts]
                or source["raw_artifact_version_ref"]
                != last["raw_artifact_version_ref"]
                or response["raw_artifact_version_ref"]
                != last["raw_artifact_version_ref"]
                or response["raw_artifact_version_hash"]
                != last["raw_artifact_version_hash"]
                or result.outputs.get("source_envelope_ref") != source["id"]
            ):
                raise RecordedReferenceShadowError(
                    "successful parent completion source authority is invalid"
                )
        elif (
            source is not None
            or response["raw_artifact_version_ref"] is not None
            or result.outputs.get("source_envelope_ref") is not None
        ):
            raise RecordedReferenceShadowError(
                "non-success parent completion fabricated source authority"
            )
        return context, result, response, receipts, journaled["retry_at"]

    def execute(
        self,
        request: Mapping[str, Any],
        *,
        scheduler_lease_token: str,
    ) -> dict[str, Any]:
        parent_request = validate_connector_runner_request(request)
        try:
            latest = self.journal.latest(parent_request["id"])
        except RunnerJournalNotFound:
            latest = None
        if latest is not None and latest["state"] == "responded":
            context, result, response, receipts, retry_at = (
                self._validate_parent_completion_payload(
                    parent_request, latest["payload"]
                )
            )
            result_hash = content_hash(result.to_dict())
            self._ensure_scheduler_completion(
                context,
                result,
                result_hash,
                retry_at=retry_at,
            )
            return self._duplicate_response(response)

        if latest is not None and latest["state"] in {
            "transport_started", "observed", "indeterminate_recovered",
        }:
            parent_admission = self._commit_context_from_wire(
                latest["payload"].get("commit_context", {})
            )
        else:
            try:
                live_admission = self.gate.validate(
                    parent_request, scheduler_lease_token=scheduler_lease_token
                )
            except LeaseExpired:
                if latest is not None and latest["state"] == "reserved":
                    reservation_ref = latest["payload"]["reservation_ref"]
                    settlement = self.authority.settle_quota(
                        reservation_ref, "released", usage_entry_ref=None,
                        cost_entry_ref=None,
                        idempotency_key=(
                            f"runner-shadow:{parent_request['connector_invocation_ref']}:"
                            "1:expired-reserved-release"
                        ),
                    )
                    self.journal.append(
                        parent_request["id"], "released_recovered",
                        {
                            "reservation_ref": reservation_ref,
                            "quota_settlement_ref": settlement["id"],
                            "quota_settlement_hash": settlement["content_hash"],
                            "reason": "lease_expired_before_transport",
                        },
                    )
                    self.spool.gc_orphans()
                raise
            self._validate_plan_binding(live_admission)
            parent_admission = self._commit_context(live_admission)
        receipts: list[dict[str, Any]] = []
        prior_cursor: str | None = None
        terminal = False
        all_fresh = True
        for ordinal in range(1, int(self.plan["max_pages"]) + 1):
            page_request = self._page_request(parent_request, ordinal)
            receipt = self._execute_page(
                page_request,
                ordinal=ordinal,
                prior_cursor=prior_cursor,
                prior_receipt=None if not receipts else receipts[-1],
                scheduler_lease_token=scheduler_lease_token,
                is_parent=ordinal == 1,
            )
            page_write_status = receipt.pop("page_write_status")
            all_fresh = all_fresh and page_write_status == "fresh"
            receipts.append(receipt)
            if receipt["attempt_outcome"] != "succeeded":
                return self._finish_failure(
                    parent_admission,
                    receipts,
                    idempotency_status="fresh" if all_fresh else "duplicate",
                )
            observation = receipt["observation"]
            assert observation is not None
            prior_cursor = observation["cursor"]
            if prior_cursor is None:
                terminal = True
                break
        self._validate_normalized_pages(receipts)
        return self._finish_success(
            parent_admission,
            receipts,
            terminal=terminal,
            idempotency_status="fresh" if all_fresh else "duplicate",
        )

    def _validate_plan_binding(self, admission: ValidatedRunnerAdmission) -> None:
        profile = admission.profile
        call = admission.call_spec
        slug = "cninfo" if self.plan["source_ref"] == "source:cninfo" else "sec"
        inventory = load_packaged_connector_inventory()
        template = inventory["templates"][slug]
        inventory_fixture = inventory["fixtures"][slug]
        inventory_entry = next(
            item for item in inventory["index"]["profiles"]
            if item["connector_ref"] == template["connector_ref"]
        )
        if not isinstance(admission.adapter, RecordedSourceFixtureAdapter):
            raise RecordedReferenceShadowError(
                "reference shadow requires the frozen recorded fixture adapter"
            )
        fixture = validate_recorded_source_fixture(
            getattr(admission.adapter, "fixture", {})
        )
        frozen_fixture = load_recorded_source_fixture(slug)
        selected_scenario = next(
            item for item in frozen_fixture["scenarios"]
            if item["scenario"] == self.plan["recorded_scenario"]
        )
        expected_adapter_ref = f"adapter:recorded-reference-shadow:{slug}:0.1"
        expected_adapter_hash = content_hash(
            {
                "adapter_ref": expected_adapter_ref,
                "fixture_hash": frozen_fixture["content_hash"],
            }
        )
        expected_package_ref = (
            f"artifact:runner-package:recorded-reference-shadow:{slug}:0.1"
        )
        expected_package_hash = content_hash(
            {
                "schema_version": "0.1",
                "adapter_ref": expected_adapter_ref,
                "adapter_hash": expected_adapter_hash,
                "recorded_fixture_ref": frozen_fixture["id"],
                "recorded_fixture_hash": frozen_fixture["content_hash"],
            }
        )
        operation = next(
            (
                item for item in template["operations"]
                if item["operation"] == self.plan["operation"]
            ),
            None,
        )
        if (
            self.plan["inventory_template_ref"] != template["id"]
            or self.plan["inventory_template_hash"] != template["content_hash"]
            or self.plan["inventory_fixture_manifest_ref"] != inventory_fixture["id"]
            or self.plan["inventory_fixture_manifest_hash"]
            != inventory_fixture["content_hash"]
            or template["fixture_manifest_ref"] != inventory_fixture["id"]
            or template["fixture_manifest_hash"] != inventory_fixture["content_hash"]
            or inventory_entry["profile_template_ref"] != template["id"]
            or inventory_entry["profile_template_hash"] != template["content_hash"]
            or inventory_entry["fixture_manifest_ref"] != inventory_fixture["id"]
            or inventory_entry["fixture_manifest_hash"] != inventory_fixture["content_hash"]
            or fixture != frozen_fixture
            or self.plan["recorded_fixture_ref"] != frozen_fixture["id"]
            or self.plan["recorded_fixture_hash"] != frozen_fixture["content_hash"]
            or self.plan["recorded_scenario_hash"] != content_hash(selected_scenario)
            or self.plan["recorded_behavior"] != selected_scenario["behavior"]
            or admission.adapter.scenario != self.plan["recorded_scenario"]
            or admission.adapter.scenario_ref != self.plan["recorded_scenario_ref"]
            or admission.adapter.scenario_hash != self.plan["recorded_scenario_hash"]
            or admission.adapter.behavior != self.plan["recorded_behavior"]
            or admission.adapter.selected_case != selected_scenario
            or call["parameters"] != frozen_fixture["parent_parameters"]
            or call["query_hash"] != frozen_fixture["parent_query_hash"]
            or self.plan["transport_target_ref"] != template["transport"]["target_ref"]
            or self.plan["transport_target_hash"] != template["transport"]["target_hash"]
            or self.plan["transport_host_policy"] != template["transport"]["host_policy"]
            or self.plan["adapter_ref"] != expected_adapter_ref
            or self.plan["adapter_hash"] != expected_adapter_hash
            or self.plan["package_manifest_ref"] != expected_package_ref
            or self.plan["package_manifest_hash"] != expected_package_hash
            or fixture["source_ref"] != self.plan["source_ref"]
            or fixture["operation"] != self.plan["operation"]
            or operation is None
            or self.plan["connector_profile_ref"] != profile["id"]
            or self.plan["connector_profile_hash"] != profile["content_hash"]
            or profile["connector_ref"] != template["connector_ref"]
            or self.plan["source_ref"] != profile["source_identity"]["source_ref"]
            or template["source_identity"] != profile["source_identity"]
            or template["connector_ref"] != profile["connector_ref"]
            or profile["allowed_hosts"] != template["transport"]["allowed_hosts"]
            or profile["network_policy"]
            != {
                "allowed_schemes": ["https"], "allow_redirects": False,
                "max_redirects": 0, "resolve_public_only": True,
            }
            or profile["adapter_ref"] != expected_adapter_ref
            or profile["adapter_hash"] != expected_adapter_hash
            or profile["allowed_operations"] != [self.plan["operation"]]
            or set(profile["input_schema_refs"]) != {self.plan["operation"]}
            or set(profile["input_schema_hashes"]) != {self.plan["operation"]}
            or set(profile["output_schema_refs"]) != {self.plan["operation"]}
            or set(profile["output_schema_hashes"]) != {self.plan["operation"]}
            or profile["completeness"]
            != {self.plan["operation"]: "enumerated"}
            or profile["max_response_bytes"] != 1_000_000
            or profile["max_records"] != 1000
            or profile["timeout_ms"] != 5_000
            or profile["runner_runtime_ref"] != "runtime:connector-runner:0.1"
            or profile["runner_actor_ref"] != "runner:connector"
            or profile["credential_slot_refs"] != []
            or profile["access_policy_ref"] != "policy:access:public"
            or profile["retention_policy_ref"] != "policy:retention:filing"
            or profile["terms_policy_ref"] != f"policy:terms:{slug}"
            or admission.binding["adapter_ref"] != expected_adapter_ref
            or admission.binding["adapter_hash"] != expected_adapter_hash
            or self.gate.resolver.manifest["package_manifest_ref"]
            != expected_package_ref
            or self.gate.resolver.manifest["package_manifest_hash"]
            != expected_package_hash
            or self.plan["operation"] != call["operation"]
            or self.plan["operation"] not in profile["allowed_operations"]
            or profile["completeness"][call["operation"]] != "enumerated"
            or profile["pagination"]["mode"] != self.plan["pagination_mode"]
            or profile["pagination"]["cursor_field"] != self.plan["cursor_field"]
            or int(self.plan["max_pages"]) > int(profile["pagination"]["max_pages"])
            or profile["input_schema_refs"][call["operation"]]
            != operation["input_schema_ref"]
            or profile["input_schema_hashes"][call["operation"]]
            != operation["input_schema_hash"]
            or profile["output_schema_refs"][call["operation"]]
            != operation["output_schema_ref"]
            or profile["output_schema_hashes"][call["operation"]]
            != operation["output_schema_hash"]
            or profile["auth_mode"] != "none"
        ):
            raise RecordedReferenceShadowError(
                "reference shadow plan does not exactly bind Runner authority"
            )
        for field in self.plan["bounded_window_fields"]:
            value = call["parameters"].get(field)
            if not isinstance(value, str) or not value:
                raise RecordedReferenceShadowError(
                    "enumerated reference shadow requires a bounded filing window"
                )
        try:
            date_from = date.fromisoformat(call["parameters"]["date_from"])
            date_to = date.fromisoformat(call["parameters"]["date_to"])
        except ValueError as exc:
            raise RecordedReferenceShadowError(
                "filing window dates must use YYYY-MM-DD"
            ) from exc
        if (
            date_from.isoformat() != call["parameters"]["date_from"]
            or date_to.isoformat() != call["parameters"]["date_to"]
        ):
            raise RecordedReferenceShadowError(
                "filing window dates must be canonical"
            )
        if date_from > date_to:
            raise RecordedReferenceShadowError("filing window is reversed")

    @staticmethod
    def _page_request(parent: Mapping[str, Any], ordinal: int) -> dict[str, Any]:
        if ordinal == 1:
            return dict(parent)
        wire = {key: value for key, value in parent.items() if key != "content_hash"}
        wire["id"] = _derived_id("connector-runner-request", f"{parent['id']}:page:{ordinal}")
        wire["idempotency_key"] = f"{parent['idempotency_key']}:page:{ordinal}"
        wire["content_hash"] = content_hash(wire)
        return validate_connector_runner_request(wire)

    def _execute_page(
        self,
        request: Mapping[str, Any],
        *,
        ordinal: int,
        prior_cursor: str | None,
        prior_receipt: Mapping[str, Any] | None,
        scheduler_lease_token: str,
        is_parent: bool,
    ) -> dict[str, Any]:
        try:
            latest = self.journal.latest(request["id"])
        except RunnerJournalNotFound:
            latest = None
        if latest is not None and latest["state"] == "responded":
            receipt = latest["payload"].get("page_receipt")
            if not isinstance(receipt, Mapping):
                raise RecordedReferenceShadowError("page journal lacks its receipt")
            context = self._commit_context_from_wire(
                latest["payload"].get("commit_context", {})
            )
            receipt = self._validate_page_receipt(
                receipt, context, ordinal=ordinal, prior_receipt=prior_receipt
            )
            return {**receipt, "page_write_status": "duplicate"}
        if latest is not None and latest["state"] in {
            "transport_started", "observed", "indeterminate_recovered",
        }:
            admission: ValidatedRunnerAdmission | _ShadowCommitContext = (
                self._commit_context_from_wire(
                    latest["payload"].get("commit_context", {})
                )
            )
        else:
            try:
                live_admission = self.gate.validate(
                    request, scheduler_lease_token=scheduler_lease_token
                )
            except LeaseExpired:
                if latest is not None and latest["state"] == "reserved":
                    reservation_ref = latest["payload"]["reservation_ref"]
                    settlement = self.authority.settle_quota(
                        reservation_ref, "released", usage_entry_ref=None,
                        cost_entry_ref=None,
                        idempotency_key=(
                            f"runner-shadow:{request['connector_invocation_ref']}:"
                            f"{ordinal}:expired-reserved-release"
                        ),
                    )
                    self.journal.append(
                        request["id"], "released_recovered",
                        {
                            "reservation_ref": reservation_ref,
                            "quota_settlement_ref": settlement["id"],
                            "quota_settlement_hash": settlement["content_hash"],
                            "reason": "lease_expired_before_transport",
                        },
                    )
                    self.spool.gc_orphans()
                raise
            self._validate_plan_binding(live_admission)
            admission = live_admission
            if latest is None:
                latest = self.journal.begin_request(live_admission.request)
        assert latest is not None
        if latest["state"] == "observed":
            receipt = self._commit_page(
                admission, latest["payload"], prior_receipt=prior_receipt,
                completion_event=latest,
                idempotency_status="duplicate",
            )
            if not is_parent:
                self.journal.append(
                    request["id"], "responded",
                    {
                        "reservation_ref": receipt["reservation_ref"],
                        "page_receipt": receipt,
                        "commit_context": self._commit_context_wire(admission),
                    },
                )
                self._fault("after_page_responded")
            self._validate_page_cursor(receipt, ordinal, prior_cursor)
            return {**receipt, "page_write_status": "duplicate"}
        if latest["state"] in {"transport_started", "indeterminate_recovered"}:
            if latest["state"] == "indeterminate_recovered":
                receipt = latest["payload"].get("page_receipt")
                if not isinstance(receipt, Mapping):
                    raise RecordedReferenceShadowError(
                        "indeterminate page recovery lacks a closed receipt"
                    )
                receipt = self._validate_page_receipt(
                    receipt, admission, ordinal=ordinal,
                    prior_receipt=prior_receipt,
                )
            else:
                recovered = {
                    **dict(latest["payload"]),
                    "completed_at": _wire_time(self.clock()),
                    "attempt_outcome": "indeterminate",
                    "retry_at": None,
                    "observation": None,
                    "raw_object": None,
                    "error": {
                        "code": "transport_indeterminate",
                        "message": "runner exited after durable transport start",
                        "retryable": True,
                    },
                }
                receipt = self._commit_page(
                    admission, recovered, prior_receipt=prior_receipt,
                    completion_event=latest,
                    idempotency_status="duplicate",
                )
                self.journal.append(
                    request["id"], "indeterminate_recovered",
                    {
                        "reservation_ref": receipt["reservation_ref"],
                        "page_receipt": receipt,
                        "commit_context": self._commit_context_wire(admission),
                    },
                )
                self.spool.gc_orphans()
            self._validate_page_cursor(receipt, ordinal, prior_cursor)
            return {**dict(receipt), "page_write_status": "duplicate"}
        if latest["state"] not in {"admitted", "reserved"}:
            raise RecordedReferenceShadowError(
                f"reference page requires recovery from {latest['state']}"
            )
        if latest["state"] == "admitted":
            reservation = self.gate.reserve_for_admission(
                admission,
                scheduler_lease_token=scheduler_lease_token,
                idempotency_key=f"runner-shadow:{admission.invocation['id']}:{ordinal}:reserve",
            )
            self.journal.append(
                request["id"], "reserved",
                {
                    "reservation_ref": reservation["id"],
                    "reservation_hash": reservation["content_hash"],
                    "physical_attempt_number": reservation["physical_attempt_number"],
                },
            )
            self._fault("after_page_reserved")
        else:
            reservation = self.authority.get_reservation(
                latest["payload"]["reservation_ref"]
            )
            if (
                reservation["content_hash"] != latest["payload"]["reservation_hash"]
                or int(reservation["physical_attempt_number"]) != ordinal
            ):
                raise RecordedReferenceShadowError(
                    "reserved page journal differs from quota authority"
                )
        adapter_request = self.gate.build_adapter_request(
            admission, scheduler_lease_token=scheduler_lease_token
        )
        if int(adapter_request["physical_attempt_number"]) != ordinal:
            raise RecordedReferenceShadowError(
                "recorded page ordinal does not match physical attempt number"
            )
        adapter_request = self._paginated_adapter_request(
            adapter_request,
            admission,
            ordinal=ordinal,
            prior_receipt=prior_receipt,
        )
        try:
            raw_sink = self.spool.open_sink(
                adapter_request["raw_sink_ref"],
                max_response_bytes=adapter_request["max_response_bytes"],
            )
        except RawSpoolCapacityError:
            failed_at = _wire_time(self.clock())
            payload = {
                "reservation_ref": reservation["id"],
                "reservation_hash": reservation["content_hash"],
                "physical_attempt_number": ordinal,
                "adapter_request_hash": adapter_request["content_hash"],
                "started_at": failed_at, "completed_at": failed_at,
                "attempt_outcome": "failed", "retry_at": None,
                "observation": None, "raw_object": None,
                "commit_context": self._commit_context_wire(admission),
                "error": {
                    "code": "spool_capacity",
                    "message": "raw spool capacity is unavailable",
                    "retryable": True,
                },
            }
            observed_event = self.journal.append(
                request["id"], "observed", payload, event_at=failed_at,
            )
            receipt = self._commit_page(
                admission, payload, prior_receipt=prior_receipt,
                completion_event=observed_event,
                idempotency_status="fresh",
            )
            self._validate_page_cursor(receipt, ordinal, prior_cursor)
            if not is_parent:
                self.journal.append(
                    request["id"], "responded",
                    {
                        "reservation_ref": reservation["id"],
                        "page_receipt": receipt,
                        "commit_context": self._commit_context_wire(admission),
                    },
                )
            return {**receipt, "page_write_status": "fresh"}
        started_at = _wire_time(self.clock())
        self.journal.append(
            request["id"], "transport_started",
            {
                "reservation_ref": reservation["id"],
                "reservation_hash": reservation["content_hash"],
                "physical_attempt_number": ordinal,
                "adapter_request_hash": adapter_request["content_hash"],
                "started_at": started_at,
                "raw_sink_ref": adapter_request["raw_sink_ref"],
                "prior_cursor": prior_cursor,
                "commit_context": self._commit_context_wire(admission),
            },
            event_at=started_at,
        )
        self._fault("after_page_transport_started")
        observation: dict[str, Any] | None = None
        raw_object: RawObject | None = None
        error: dict[str, Any] | None = None
        try:
            returned = invoke_adapter_with_deadline(
                admission.adapter, adapter_request, raw_sink, clock=self.clock
            )
            completed_at = _wire_time(self.clock())
            if _parse_time(completed_at) >= _parse_time(adapter_request["deadline_at"]):
                raw_sink.abort()
                outcome = "timeout"
                error = {
                    "code": "deadline_exceeded",
                    "message": "recorded source page exceeded its authority deadline",
                    "retryable": True,
                }
            else:
                observation = validate_adapter_transport_observation(returned)
                if observation["request_hash"] != adapter_request["content_hash"]:
                    raise RecordedReferenceShadowError(
                        "recorded page observation does not bind AdapterRequest"
                    )
                self._validate_output_observation(admission, observation)
                outcome = observation["outcome"]
                if outcome == "succeeded":
                    raw_object = raw_sink.finalize()
                else:
                    raw_sink.abort()
        except AdapterDeadlineExceeded:
            completed_at = _wire_time(self.clock())
            raw_sink.abort()
            outcome = "timeout"
            error = {
                "code": "deadline_exceeded", "message": "recorded source page timed out",
                "retryable": True,
            }
        except RawSpoolLimitExceeded as exc:
            completed_at = _wire_time(self.clock())
            raw_sink.abort()
            outcome = "failed"
            error = {"code": "response_too_large", "message": str(exc), "retryable": True}
        except RecordedSourceError as exc:
            completed_at = _wire_time(self.clock())
            raw_sink.abort()
            outcome = "failed"
            code = (
                self.plan["recorded_scenario"]
                if self.plan["recorded_behavior"] == "normalize_error"
                else "adapter_failure"
            )
            error = {"code": code, "message": str(exc), "retryable": True}
        except Exception as exc:
            completed_at = _wire_time(self.clock())
            raw_sink.abort()
            observation = None
            outcome = "failed"
            error = {
                "code": "adapter_failure", "message": f"{type(exc).__name__}: {exc}",
                "retryable": True,
            }
        retry_at = None
        if outcome == "rate_limited":
            assert observation is not None
            retry_at = _wire_time(
                _parse_time(completed_at)
                + timedelta(milliseconds=int(observation["retry_after_ms"]))
            )
        payload = {
            "reservation_ref": reservation["id"],
            "reservation_hash": reservation["content_hash"],
            "physical_attempt_number": ordinal,
            "adapter_request_hash": adapter_request["content_hash"],
            "started_at": started_at,
            "completed_at": completed_at,
            "attempt_outcome": outcome,
            "retry_at": retry_at,
            "observation": observation,
            "raw_object": None if raw_object is None else raw_object.to_dict(),
            "error": error,
            "commit_context": self._commit_context_wire(admission),
        }
        observed_event = self.journal.append(
            request["id"], "observed", payload, event_at=completed_at
        )
        self._fault("after_page_observed")
        receipt = self._commit_page(
            admission, payload, prior_receipt=prior_receipt,
            completion_event=observed_event,
            idempotency_status="fresh",
        )
        self._validate_page_cursor(receipt, ordinal, prior_cursor)
        if not is_parent:
            self.journal.append(
                request["id"], "responded",
                {
                    "reservation_ref": reservation["id"],
                    "page_receipt": receipt,
                    "commit_context": self._commit_context_wire(admission),
                },
            )
            self._fault("after_page_responded")
        return {**receipt, "page_write_status": "fresh"}

    def _paginated_adapter_request(
        self,
        base_request: Mapping[str, Any],
        admission: ValidatedRunnerAdmission,
        *,
        ordinal: int,
        prior_receipt: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Derive a page-specific 0.2 request from immutable prior authority."""

        parameters = dict(admission.call_spec["parameters"])
        cursor_field = self.plan["cursor_field"]
        request_cursor: str | None = None
        prior_adapter_request_hash = None
        prior_observation_hash = None
        prior_attempt_ref = None
        prior_attempt_hash = None
        if ordinal == 1:
            if prior_receipt is not None:
                raise RecordedReferenceShadowError("first page cannot have a prior receipt")
            if cursor_field == "page" and parameters.get(cursor_field) != 1:
                raise RecordedReferenceShadowError("page pagination must start at page one")
            if cursor_field == "cursor" and parameters.get(cursor_field) not in {None, ""}:
                raise RecordedReferenceShadowError("cursor pagination must start without a cursor")
        else:
            if prior_receipt is None:
                raise RecordedReferenceShadowError("continued page lacks prior authority")
            observation = validate_adapter_transport_observation(
                prior_receipt.get("observation")
            )
            request_cursor = observation["cursor"]
            if request_cursor is None:
                raise RecordedReferenceShadowError(
                    "terminal observation cannot authorize another page"
                )
            prior_adapter_request_hash = _text(
                prior_receipt.get("adapter_request_hash"), "adapter_request_hash"
            )
            if observation["request_hash"] != prior_adapter_request_hash:
                raise RecordedReferenceShadowError(
                    "prior observation does not bind its adapter request"
                )
            prior_observation_hash = observation["content_hash"]
            prior_attempt_ref = _text(
                prior_receipt.get("physical_attempt_ref"), "physical_attempt_ref"
            )
            prior_attempt_hash = _text(
                prior_receipt.get("physical_attempt_hash"), "physical_attempt_hash"
            )
            attempt = self.authority.get_physical_attempt(prior_attempt_ref)
            if attempt is None:
                raise RecordedReferenceShadowError("prior physical attempt is missing")
            if (
                attempt["content_hash"] != prior_attempt_hash
                or attempt["connector_invocation_ref"] != admission.invocation["id"]
                or int(attempt["physical_attempt_number"]) != ordinal - 1
                or attempt["outcome"] != "succeeded"
            ):
                raise RecordedReferenceShadowError(
                    "prior physical attempt cannot authorize pagination"
                )
            if cursor_field == "page":
                try:
                    page = int(request_cursor)
                except ValueError as exc:
                    raise RecordedReferenceShadowError(
                        "recorded page cursor is not an integer"
                    ) from exc
                if str(page) != request_cursor or page != ordinal:
                    raise RecordedReferenceShadowError(
                        "recorded page cursor is not contiguous"
                    )
                parameters[cursor_field] = page
            else:
                parameters[cursor_field] = request_cursor
        self.gate.resolver.validate_input(admission.binding, parameters)
        wire = dict(base_request)
        wire.pop("content_hash", None)
        wire.update(
            {
                "protocol_version": "0.2",
                "parameters": parameters,
                "query_hash": content_hash(
                    {"operation": admission.call_spec["operation"], "parameters": parameters}
                ),
                "pagination_authority": {
                    "mode": self.plan["pagination_mode"],
                    "cursor_field": cursor_field,
                    "page_ordinal": ordinal,
                    "request_cursor": request_cursor,
                    "parent_query_hash": admission.call_spec["query_hash"],
                    "recorded_scenario_ref": self.plan["recorded_scenario_ref"],
                    "recorded_scenario_hash": self.plan["recorded_scenario_hash"],
                    "recorded_behavior": self.plan["recorded_behavior"],
                    "prior_adapter_request_hash": prior_adapter_request_hash,
                    "prior_observation_hash": prior_observation_hash,
                    "prior_physical_attempt_ref": prior_attempt_ref,
                    "prior_physical_attempt_hash": prior_attempt_hash,
                },
            }
        )
        wire["content_hash"] = content_hash(wire)
        return validate_connector_adapter_request(wire)

    def _validate_output_observation(
        self,
        admission: ValidatedRunnerAdmission,
        observation: Mapping[str, Any],
    ) -> None:
        slug = "cninfo" if self.plan["source_ref"] == "source:cninfo" else "sec"
        template = load_packaged_connector_inventory()["templates"][slug]
        operation = next(
            item for item in template["operations"]
            if item["operation"] == admission.call_spec["operation"]
        )
        document = next(
            item for item in template["schema_documents"]
            if item["schema_ref"] == operation["output_schema_ref"]
        )
        if (
            operation["output_schema_ref"]
            != admission.profile["output_schema_refs"][admission.call_spec["operation"]]
            or operation["output_schema_hash"] != document["schema_hash"]
            or document["schema_hash"] != content_hash(document["document"])
        ):
            raise RecordedReferenceShadowError("frozen output schema graph is invalid")
        structured = observation["structured_output"]
        _validate_schema_instance(structured, document["document"])
        if (
            structured["source_record_refs"] != observation["source_record_refs"]
            or structured["next_cursor"] != observation["cursor"]
            or structured["provider_status"] != observation["provider_status_code"]
        ):
            raise RecordedReferenceShadowError(
                "normalized page output does not bind transport observation"
            )

    @staticmethod
    def _validate_page_cursor(
        receipt: Mapping[str, Any], ordinal: int, prior_cursor: str | None
    ) -> None:
        observation = receipt["observation"]
        if observation is None:
            return
        structured = observation["structured_output"]
        if (
            structured.get("page_ordinal") != ordinal
            or structured.get("request_cursor") != prior_cursor
        ):
            raise RecordedReferenceShadowError(
                "recorded page cursor/ordinal chain is not exact"
            )

    def _commit_page(
        self,
        admission: ValidatedRunnerAdmission | _ShadowCommitContext,
        observed: Mapping[str, Any],
        *,
        prior_receipt: Mapping[str, Any] | None,
        completion_event: Mapping[str, Any],
        idempotency_status: str,
    ) -> dict[str, Any]:
        ordinal = int(observed["physical_attempt_number"])
        key = f"runner-shadow:{admission.invocation['id']}:{ordinal}"
        observation = observed["observation"]
        provider_request_id = None if observation is None else observation["provider_request_id"]
        attempt = self.authority.record_physical_attempt(
            admission.invocation["id"], observed["reservation_ref"], ordinal,
            observed["attempt_outcome"], started_at=observed["started_at"],
            completed_at=observed["completed_at"], provider_request_id=provider_request_id,
            retry_at=observed["retry_at"], idempotency_key=f"{key}:attempt",
        )
        successful = observed["attempt_outcome"] == "succeeded"
        final_usage = observed["attempt_outcome"] in {"succeeded", "rate_limited"}
        raw = observed["raw_object"]
        records = [] if observation is None else observation["source_record_refs"]
        usage = self.authority.record_usage(
            attempt["id"],
            {
                "calls": 1,
                "bytes": int(raw["size_bytes"]) if successful else 0,
                "records": len(records) if successful else 0,
                "cost_micros": 0,
            },
            measurement_status="final" if final_usage else "unavailable",
            metering_source="runner_measured" if final_usage else "estimated",
            provider_usage_ref=(
                None if observation is None or observation["provider_usage"] is None
                else "provider-usage:" + content_hash(observation["provider_usage"])
            ),
            idempotency_key=f"{key}:usage",
        )
        cost = self.authority.record_cost(
            usage["id"], cost_status="actual" if final_usage else "estimated",
            calculation_ref="calculator:recorded-reference-shadow:0.1",
            actor_ref="system:connector-runner", idempotency_key=f"{key}:cost",
        )
        settlement = self.authority.settle_quota(
            observed["reservation_ref"],
            "consumed" if final_usage else "indeterminate",
            usage_entry_ref=usage["id"], cost_entry_ref=cost["id"],
            idempotency_key=f"{key}:settle",
        )
        artifact = None
        page_result_wire = None
        page_result_hash = None
        if successful:
            if raw is None or not self.spool.object_exists(raw["content_hash"]):
                raise RecordedReferenceShadowError(
                    "successful page lacks its finalized raw object"
                )
            if len(admission.execution.output_refs) != 1:
                raise RecordedReferenceShadowError(
                    "recorded reference execution requires one raw artifact output"
                )
            artifact_ref = admission.execution.output_refs[0]
            page_result = ResultEnvelope(
                schema_version="0.1",
                id=_derived_id("result-envelope", f"{key}:page-result"),
                created_at=observed["completed_at"],
                work_order_ref=admission.work_order.id,
                invocation_ref=admission.execution.id,
                status="succeeded",
                outputs={
                    "connector_invocation_ref": admission.invocation["id"],
                    "physical_attempt_ref": attempt["id"],
                    "quota_settlement_ref": settlement["id"],
                    "source_envelope_ref": None,
                    "page_ordinal": ordinal,
                },
                actual_side_effects=("read:recorded-fixture",),
                usage_refs=(usage["id"],), artifact_refs=(artifact_ref,), error=None,
                metadata={
                    "runner_request_ref": admission.request["id"],
                    "recorded_page_commit": True,
                },
            )
            page_result_wire = page_result.to_dict()
            page_result_hash = content_hash(page_result_wire)
            prior_artifact_ref = (
                None
                if prior_receipt is None
                else prior_receipt.get("raw_artifact_version_ref")
            )
            artifact = self.authority.register_artifact_version_v2(
                artifact_ref,
                title=f"Recorded {self.plan['source_ref']} response page {ordinal}",
                kind="connector_raw_response", media_type="application/json",
                artifact_content_hash=raw["content_hash"],
                size_bytes=int(raw["size_bytes"]),
                storage_locator=raw["storage_locator"],
                producer_execution_ref=admission.execution.id,
                result_envelope_ref=page_result.id,
                result_envelope_hash=page_result_hash,
                access_class="internal", preview_status="unavailable",
                actor_ref="system:connector-runner",
                prior_version_ref=prior_artifact_ref,
                version_id=_derived_id("artifact-version", f"{key}:artifact"),
                idempotency_key=f"{key}:artifact",
            )
            self._fault("after_page_artifact_recorded")
        return {
            "idempotency_status": idempotency_status,
            "runner_request_ref": admission.request["id"],
            "runner_request_hash": admission.request["content_hash"],
            "reservation_ref": observed["reservation_ref"],
            "physical_attempt_ref": attempt["id"],
            "physical_attempt_hash": attempt["content_hash"],
            "adapter_request_hash": observed["adapter_request_hash"],
            "usage_entry_ref": usage["id"], "usage_entry_hash": usage["content_hash"],
            "cost_entry_ref": cost["id"], "cost_entry_hash": cost["content_hash"],
            "quota_settlement_ref": settlement["id"],
            "quota_settlement_hash": settlement["content_hash"],
            "attempt_outcome": observed["attempt_outcome"],
            "retry_at": observed["retry_at"], "observation": observation,
            "raw_object": raw, "error": observed["error"],
            "raw_artifact_version_ref": None if artifact is None else artifact["id"],
            "raw_artifact_version_hash": (
                None if artifact is None else artifact["content_hash"]
            ),
            "page_result_envelope": page_result_wire,
            "page_result_envelope_hash": page_result_hash,
            "completion_event_ref": completion_event["id"],
            "completion_event_hash": completion_event["content_hash"],
            "completion_event_at": completion_event["event_at"],
        }

    @staticmethod
    def _validate_normalized_pages(receipts: list[Mapping[str, Any]]) -> None:
        seen: set[str] = set()
        for receipt in receipts:
            observation = receipt["observation"]
            if observation is None:
                continue
            records = observation["structured_output"]["records"]
            if not isinstance(records, list):
                raise RecordedReferenceShadowError("normalized page records must be an array")
            if [record.get("record_ref") for record in records] != observation[
                "source_record_refs"
            ]:
                raise RecordedReferenceShadowError(
                    "normalized records do not bind source_record_refs"
                )
            for record in records:
                if not isinstance(record, Mapping) or set(record) != {
                    "record_ref", "revision_of_ref", "record_hash"
                }:
                    raise RecordedReferenceShadowError("normalized record shape is open")
                record_ref = _text(record["record_ref"], "record_ref")
                if record_ref in seen:
                    raise RecordedReferenceShadowError("recorded pages contain duplicate records")
                prior = record["revision_of_ref"]
                if prior is not None and prior not in seen:
                    raise RecordedReferenceShadowError(
                        "recorded revision chain points outside earlier pages"
                    )
                seen.add(record_ref)

    def _finish_success(
        self,
        admission: ValidatedRunnerAdmission | _ShadowCommitContext,
        receipts: list[Mapping[str, Any]],
        *,
        terminal: bool,
        idempotency_status: str,
    ) -> dict[str, Any]:
        last = receipts[-1]
        all_records = [
            ref
            for receipt in receipts
            for ref in receipt["observation"]["source_record_refs"]
        ]
        completeness = "enumerated" if terminal else "partial"
        status = "empty" if terminal and not all_records else (
            "complete" if terminal else "partial"
        )
        key = f"runner-shadow:{admission.invocation['id']}:aggregate"
        source_id = _derived_id("source-envelope", f"{key}:source")
        result_id = _derived_id("result-envelope", f"{key}:result")
        artifact_ref = admission.execution.output_refs[0]
        result = ResultEnvelope(
            schema_version="0.1", id=result_id, created_at=last["observation"]["structured_output"].get("retrieved_at", _wire_time(self.clock())),
            work_order_ref=admission.work_order.id, invocation_ref=admission.execution.id,
            status="succeeded",
            outputs={
                "connector_invocation_ref": admission.invocation["id"],
                "physical_attempt_refs": [item["physical_attempt_ref"] for item in receipts],
                "result_physical_attempt_ref": last["physical_attempt_ref"],
                "quota_settlement_refs": [item["quota_settlement_ref"] for item in receipts],
                "source_envelope_ref": source_id,
            },
            actual_side_effects=("read:recorded-fixture",),
            usage_refs=tuple(item["usage_entry_ref"] for item in receipts),
            artifact_refs=(artifact_ref,), error=None,
            metadata={
                "runner_request_ref": admission.request["id"],
                "physical_attempt_count": len(receipts),
                "terminal_cursor_observed": terminal,
                "completeness": completeness,
            },
        )
        result_wire = result.to_dict()
        result_hash = content_hash(result_wire)
        artifacts: list[dict[str, Any]] = []
        for ordinal, receipt in enumerate(receipts, start=1):
            raw = receipt["raw_object"]
            if raw is None or not self.spool.object_exists(raw["content_hash"]):
                raise RecordedReferenceShadowError("successful page lacks finalized raw artifact")
            artifact_ref_id = receipt["raw_artifact_version_ref"]
            if artifact_ref_id is None:
                raise RecordedReferenceShadowError(
                    "successful page receipt lacks its artifact authority"
                )
            artifact = self.authority.get_artifact_version(artifact_ref_id)
            expected_prior = (
                None if ordinal == 1
                else receipts[ordinal - 2]["raw_artifact_version_ref"]
            )
            if (
                artifact["content_hash"] != receipt["raw_artifact_version_hash"]
                or artifact["artifact_ref"] != artifact_ref
                or artifact["artifact_content_hash"] != raw["content_hash"]
                or artifact["prior_version_ref"] != expected_prior
                or artifact["result_envelope_ref"]
                != receipt["page_result_envelope"]["id"]
                or artifact["result_envelope_hash"]
                != receipt["page_result_envelope_hash"]
            ):
                raise RecordedReferenceShadowError(
                    "page artifact differs from its immutable receipt chain"
                )
            artifacts.append(artifact)
        final_raw = last["raw_object"]
        final_artifact = artifacts[-1]
        retrieved_at = _wire_time(self.clock())
        source_spec = {
            "schema_version": "0.1", "id": source_id, "created_at": retrieved_at,
            "connector_invocation_ref": admission.invocation["id"],
            "connector_profile_ref": admission.profile["id"],
            "physical_attempt_refs": [item["physical_attempt_ref"] for item in receipts],
            "result_physical_attempt_ref": last["physical_attempt_ref"],
            "source": admission.profile["source_identity"]["source_ref"],
            "operation": admission.call_spec["operation"],
            "source_record_refs": all_records,
            "published_at": None, "updated_at": None, "as_of": None,
            "retrieved_at": retrieved_at,
            "cursor": last["observation"]["cursor"],
            "provider_request_id": last["observation"]["provider_request_id"],
            "raw_artifact_version_ref": final_artifact["id"],
            "raw_response_hash": final_raw["content_hash"],
            "source_schema_hash": admission.profile["output_schema_hashes"][admission.call_spec["operation"]],
            "source_content_hash": "", "completeness": completeness, "status": status,
            "access_policy_ref": admission.profile["access_policy_ref"],
            "retention_policy_ref": admission.profile["retention_policy_ref"],
            "terms_policy_ref": admission.profile["terms_policy_ref"], "error": None,
        }
        source_spec["source_content_hash"] = source_envelope_content_hash(source_spec)
        source = self.authority.record_source_envelope(
            source_spec, idempotency_key=f"{key}:source"
        )
        response = self._response(
            admission, last, result, result_hash,
            artifact=final_artifact, source=source, outcome="succeeded",
            retry_at=None, idempotency_status=idempotency_status,
        )
        journal_event = self.journal.append(
            admission.request["id"], "responded",
            {
                "reservation_ref": receipts[0]["reservation_ref"],
                "response": response, "result_envelope": result_wire,
                "result_envelope_hash": result_hash, "retry_at": None,
                "page_receipts": [dict(item) for item in receipts],
                "commit_context": self._commit_context_wire(admission),
            },
        )
        verified_context, verified_result, verified_response, _, _ = (
            self._validate_parent_completion_payload(
                admission.request, journal_event["payload"]
            )
        )
        self._fault("after_response_journaled")
        self._ensure_scheduler_completion(
            verified_context, verified_result, result_hash, retry_at=None,
        )
        self._fault("after_scheduler_completed")
        return verified_response

    def _finish_failure(
        self,
        admission: ValidatedRunnerAdmission | _ShadowCommitContext,
        receipts: list[Mapping[str, Any]],
        *,
        idempotency_status: str,
    ) -> dict[str, Any]:
        last = receipts[-1]
        key = f"runner-shadow:{admission.invocation['id']}:aggregate"
        retry_at = last["retry_at"] or _wire_time(
            self.clock() + timedelta(seconds=self.retry_backoff_seconds)
        )
        retained_artifacts = any(
            item.get("raw_artifact_version_ref") is not None for item in receipts
        )
        result = ResultEnvelope(
            schema_version="0.1", id=_derived_id("result-envelope", f"{key}:result"),
            created_at=_wire_time(self.clock()), work_order_ref=admission.work_order.id,
            invocation_ref=admission.execution.id, status="retryable",
            outputs={
                "connector_invocation_ref": admission.invocation["id"],
                "physical_attempt_refs": [item["physical_attempt_ref"] for item in receipts],
                "result_physical_attempt_ref": last["physical_attempt_ref"],
                "quota_settlement_refs": [item["quota_settlement_ref"] for item in receipts],
                "source_envelope_ref": None,
            },
            actual_side_effects=("read:recorded-fixture",),
            usage_refs=tuple(item["usage_entry_ref"] for item in receipts),
            artifact_refs=(admission.execution.output_refs[0],) if retained_artifacts else (),
            error=last["error"] or {
                "code": last["attempt_outcome"], "message": "recorded source page failed",
                "retryable": True,
            },
            metadata={
                "runner_request_ref": admission.request["id"],
                "physical_attempt_count": len(receipts), "completeness": "unknown",
            },
        )
        result_hash = content_hash(result.to_dict())
        response = self._response(
            admission, last, result, result_hash, artifact=None, source=None,
            outcome="retryable", retry_at=retry_at,
            idempotency_status=idempotency_status,
        )
        journal_event = self.journal.append(
            admission.request["id"], "responded",
            {
                "reservation_ref": receipts[0]["reservation_ref"],
                "response": response, "result_envelope": result.to_dict(),
                "result_envelope_hash": result_hash, "retry_at": retry_at,
                "page_receipts": [dict(item) for item in receipts],
                "commit_context": self._commit_context_wire(admission),
            },
        )
        (
            verified_context, verified_result, verified_response,
            _, verified_retry_at,
        ) = self._validate_parent_completion_payload(
            admission.request, journal_event["payload"]
        )
        self._fault("after_response_journaled")
        self._ensure_scheduler_completion(
            verified_context, verified_result, result_hash,
            retry_at=verified_retry_at,
        )
        self._fault("after_scheduler_completed")
        return verified_response

    def _ensure_scheduler_completion(
        self,
        context: ValidatedRunnerAdmission | _ShadowCommitContext,
        result: ResultEnvelope,
        result_hash: str,
        *,
        retry_at: str | None,
    ) -> None:
        request = context.request
        row = self.authority.get_scheduler_result(result.id)
        expected = (
            result.work_order_ref,
            int(request["scheduler_attempt_number"]),
            result_hash,
            result.status,
        )
        if row is not None:
            observed = (
                row["work_order_id"], int(row["attempt_number"]),
                row["result_envelope_hash"], row["outcome"],
            )
            if observed != expected:
                raise RecordedReferenceShadowError(
                    "scheduler completion conflicts with journaled result"
                )
            return
        completion = self.authority.reconcile_journaled_completion(
            result.work_order_ref,
            int(request["scheduler_attempt_number"]),
            request["runner_actor_ref"],
            result,
            lease_revision_ref=request["scheduler_lease_revision_ref"],
            lease_hash=request["scheduler_lease_hash"],
            work_order_hash=request["work_order_hash"],
            idempotency_key=(
                f"runner-shadow:{request['connector_invocation_ref']}:aggregate:"
                "scheduler-journal-reconcile"
            ),
            result_envelope_hash=result_hash,
            retry_at=retry_at,
        )
        if completion.get("status") not in {"fresh", "duplicate"}:
            raise RecordedReferenceShadowError("scheduler completion did not converge")
        stored = self.authority.get_scheduler_result(result.id)
        if stored is None or (
            stored["work_order_id"], int(stored["attempt_number"]),
            stored["result_envelope_hash"], stored["outcome"],
        ) != expected:
            raise RecordedReferenceShadowError("scheduler completion receipt is missing")

    def _fault(self, barrier: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(barrier)

    @staticmethod
    def _response(
        admission: ValidatedRunnerAdmission | _ShadowCommitContext,
        last: Mapping[str, Any],
        result: ResultEnvelope,
        result_hash: str,
        *,
        artifact: Mapping[str, Any] | None,
        source: Mapping[str, Any] | None,
        outcome: str,
        retry_at: str | None,
        idempotency_status: str,
    ) -> dict[str, Any]:
        base = {
            "schema_version": "0.2",
            "id": _derived_id(
                "connector-runner-response",
                f"runner-shadow:{admission.invocation['id']}:response",
            ),
            "created_at": result.created_at,
            "runner_request_ref": admission.request["id"],
            "runner_request_hash": admission.request["content_hash"],
            "idempotency_status": idempotency_status,
            "connector_invocation_ref": admission.invocation["id"],
            "connector_invocation_hash": admission.invocation["content_hash"],
            "physical_attempt_ref": last["physical_attempt_ref"],
            "physical_attempt_hash": last["physical_attempt_hash"],
            "usage_entry_ref": last["usage_entry_ref"],
            "usage_entry_hash": last["usage_entry_hash"],
            "cost_entry_ref": last["cost_entry_ref"],
            "cost_entry_hash": last["cost_entry_hash"],
            "quota_settlement_ref": last["quota_settlement_ref"],
            "quota_settlement_hash": last["quota_settlement_hash"],
            "raw_artifact_version_ref": None if artifact is None else artifact["id"],
            "raw_artifact_version_hash": None if artifact is None else artifact["content_hash"],
            "source_envelope_ref": None if source is None else source["id"],
            "source_envelope_hash": None if source is None else source["content_hash"],
            "result_envelope_ref": result.id, "result_envelope_hash": result_hash,
            "outcome": outcome, "retry_at": retry_at,
        }
        base["content_hash"] = content_hash(base)
        return validate_connector_runner_response(base)

    @staticmethod
    def _duplicate_response(response: Mapping[str, Any]) -> dict[str, Any]:
        duplicate = dict(response)
        duplicate["idempotency_status"] = "duplicate"
        duplicate.pop("content_hash", None)
        duplicate["content_hash"] = content_hash(duplicate)
        return validate_connector_runner_response(duplicate)


__all__ = [
    "RecordedReferenceShadowCoordinator", "RecordedReferenceShadowError",
    "validate_recorded_reference_shadow_plan",
]
