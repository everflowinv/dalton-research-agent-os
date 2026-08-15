"""Separate runner wire for host-owned authenticated MCP connectors.

This module does not implement MCP networking.  It freezes an offline
AlphaEngine reference shadow, authorizes one exact credential use, and passes
an opaque handle to an injected adapter only after a final revoke/expiry check.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Callable

from .connector_inventory import load_packaged_connector_inventory
from .connector_runner import (
    ConnectorRunnerAdmissionGate,
    RunnerConflict,
    RunnerValidationError,
    ValidatedRunnerAdmission,
)
from .credential_authority import CredentialAuthorityPort
from .recorded_alphaengine_adapter import (
    ALPHAENGINE_FIXTURE_CREATED_AT,
    load_recorded_alphaengine_fixture,
    recorded_alphaengine_scenario,
)
from .store import canonical_json, content_hash


MCP_MANAGED_PROTOCOL_VERSION = "0.2"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_RAW_SINK_RE = re.compile(r"^raw-sink:[0-9a-f]{64}$")
_REF_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9+._-]*:[A-Za-z0-9][A-Za-z0-9:._-]*$"
)
_SOURCE_STATUSES = frozenset({"complete", "partial", "empty"})
_COMPLETENESS = frozenset({"enumerated", "ranked", "partial", "unknown"})
_BEHAVIORS = frozenset(
    {
        "return",
        "rate_limited",
        "timeout",
        "normalize_error",
        "permission_denied",
        "revoked",
    }
)


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RunnerValidationError(f"{name} must be an object")
    missing = fields - set(value)
    unknown = set(value) - fields
    if missing or unknown:
        raise RunnerValidationError(
            f"{name} closed shape mismatch: missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise RunnerValidationError(f"{name} must be finite JSON") from exc


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RunnerValidationError(f"{name} must be a non-empty string")
    return value


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if _HASH_RE.fullmatch(value) is None:
        raise RunnerValidationError(f"{name} must be lowercase SHA-256 hex")
    return value


def _ref(value: Any, name: str) -> str:
    value = _text(value, name)
    compact = re.sub(r"[^a-z0-9]+", "", value.lower())
    if (
        _REF_RE.fullmatch(value) is None
        or value.startswith(("file:", "path:", "http:", "https:"))
        or any(
            marker in compact
            for marker in (
                "systemprompt", "apikey", "accesstoken", "refreshtoken",
                "password", "cookiematerial", "secretmaterial",
                "credentialvalue", "oauthconfig", "serverconfig",
            )
        )
    ):
        raise RunnerValidationError(f"{name} must be an opaque namespaced ref")
    return value


def _timestamp(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RunnerValidationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise RunnerValidationError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RunnerValidationError(f"{name} must be an integer >= {minimum}")
    return value


def _nullable_text(value: Any, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _validate_hash(wire: Mapping[str, Any], name: str) -> None:
    declared = _hash(wire["content_hash"], f"{name}.content_hash")
    expected = content_hash(
        {key: value for key, value in wire.items() if key != "content_hash"}
    )
    if declared != expected:
        raise RunnerConflict(f"{name} content_hash mismatch")


@lru_cache(maxsize=16)
def _recorded_mcp_shadow_plan_json(scenario: str) -> str:
    inventory = load_packaged_connector_inventory()
    template = inventory["templates"]["alphaengine"]
    inventory_fixture = inventory["fixtures"]["alphaengine"]
    fixture = load_recorded_alphaengine_fixture()
    selected = recorded_alphaengine_scenario(fixture, scenario)
    operation = next(
        item for item in template["operations"]
        if item["operation"] == fixture["operation"]
    )
    base = {
        "schema_version": MCP_MANAGED_PROTOCOL_VERSION,
        "id": f"recorded-mcp-plan:alphaengine:search-library:{scenario}:0.2",
        "created_at": ALPHAENGINE_FIXTURE_CREATED_AT,
        "connector_profile_template_ref": template["id"],
        "connector_profile_template_hash": template["content_hash"],
        "inventory_fixture_ref": inventory_fixture["id"],
        "inventory_fixture_hash": inventory_fixture["content_hash"],
        "recorded_fixture_ref": fixture["id"],
        "recorded_fixture_hash": fixture["content_hash"],
        "source_ref": fixture["source_ref"],
        "transport_target_ref": fixture["transport_target_ref"],
        "transport_target_hash": fixture["transport_target_hash"],
        "operation": fixture["operation"],
        "input_schema_ref": operation["input_schema_ref"],
        "input_schema_hash": operation["input_schema_hash"],
        "output_schema_ref": operation["output_schema_ref"],
        "output_schema_hash": operation["output_schema_hash"],
        "parent_parameters": fixture["parent_parameters"],
        "parent_query_hash": fixture["parent_query_hash"],
        "inventory_case_ref": selected["inventory_case_ref"],
        "inventory_case_hash": selected["inventory_case_hash"],
        "recorded_scenario_ref": selected["scenario_ref"],
        "recorded_scenario_hash": selected["scenario_hash"],
        "recorded_behavior": selected["behavior"],
    }
    return canonical_json({**base, "content_hash": content_hash(base)})


def build_recorded_mcp_shadow_plan(scenario: str) -> dict[str, Any]:
    return json.loads(_recorded_mcp_shadow_plan_json(scenario))


_PLAN_FIELDS = {
    "schema_version",
    "id",
    "created_at",
    "connector_profile_template_ref",
    "connector_profile_template_hash",
    "inventory_fixture_ref",
    "inventory_fixture_hash",
    "recorded_fixture_ref",
    "recorded_fixture_hash",
    "source_ref",
    "transport_target_ref",
    "transport_target_hash",
    "operation",
    "input_schema_ref",
    "input_schema_hash",
    "output_schema_ref",
    "output_schema_hash",
    "parent_parameters",
    "parent_query_hash",
    "inventory_case_ref",
    "inventory_case_hash",
    "recorded_scenario_ref",
    "recorded_scenario_hash",
    "recorded_behavior",
    "content_hash",
}


def validate_recorded_mcp_shadow_plan(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    wire = _closed(value, _PLAN_FIELDS, "RecordedMcpShadowPlan")
    if wire["schema_version"] != MCP_MANAGED_PROTOCOL_VERSION:
        raise RunnerValidationError("unsupported RecordedMcpShadowPlan schema_version")
    for name in (
        "id",
        "connector_profile_template_ref",
        "inventory_fixture_ref",
        "recorded_fixture_ref",
        "source_ref",
        "transport_target_ref",
        "input_schema_ref",
        "output_schema_ref",
        "inventory_case_ref",
        "recorded_scenario_ref",
    ):
        wire[name] = _ref(wire[name], name)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    wire["operation"] = _text(wire["operation"], "operation")
    for name in (
        "connector_profile_template_hash",
        "inventory_fixture_hash",
        "recorded_fixture_hash",
        "transport_target_hash",
        "input_schema_hash",
        "output_schema_hash",
        "parent_query_hash",
        "inventory_case_hash",
        "recorded_scenario_hash",
    ):
        wire[name] = _hash(wire[name], name)
    if wire["recorded_behavior"] not in _BEHAVIORS:
        raise RunnerValidationError("recorded MCP behavior is invalid")
    if not isinstance(wire["parent_parameters"], Mapping):
        raise RunnerValidationError("parent_parameters must be an object")
    _validate_hash(wire, "RecordedMcpShadowPlan")
    match = re.fullmatch(
        r"recorded-mcp-plan:alphaengine:search-library:([a-z_]+):0\.2",
        wire["id"],
    )
    if match is None:
        raise RunnerValidationError("recorded MCP plan id is not frozen")
    try:
        expected = build_recorded_mcp_shadow_plan(match.group(1))
    except (KeyError, StopIteration, ValueError) as exc:
        raise RunnerValidationError("recorded MCP scenario is not frozen") from exc
    if wire != expected:
        raise RunnerConflict(
            "recorded MCP plan differs from inventory and fixture authority"
        )
    return wire


_MCP_REQUEST_FIELDS = {
    "protocol_version",
    "runner_request_ref",
    "runner_request_hash",
    "connector_invocation_ref",
    "connector_invocation_hash",
    "profile_ref",
    "profile_hash",
    "call_spec_ref",
    "call_spec_hash",
    "capability_lease_ref",
    "capability_lease_hash",
    "principal_ref",
    "reservation_ref",
    "reservation_hash",
    "physical_attempt_number",
    "source_identity",
    "source_hash",
    "adapter_ref",
    "adapter_hash",
    "resolver_ref",
    "resolver_manifest_hash",
    "transport_target_ref",
    "transport_target_hash",
    "transport_plan_ref",
    "transport_plan_hash",
    "recorded_fixture_ref",
    "recorded_fixture_hash",
    "recorded_scenario_ref",
    "recorded_scenario_hash",
    "recorded_behavior",
    "credential_grant_ref",
    "credential_grant_hash",
    "credential_use_ref",
    "credential_use_hash",
    "operation",
    "parameters",
    "query_hash",
    "input_schema_ref",
    "input_schema_hash",
    "output_schema_ref",
    "output_schema_hash",
    "deadline_at",
    "max_response_bytes",
    "max_records",
    "raw_sink_ref",
    "content_hash",
}


def validate_mcp_managed_adapter_request(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    wire = _closed(value, _MCP_REQUEST_FIELDS, "McpManagedAdapterRequest")
    if wire["protocol_version"] != MCP_MANAGED_PROTOCOL_VERSION:
        raise RunnerValidationError("unsupported McpManagedAdapterRequest protocol_version")
    for name in (
        "runner_request_ref",
        "connector_invocation_ref",
        "profile_ref",
        "call_spec_ref",
        "capability_lease_ref",
        "principal_ref",
        "reservation_ref",
        "adapter_ref",
        "resolver_ref",
        "transport_target_ref",
        "transport_plan_ref",
        "recorded_fixture_ref",
        "recorded_scenario_ref",
        "credential_grant_ref",
        "credential_use_ref",
        "input_schema_ref",
        "output_schema_ref",
    ):
        wire[name] = _ref(wire[name], name)
    wire["operation"] = _text(wire["operation"], "operation")
    wire["deadline_at"] = _timestamp(wire["deadline_at"], "deadline_at")
    wire["raw_sink_ref"] = _text(wire["raw_sink_ref"], "raw_sink_ref")
    if _RAW_SINK_RE.fullmatch(wire["raw_sink_ref"]) is None:
        raise RunnerValidationError("raw_sink_ref must be Runner-derived")
    for name in (
        "runner_request_hash",
        "connector_invocation_hash",
        "profile_hash",
        "call_spec_hash",
        "capability_lease_hash",
        "reservation_hash",
        "source_hash",
        "adapter_hash",
        "resolver_manifest_hash",
        "transport_target_hash",
        "transport_plan_hash",
        "recorded_fixture_hash",
        "recorded_scenario_hash",
        "credential_grant_hash",
        "credential_use_hash",
        "query_hash",
        "input_schema_hash",
        "output_schema_hash",
    ):
        wire[name] = _hash(wire[name], name)
    wire["physical_attempt_number"] = _integer(
        wire["physical_attempt_number"], "physical_attempt_number", minimum=1
    )
    wire["max_response_bytes"] = _integer(
        wire["max_response_bytes"], "max_response_bytes", minimum=1
    )
    wire["max_records"] = _integer(wire["max_records"], "max_records", minimum=1)
    identity = _closed(
        wire["source_identity"],
        {"source_ref", "source_type", "source_version"},
        "source_identity",
    )
    identity["source_ref"] = _ref(
        identity["source_ref"], "source_identity.source_ref"
    )
    identity["source_type"] = _text(
        identity["source_type"], "source_identity.source_type"
    )
    identity["source_version"] = _text(
        identity["source_version"], "source_identity.source_version"
    )
    if identity["source_type"] != "authenticated_library":
        raise RunnerValidationError("MCP source must be an authenticated library")
    wire["source_identity"] = identity
    if wire["source_hash"] != content_hash(identity):
        raise RunnerConflict("MCP source_hash does not bind source_identity")
    if not isinstance(wire["parameters"], Mapping):
        raise RunnerValidationError("MCP parameters must be an object")
    wire["parameters"] = json.loads(canonical_json(wire["parameters"]))
    if wire["query_hash"] != content_hash(
        {"operation": wire["operation"], "parameters": wire["parameters"]}
    ):
        raise RunnerConflict("MCP query_hash does not bind operation/parameters")
    if wire["recorded_behavior"] not in _BEHAVIORS:
        raise RunnerValidationError("recorded MCP behavior is invalid")
    _validate_hash(wire, "McpManagedAdapterRequest")
    return wire


_MCP_OBSERVATION_FIELDS = {
    "protocol_version",
    "request_hash",
    "outcome",
    "provider_request_id",
    "provider_status_code",
    "retry_after_ms",
    "structured_output",
    "source_record_refs",
    "cursor",
    "provider_usage",
    "source_status",
    "completeness",
    "error",
    "content_hash",
}


def validate_mcp_managed_transport_observation(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    wire = _closed(value, _MCP_OBSERVATION_FIELDS, "McpManagedTransportObservation")
    if wire["protocol_version"] != MCP_MANAGED_PROTOCOL_VERSION:
        raise RunnerValidationError(
            "unsupported McpManagedTransportObservation protocol_version"
        )
    wire["request_hash"] = _hash(wire["request_hash"], "request_hash")
    if wire["outcome"] not in {"succeeded", "rate_limited", "failed"}:
        raise RunnerValidationError("MCP observation outcome is invalid")
    wire["provider_request_id"] = _nullable_text(
        wire["provider_request_id"], "provider_request_id"
    )
    status = wire["provider_status_code"]
    if status is not None:
        status = _integer(status, "provider_status_code", minimum=100)
        if status > 599:
            raise RunnerValidationError("provider_status_code must be <= 599")
    wire["provider_status_code"] = status
    retry = wire["retry_after_ms"]
    if retry is not None:
        retry = _integer(retry, "retry_after_ms", minimum=1)
    wire["retry_after_ms"] = retry
    refs = wire["source_record_refs"]
    if not isinstance(refs, list) or any(
        not isinstance(item, str) or not item for item in refs
    ) or len(refs) != len(set(refs)):
        raise RunnerValidationError("source_record_refs must contain unique refs")
    wire["cursor"] = _nullable_text(wire["cursor"], "cursor")
    if wire["provider_usage"] is not None and not isinstance(
        wire["provider_usage"], Mapping
    ):
        raise RunnerValidationError("provider_usage must be an object or null")
    wire["source_status"] = (
        None
        if wire["source_status"] is None
        else _text(wire["source_status"], "source_status")
    )
    wire["completeness"] = (
        None
        if wire["completeness"] is None
        else _text(wire["completeness"], "completeness")
    )
    if wire["source_status"] is not None and wire["source_status"] not in _SOURCE_STATUSES:
        raise RunnerValidationError("source_status is invalid")
    if wire["completeness"] is not None and wire["completeness"] not in _COMPLETENESS:
        raise RunnerValidationError("completeness is invalid")
    if wire["error"] is not None:
        error = _closed(wire["error"], {"code", "message", "retryable"}, "error")
        error["code"] = _text(error["code"], "error.code")
        error["message"] = _text(error["message"], "error.message")
        if type(error["retryable"]) is not bool:
            raise RunnerValidationError("error.retryable must be boolean")
        wire["error"] = error
    if wire["outcome"] == "succeeded":
        if (
            not isinstance(wire["structured_output"], Mapping)
            or wire["error"] is not None
            or wire["source_status"] is None
            or wire["completeness"] is None
            or status is None
            or not 200 <= status < 300
            or retry is not None
        ):
            raise RunnerValidationError("successful MCP observation is inconsistent")
    else:
        if (
            wire["structured_output"] is not None
            or wire["source_record_refs"]
            or wire["cursor"] is not None
            or wire["source_status"] is not None
            or wire["completeness"] is not None
            or wire["error"] is None
        ):
            raise RunnerValidationError("non-success MCP observation carries success data")
        if wire["outcome"] == "rate_limited":
            if status != 429 or retry is None or wire["error"]["code"] != "rate_limited":
                raise RunnerValidationError("rate-limited MCP observation is inconsistent")
        elif retry is not None:
            raise RunnerValidationError("retry_after_ms is only valid for rate limits")
    _validate_hash(wire, "McpManagedTransportObservation")
    return wire


def _validate_schema_instance(value: Any, schema: Mapping[str, Any], *, path: str = "$") -> None:
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected]
    checks = {
        "null": lambda item: item is None,
        "object": lambda item: isinstance(item, Mapping),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
    }
    if expected is not None and not any(checks[item](value) for item in types):
        raise RunnerValidationError(f"{path} does not match the frozen schema type")
    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        required = set(schema.get("required", ()))
        if not required.issubset(value):
            raise RunnerValidationError(f"{path} misses required output fields")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise RunnerValidationError(f"{path} contains undeclared output fields")
        for name, item in value.items():
            if name in properties:
                _validate_schema_instance(item, properties[name], path=f"{path}.{name}")
    if isinstance(value, list):
        if schema.get("uniqueItems") and len(
            {canonical_json(item) for item in value}
        ) != len(value):
            raise RunnerValidationError(f"{path} contains duplicate values")
        for index, item in enumerate(value):
            _validate_schema_instance(item, schema.get("items", {}), path=f"{path}[{index}]")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise RunnerValidationError(f"{path} is too short")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise RunnerValidationError(f"{path} does not match its pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise RunnerValidationError(f"{path} is below minimum")


class McpManagedRunnerAdmissionGate(ConnectorRunnerAdmissionGate):
    """Runner admission for one exact host-owned AlphaEngine reference shadow."""

    def __init__(
        self,
        *,
        credential_authority: CredentialAuthorityPort,
        transport_plans: Sequence[Mapping[str, Any]],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if credential_authority is None:
            raise RunnerValidationError("MCP runner requires credential authority")
        plans = [validate_recorded_mcp_shadow_plan(item) for item in transport_plans]
        by_ref = {item["id"]: item for item in plans}
        if not plans or len(by_ref) != len(plans):
            raise RunnerValidationError("MCP transport plans must be non-empty and unique")
        self.credential_authority = credential_authority
        self.transport_plans = MappingProxyType(by_ref)
        inventory = load_packaged_connector_inventory()
        self._template = inventory["templates"]["alphaengine"]
        self._inventory_fixture = inventory["fixtures"]["alphaengine"]
        self._fixture = load_recorded_alphaengine_fixture()

    def _validate_runner_request_protocol(self, wire: Mapping[str, Any]) -> None:
        if wire["schema_version"] != MCP_MANAGED_PROTOCOL_VERSION:
            raise RunnerConflict(
                "MCP-managed connector runner requires RunnerRequest wire 0.2"
            )

    def _plan_for_request(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        plan = self.transport_plans.get(str(request.get("transport_plan_ref")))
        if plan is None or plan["content_hash"] != request.get("transport_plan_hash"):
            raise RunnerConflict("RunnerRequest does not bind an installed MCP plan")
        return plan

    @staticmethod
    def _credential_authority(admission: ValidatedRunnerAdmission) -> dict[str, Any]:
        return {
            "connector_profile_ref": admission.profile["id"],
            "connector_profile_hash": admission.profile["content_hash"],
            "capability_lease_ref": admission.capability_lease.id,
            "capability_lease_hash": admission.capability_lease.content_hash,
            "adapter_ref": admission.profile["adapter_ref"],
            "adapter_hash": admission.profile["adapter_hash"],
            "principal_ref": admission.request["principal_ref"],
            "credential_slot_refs": admission.profile["credential_slot_refs"],
            "operation": admission.call_spec["operation"],
        }

    def _validate_inventory_binding(
        self,
        admission: ValidatedRunnerAdmission,
        plan: Mapping[str, Any],
    ) -> None:
        operation = next(
            item for item in self._template["operations"]
            if item["operation"] == "search_library"
        )
        profile = admission.profile
        expected_profile = (
            self._template["connector_ref"],
            self._template["source_identity"],
            self._template["transport"]["target_ref"],
            "mcp_managed",
            ["credential-slot:alphaengine"],
            [],
            None,
            ["search_library"],
            {"search_library": operation["input_schema_ref"]},
            {"search_library": operation["input_schema_hash"]},
            {"search_library": operation["output_schema_ref"]},
            {"search_library": operation["output_schema_hash"]},
            {
                "mode": operation["pagination"]["mode"],
                "cursor_field": operation["pagination"]["cursor_field"],
                "max_pages": operation["pagination"]["max_pages"],
            },
            {"search_library": operation["completeness_ceiling"]},
        )
        actual_profile = (
            profile["connector_ref"],
            profile["source_identity"],
            profile["adapter_ref"],
            profile["auth_mode"],
            profile["credential_slot_refs"],
            profile["allowed_hosts"],
            profile["network_policy"],
            profile["allowed_operations"],
            profile["input_schema_refs"],
            profile["input_schema_hashes"],
            profile["output_schema_refs"],
            profile["output_schema_hashes"],
            profile["pagination"],
            profile["completeness"],
        )
        if expected_profile != actual_profile:
            raise RunnerConflict("runtime MCP profile is not the AlphaEngine projection")
        if profile["source_hash"] != content_hash(self._template["source_identity"]):
            raise RunnerConflict("runtime MCP source identity hash drifted")
        if (
            admission.binding["auth_mode"] != "mcp_managed"
            or admission.binding["credential_slot_refs"]
            != ["credential-slot:alphaengine"]
            or admission.binding["adapter_ref"]
            != self._template["transport"]["target_ref"]
        ):
            raise RunnerConflict("resolver binding is not the MCP-managed route")
        selected = recorded_alphaengine_scenario(
            self._fixture,
            plan["id"].removeprefix(
                "recorded-mcp-plan:alphaengine:search-library:"
            ).removesuffix(":0.2"),
        )
        expected_plan = (
            self._template["id"],
            self._template["content_hash"],
            self._inventory_fixture["id"],
            self._inventory_fixture["content_hash"],
            self._fixture["id"],
            self._fixture["content_hash"],
            self._template["transport"]["target_ref"],
            self._template["transport"]["target_hash"],
            admission.call_spec["operation"],
            admission.call_spec["parameters"],
            admission.call_spec["query_hash"],
            selected["scenario_ref"],
            selected["scenario_hash"],
            selected["behavior"],
        )
        actual_plan = (
            plan["connector_profile_template_ref"],
            plan["connector_profile_template_hash"],
            plan["inventory_fixture_ref"],
            plan["inventory_fixture_hash"],
            plan["recorded_fixture_ref"],
            plan["recorded_fixture_hash"],
            plan["transport_target_ref"],
            plan["transport_target_hash"],
            plan["operation"],
            plan["parent_parameters"],
            plan["parent_query_hash"],
            plan["recorded_scenario_ref"],
            plan["recorded_scenario_hash"],
            plan["recorded_behavior"],
        )
        if expected_plan != actual_plan:
            raise RunnerConflict("MCP plan does not bind exact runtime/query/scenario")

    def validate(
        self,
        request: Mapping[str, Any],
        *,
        scheduler_lease_token: str,
    ) -> ValidatedRunnerAdmission:
        admission = super().validate(
            request, scheduler_lease_token=scheduler_lease_token
        )
        plan = self._plan_for_request(admission.request)
        self._validate_inventory_binding(admission, plan)
        self.credential_authority.peek_for_use(
            **self._credential_authority(admission)
        )
        return admission

    def reserve_for_admission(
        self,
        admission: ValidatedRunnerAdmission,
        *,
        scheduler_lease_token: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        revalidated = self.validate(
            admission.request, scheduler_lease_token=scheduler_lease_token
        )
        return self.connectors.reserve_quota_for_runner(
            revalidated.invocation["id"],
            revalidated.binding["rate_policy_ref"],
            revalidated.deadline_at,
            idempotency_key=idempotency_key,
        )

    def build_adapter_request(
        self,
        admission: ValidatedRunnerAdmission,
        *,
        scheduler_lease_token: str,
    ) -> dict[str, Any]:
        initial = self.validate(
            admission.request, scheduler_lease_token=scheduler_lease_token
        )
        reservation = self.connectors.pending_reservation_for_runner(
            initial.invocation["id"]
        )
        reservation = self.connectors.validate_reservation_for_use(
            reservation["id"],
            reservation_hash=reservation["content_hash"],
            connector_invocation_ref=initial.invocation["id"],
            physical_attempt_number=reservation["physical_attempt_number"],
        )
        final = self.validate(
            admission.request, scheduler_lease_token=scheduler_lease_token
        )
        if (
            final.invocation["content_hash"] != initial.invocation["content_hash"]
            or final.binding != initial.binding
            or final.capability_lease.content_hash
            != initial.capability_lease.content_hash
        ):
            raise RunnerConflict("MCP authority changed between admission gates")
        policy_row = self.connectors.connection.execute(
            "SELECT policy_ref FROM connector_rate_policy_versions "
            "WHERE policy_version_id=?",
            (reservation["policy_version_ref"],),
        ).fetchone()
        if (
            policy_row is None
            or policy_row["policy_ref"] != final.binding["rate_policy_ref"]
        ):
            raise RunnerConflict("MCP reservation uses another rate policy")
        plan = self._plan_for_request(final.request)
        use = self.credential_authority.authorize_use(
            runner_request_ref=final.request["id"],
            runner_request_hash=final.request["content_hash"],
            connector_invocation_ref=final.invocation["id"],
            connector_invocation_hash=final.invocation["content_hash"],
            reservation_ref=reservation["id"],
            reservation_hash=reservation["content_hash"],
            physical_attempt_number=reservation["physical_attempt_number"],
            idempotency_key=(
                f"mcp-credential-use:{final.request['id']}:"
                f"{reservation['physical_attempt_number']}"
            ),
            **self._credential_authority(final),
        )
        deadline_at = min(reservation["expires_at"], final.deadline_at)
        raw_sink_ref = "raw-sink:" + content_hash(
            {
                "connector_invocation_ref": final.invocation["id"],
                "reservation_ref": reservation["id"],
                "physical_attempt_number": reservation["physical_attempt_number"],
                "transport_plan_hash": plan["content_hash"],
            }
        )
        wire = {
            "protocol_version": MCP_MANAGED_PROTOCOL_VERSION,
            "runner_request_ref": final.request["id"],
            "runner_request_hash": final.request["content_hash"],
            "connector_invocation_ref": final.invocation["id"],
            "connector_invocation_hash": final.invocation["content_hash"],
            "profile_ref": final.profile["id"],
            "profile_hash": final.profile["content_hash"],
            "call_spec_ref": final.call_spec["id"],
            "call_spec_hash": final.call_spec["content_hash"],
            "capability_lease_ref": final.capability_lease.id,
            "capability_lease_hash": final.capability_lease.content_hash,
            "principal_ref": final.request["principal_ref"],
            "reservation_ref": reservation["id"],
            "reservation_hash": reservation["content_hash"],
            "physical_attempt_number": reservation["physical_attempt_number"],
            "source_identity": dict(final.profile["source_identity"]),
            "source_hash": final.profile["source_hash"],
            "adapter_ref": final.profile["adapter_ref"],
            "adapter_hash": final.profile["adapter_hash"],
            "resolver_ref": self.resolver.manifest["resolver_ref"],
            "resolver_manifest_hash": self.resolver.content_hash,
            "transport_target_ref": plan["transport_target_ref"],
            "transport_target_hash": plan["transport_target_hash"],
            "transport_plan_ref": plan["id"],
            "transport_plan_hash": plan["content_hash"],
            "recorded_fixture_ref": plan["recorded_fixture_ref"],
            "recorded_fixture_hash": plan["recorded_fixture_hash"],
            "recorded_scenario_ref": plan["recorded_scenario_ref"],
            "recorded_scenario_hash": plan["recorded_scenario_hash"],
            "recorded_behavior": plan["recorded_behavior"],
            "credential_grant_ref": use["grant_ref"],
            "credential_grant_hash": use["grant_hash"],
            "credential_use_ref": use["id"],
            "credential_use_hash": use["content_hash"],
            "operation": final.call_spec["operation"],
            "parameters": dict(final.call_spec["parameters"]),
            "query_hash": final.call_spec["query_hash"],
            "input_schema_ref": final.profile["input_schema_refs"][
                final.call_spec["operation"]
            ],
            "input_schema_hash": final.profile["input_schema_hashes"][
                final.call_spec["operation"]
            ],
            "output_schema_ref": final.profile["output_schema_refs"][
                final.call_spec["operation"]
            ],
            "output_schema_hash": final.profile["output_schema_hashes"][
                final.call_spec["operation"]
            ],
            "deadline_at": deadline_at,
            "max_response_bytes": final.profile["max_response_bytes"],
            "max_records": final.profile["max_records"],
            "raw_sink_ref": raw_sink_ref,
        }
        return validate_mcp_managed_adapter_request(
            {**wire, "content_hash": content_hash(wire)}
        )

    def credential_handle_for_use(
        self, adapter_request: Mapping[str, Any]
    ) -> Any:
        request = validate_mcp_managed_adapter_request(adapter_request)
        plan = self.transport_plans.get(request["transport_plan_ref"])
        if plan is None or plan["content_hash"] != request["transport_plan_hash"]:
            raise RunnerConflict("MCP adapter request names an uninstalled plan")
        self._validate_adapter_request_authority(request, plan)
        authorization = self.credential_authority.validate_use(
            request["credential_use_ref"],
            use_hash=request["credential_use_hash"],
            runner_request_ref=request["runner_request_ref"],
            runner_request_hash=request["runner_request_hash"],
            connector_invocation_ref=request["connector_invocation_ref"],
            connector_invocation_hash=request["connector_invocation_hash"],
            reservation_ref=request["reservation_ref"],
            reservation_hash=request["reservation_hash"],
            physical_attempt_number=request["physical_attempt_number"],
            connector_profile_ref=request["profile_ref"],
            connector_profile_hash=request["profile_hash"],
            capability_lease_ref=request["capability_lease_ref"],
            capability_lease_hash=request["capability_lease_hash"],
            adapter_ref=request["adapter_ref"],
            adapter_hash=request["adapter_hash"],
            principal_ref=request["principal_ref"],
            credential_slot_refs=["credential-slot:alphaengine"],
            operation=request["operation"],
        )
        if (
            authorization.grant.id != request["credential_grant_ref"]
            or authorization.grant.content_hash != request["credential_grant_hash"]
        ):
            raise RunnerConflict("MCP grant authority drifted after authorization")
        return authorization.handle

    def _validate_adapter_request_authority(
        self,
        request: Mapping[str, Any],
        plan: Mapping[str, Any],
    ) -> None:
        expected_plan = (
            plan["transport_target_ref"],
            plan["transport_target_hash"],
            plan["recorded_fixture_ref"],
            plan["recorded_fixture_hash"],
            plan["recorded_scenario_ref"],
            plan["recorded_scenario_hash"],
            plan["recorded_behavior"],
            plan["operation"],
            plan["parent_parameters"],
            plan["parent_query_hash"],
            plan["input_schema_ref"],
            plan["input_schema_hash"],
            plan["output_schema_ref"],
            plan["output_schema_hash"],
        )
        actual_plan = (
            request["transport_target_ref"],
            request["transport_target_hash"],
            request["recorded_fixture_ref"],
            request["recorded_fixture_hash"],
            request["recorded_scenario_ref"],
            request["recorded_scenario_hash"],
            request["recorded_behavior"],
            request["operation"],
            request["parameters"],
            request["query_hash"],
            request["input_schema_ref"],
            request["input_schema_hash"],
            request["output_schema_ref"],
            request["output_schema_hash"],
        )
        if expected_plan != actual_plan:
            raise RunnerConflict("MCP adapter request differs from its frozen plan")
        profile = self.connectors.get_profile(request["profile_ref"])
        call_spec = self.connectors.get_call_spec(request["call_spec_ref"])
        invocation = self.connectors.get_invocation(
            request["connector_invocation_ref"]
        )
        reservation = self.connectors.validate_reservation_for_use(
            request["reservation_ref"],
            reservation_hash=request["reservation_hash"],
            connector_invocation_ref=request["connector_invocation_ref"],
            physical_attempt_number=request["physical_attempt_number"],
        )
        del reservation
        expected_authority = (
            profile["content_hash"],
            call_spec["content_hash"],
            invocation["content_hash"],
            invocation["connector_profile_ref"],
            invocation["call_spec_ref"],
            invocation["capability_lease_ref"],
            invocation["capability_lease_hash"],
            profile["source_identity"],
            profile["source_hash"],
            profile["adapter_ref"],
            profile["adapter_hash"],
            self.resolver.manifest["resolver_ref"],
            self.resolver.content_hash,
        )
        actual_authority = (
            request["profile_hash"],
            request["call_spec_hash"],
            request["connector_invocation_hash"],
            request["profile_ref"],
            request["call_spec_ref"],
            request["capability_lease_ref"],
            request["capability_lease_hash"],
            request["source_identity"],
            request["source_hash"],
            request["adapter_ref"],
            request["adapter_hash"],
            request["resolver_ref"],
            request["resolver_manifest_hash"],
        )
        if expected_authority != actual_authority:
            raise RunnerConflict("MCP adapter request authority drifted")

    def validate_observation(
        self,
        value: Mapping[str, Any],
        adapter_request: Mapping[str, Any],
    ) -> dict[str, Any]:
        request = validate_mcp_managed_adapter_request(adapter_request)
        observation = validate_mcp_managed_transport_observation(value)
        if observation["request_hash"] != request["content_hash"]:
            raise RunnerConflict("MCP observation does not bind its AdapterRequest")
        if len(observation["source_record_refs"]) > request["max_records"]:
            raise RunnerValidationError("MCP adapter exceeded max_records")
        plan = self.transport_plans.get(request["transport_plan_ref"])
        if plan is None or plan["content_hash"] != request["transport_plan_hash"]:
            raise RunnerConflict("MCP observation is not attached to an installed plan")
        self._validate_adapter_request_authority(request, plan)
        selected = next(
            item for item in self._fixture["scenarios"]
            if item["scenario_ref"] == request["recorded_scenario_ref"]
        )
        expected_common = (
            selected["scenario_hash"],
            selected["behavior"],
            selected["provider_request_id"],
            selected["provider_status"],
            selected["retry_after_ms"],
            selected["source_record_refs"],
            selected["next_cursor"],
            selected["source_status"],
            selected["completeness"],
        )
        actual_common = (
            request["recorded_scenario_hash"],
            request["recorded_behavior"],
            observation["provider_request_id"],
            observation["provider_status_code"],
            observation["retry_after_ms"],
            observation["source_record_refs"],
            observation["cursor"],
            observation["source_status"],
            observation["completeness"],
        )
        if expected_common != actual_common:
            raise RunnerConflict("MCP observation differs from selected scenario")
        expected_outcome = {
            "return": "succeeded",
            "rate_limited": "rate_limited",
            "permission_denied": "failed",
        }.get(selected["behavior"])
        if expected_outcome is None or observation["outcome"] != expected_outcome:
            raise RunnerConflict("MCP observation violates the selected behavior")
        if expected_outcome == "succeeded":
            if observation["error"] is not None:
                raise RunnerConflict("successful MCP scenario returned an error")
            documents = {
                item["schema_ref"]: item
                for item in self._template["schema_documents"]
            }
            document = documents.get(request["output_schema_ref"])
            if (
                document is None
                or document["schema_hash"] != request["output_schema_hash"]
            ):
                raise RunnerConflict("MCP output schema authority is missing")
            _validate_schema_instance(
                observation["structured_output"], document["document"]
            )
            if observation["structured_output"] != {
                "source_record_refs": observation["source_record_refs"],
                "next_cursor": observation["cursor"],
                "provider_status": observation["provider_status_code"],
            }:
                raise RunnerConflict("MCP normalized output is not exact")
        elif observation["error"]["code"] != selected["error_code"]:
            raise RunnerConflict("MCP failure error_code differs from the scenario")
        return observation


__all__ = [
    "MCP_MANAGED_PROTOCOL_VERSION",
    "McpManagedRunnerAdmissionGate",
    "build_recorded_mcp_shadow_plan",
    "validate_mcp_managed_adapter_request",
    "validate_mcp_managed_transport_observation",
    "validate_recorded_mcp_shadow_plan",
]
