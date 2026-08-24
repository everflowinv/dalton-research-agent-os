"""Live, compiled-plan-bound MCP connector lane.

This module adds a new live wire without changing the recorded AlphaEngine
0.2 replay contract.  It accepts only operator-installed transport plans that
bind an exact ``CompiledConnectorPlan`` step, an operation-scoped runtime
profile, a frozen inventory schema, and one host-owned bridge target.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

from .connector_inventory import load_packaged_connector_inventory
from .connector_runner import (
    ConnectorRunnerAdmissionGate,
    RunnerConflict,
    RunnerValidationError,
    ValidatedRunnerAdmission,
)
from .credential_authority import CredentialAuthorityPort
from .mcp_managed_runner import (
    validate_mcp_managed_transport_observation,
    validate_mcp_schema_instance,
)
from .openclaw_connector_bridge import (
    BridgePermissionDenied,
    BridgeRateLimited,
    HostToolInvocationResult,
)
from .research_context import (
    validate_compiled_connector_plan,
    validate_compiled_connector_step,
)
from .store import canonical_json, content_hash


LIVE_MCP_ADAPTER_PROTOCOL_VERSION = "0.3"
LIVE_MCP_TRANSPORT_PLAN_VERSION = "0.1"
OPENCLAW_ALPHAENGINE_BRIDGE_REF = "openclaw-bridge:alphaengine-mcp:0.1"
_ALPHAENGINE_TOOL_NAMES = {
    "search_library": "search_library",
    "get_document": "get_document",
}
OPENCLAW_ALPHAENGINE_BRIDGE_HASH = content_hash(
    {
        "bridge_ref": OPENCLAW_ALPHAENGINE_BRIDGE_REF,
        "transport_kind": "mcp_managed",
        "source_ref": "source:alphaengine",
        "operation_tools": _ALPHAENGINE_TOOL_NAMES,
        "arbitrary_tool_execution": False,
        "credential_material_serialized": False,
    }
)

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REF_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9+._-]*:[A-Za-z0-9][A-Za-z0-9:._-]*$"
)
_RAW_SINK_RE = re.compile(r"^raw-sink:[0-9a-f]{64}$")


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


def _validate_hash(wire: Mapping[str, Any], name: str) -> None:
    declared = _hash(wire["content_hash"], f"{name}.content_hash")
    expected = content_hash(
        {key: value for key, value in wire.items() if key != "content_hash"}
    )
    if declared != expected:
        raise RunnerConflict(f"{name} content_hash mismatch")


def _alphaengine_template_operation(operation: str) -> tuple[dict[str, Any], dict[str, Any]]:
    inventory = load_packaged_connector_inventory()
    template = inventory["templates"]["alphaengine"]
    matches = [
        item for item in template["operations"]
        if item["operation"] == operation
    ]
    if len(matches) != 1:
        raise RunnerValidationError("AlphaEngine operation is not frozen")
    return template, matches[0]


_LIVE_PLAN_FIELDS = {
    "schema_version", "id", "created_at",
    "connector_profile_template_ref", "connector_profile_template_hash",
    "source_ref", "source_hash", "transport_target_ref",
    "transport_target_hash", "bridge_ref", "bridge_hash",
    "compiled_connector_plan_ref", "compiled_connector_plan_hash",
    "compiled_step_ref", "compiled_step_hash", "operation", "tool_name",
    "parameters", "query_hash", "input_schema_ref", "input_schema_hash",
    "output_schema_ref", "output_schema_hash", "content_hash",
}


def build_live_mcp_transport_plan(
    compiled_plan: Mapping[str, Any],
    compiled_step: Mapping[str, Any],
) -> dict[str, Any]:
    plan = validate_compiled_connector_plan(compiled_plan)
    step = validate_compiled_connector_step(compiled_step)
    installed = [item for item in plan["steps"] if item["id"] == step["id"]]
    if len(installed) != 1 or installed[0] != step:
        raise RunnerConflict("compiled MCP step is not installed in its plan")
    template, operation = _alphaengine_template_operation(step["operation"])
    identity = {
        "compiled_connector_plan_ref": plan["id"],
        "compiled_connector_plan_hash": plan["content_hash"],
        "compiled_step_ref": step["id"],
        "compiled_step_hash": step["content_hash"],
        "bridge_hash": OPENCLAW_ALPHAENGINE_BRIDGE_HASH,
    }
    base = {
        "schema_version": LIVE_MCP_TRANSPORT_PLAN_VERSION,
        "id": (
            f"live-mcp-plan:alphaengine:{step['operation']}:"
            + content_hash(identity)
        ),
        "created_at": plan["created_at"],
        "connector_profile_template_ref": template["id"],
        "connector_profile_template_hash": template["content_hash"],
        "source_ref": template["source_identity"]["source_ref"],
        "source_hash": content_hash(template["source_identity"]),
        "transport_target_ref": template["transport"]["target_ref"],
        "transport_target_hash": template["transport"]["target_hash"],
        "bridge_ref": OPENCLAW_ALPHAENGINE_BRIDGE_REF,
        "bridge_hash": OPENCLAW_ALPHAENGINE_BRIDGE_HASH,
        **identity,
        "operation": step["operation"],
        "tool_name": _ALPHAENGINE_TOOL_NAMES[step["operation"]],
        "parameters": step["parameters"],
        "query_hash": step["query_hash"],
        "input_schema_ref": operation["input_schema_ref"],
        "input_schema_hash": operation["input_schema_hash"],
        "output_schema_ref": operation["output_schema_ref"],
        "output_schema_hash": operation["output_schema_hash"],
    }
    return validate_live_mcp_transport_plan(
        {**base, "content_hash": content_hash(base)}
    )


def validate_live_mcp_transport_plan(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    wire = _closed(value, _LIVE_PLAN_FIELDS, "LiveMcpTransportPlan")
    if wire["schema_version"] != LIVE_MCP_TRANSPORT_PLAN_VERSION:
        raise RunnerValidationError("unsupported LiveMcpTransportPlan schema_version")
    for name in (
        "id", "connector_profile_template_ref", "source_ref",
        "transport_target_ref", "bridge_ref", "compiled_connector_plan_ref",
        "compiled_step_ref", "input_schema_ref", "output_schema_ref",
    ):
        wire[name] = _ref(wire[name], name)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    wire["operation"] = _text(wire["operation"], "operation")
    wire["tool_name"] = _text(wire["tool_name"], "tool_name")
    for name in (
        "connector_profile_template_hash", "source_hash",
        "transport_target_hash", "bridge_hash",
        "compiled_connector_plan_hash", "compiled_step_hash", "query_hash",
        "input_schema_hash", "output_schema_hash",
    ):
        wire[name] = _hash(wire[name], name)
    if not isinstance(wire["parameters"], Mapping):
        raise RunnerValidationError("LiveMcpTransportPlan.parameters must be an object")
    wire["parameters"] = json.loads(canonical_json(wire["parameters"]))
    if wire["query_hash"] != content_hash(
        {"operation": wire["operation"], "parameters": wire["parameters"]}
    ):
        raise RunnerConflict("LiveMcpTransportPlan query_hash mismatch")
    if (
        wire["bridge_ref"] != OPENCLAW_ALPHAENGINE_BRIDGE_REF
        or wire["bridge_hash"] != OPENCLAW_ALPHAENGINE_BRIDGE_HASH
        or wire["tool_name"] != _ALPHAENGINE_TOOL_NAMES.get(wire["operation"])
    ):
        raise RunnerConflict("LiveMcpTransportPlan bridge authority drifted")
    template, operation = _alphaengine_template_operation(wire["operation"])
    expected_inventory = (
        template["id"], template["content_hash"],
        template["source_identity"]["source_ref"],
        content_hash(template["source_identity"]),
        template["transport"]["target_ref"],
        template["transport"]["target_hash"],
        operation["input_schema_ref"], operation["input_schema_hash"],
        operation["output_schema_ref"], operation["output_schema_hash"],
    )
    actual_inventory = (
        wire["connector_profile_template_ref"],
        wire["connector_profile_template_hash"], wire["source_ref"],
        wire["source_hash"], wire["transport_target_ref"],
        wire["transport_target_hash"], wire["input_schema_ref"],
        wire["input_schema_hash"], wire["output_schema_ref"],
        wire["output_schema_hash"],
    )
    if expected_inventory != actual_inventory:
        raise RunnerConflict("LiveMcpTransportPlan inventory authority drifted")
    schema_documents = {
        item["schema_ref"]: item for item in template["schema_documents"]
    }
    input_document = schema_documents.get(wire["input_schema_ref"])
    if (
        input_document is None
        or input_document["schema_hash"] != wire["input_schema_hash"]
    ):
        raise RunnerConflict("LiveMcpTransportPlan input schema authority is missing")
    validate_mcp_schema_instance(wire["parameters"], input_document["document"])
    identity = {
        "compiled_connector_plan_ref": wire["compiled_connector_plan_ref"],
        "compiled_connector_plan_hash": wire["compiled_connector_plan_hash"],
        "compiled_step_ref": wire["compiled_step_ref"],
        "compiled_step_hash": wire["compiled_step_hash"],
        "bridge_hash": wire["bridge_hash"],
    }
    expected_id = (
        f"live-mcp-plan:alphaengine:{wire['operation']}:"
        + content_hash(identity)
    )
    if wire["id"] != expected_id:
        raise RunnerConflict("LiveMcpTransportPlan id is not deterministic")
    _validate_hash(wire, "LiveMcpTransportPlan")
    return wire


_LIVE_REQUEST_FIELDS = {
    "protocol_version", "runner_request_ref", "runner_request_hash",
    "connector_invocation_ref", "connector_invocation_hash", "profile_ref",
    "profile_hash", "call_spec_ref", "call_spec_hash",
    "capability_lease_ref", "capability_lease_hash", "principal_ref",
    "reservation_ref", "reservation_hash", "physical_attempt_number",
    "source_identity", "source_hash", "adapter_ref", "adapter_hash",
    "resolver_ref", "resolver_manifest_hash", "transport_target_ref",
    "transport_target_hash", "transport_plan_ref", "transport_plan_hash",
    "bridge_ref", "bridge_hash", "compiled_connector_plan_ref",
    "compiled_connector_plan_hash", "compiled_step_ref", "compiled_step_hash",
    "credential_grant_ref", "credential_grant_hash", "credential_use_ref",
    "credential_use_hash", "operation", "tool_name", "parameters",
    "query_hash", "input_schema_ref", "input_schema_hash",
    "output_schema_ref", "output_schema_hash", "deadline_at",
    "max_response_bytes", "max_records", "raw_sink_ref", "content_hash",
}


def validate_live_mcp_adapter_request(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    wire = _closed(value, _LIVE_REQUEST_FIELDS, "LiveMcpAdapterRequest")
    if wire["protocol_version"] != LIVE_MCP_ADAPTER_PROTOCOL_VERSION:
        raise RunnerValidationError("unsupported LiveMcpAdapterRequest protocol_version")
    for name in (
        "runner_request_ref", "connector_invocation_ref", "profile_ref",
        "call_spec_ref", "capability_lease_ref", "principal_ref",
        "reservation_ref", "adapter_ref", "resolver_ref",
        "transport_target_ref", "transport_plan_ref", "bridge_ref",
        "compiled_connector_plan_ref", "compiled_step_ref",
        "credential_grant_ref", "credential_use_ref", "input_schema_ref",
        "output_schema_ref",
    ):
        wire[name] = _ref(wire[name], name)
    for name in (
        "runner_request_hash", "connector_invocation_hash", "profile_hash",
        "call_spec_hash", "capability_lease_hash", "reservation_hash",
        "source_hash", "adapter_hash", "resolver_manifest_hash",
        "transport_target_hash", "transport_plan_hash", "bridge_hash",
        "compiled_connector_plan_hash", "compiled_step_hash",
        "credential_grant_hash", "credential_use_hash", "query_hash",
        "input_schema_hash", "output_schema_hash",
    ):
        wire[name] = _hash(wire[name], name)
    wire["operation"] = _text(wire["operation"], "operation")
    wire["tool_name"] = _text(wire["tool_name"], "tool_name")
    wire["deadline_at"] = _timestamp(wire["deadline_at"], "deadline_at")
    wire["physical_attempt_number"] = _integer(
        wire["physical_attempt_number"], "physical_attempt_number", minimum=1
    )
    wire["max_response_bytes"] = _integer(
        wire["max_response_bytes"], "max_response_bytes", minimum=1
    )
    wire["max_records"] = _integer(
        wire["max_records"], "max_records", minimum=1
    )
    wire["raw_sink_ref"] = _text(wire["raw_sink_ref"], "raw_sink_ref")
    if _RAW_SINK_RE.fullmatch(wire["raw_sink_ref"]) is None:
        raise RunnerValidationError("raw_sink_ref must be Runner-derived")
    identity = _closed(
        wire["source_identity"],
        {"source_ref", "source_type", "source_version"},
        "source_identity",
    )
    identity["source_ref"] = _ref(identity["source_ref"], "source_identity.source_ref")
    identity["source_type"] = _text(identity["source_type"], "source_identity.source_type")
    identity["source_version"] = _text(identity["source_version"], "source_identity.source_version")
    if identity["source_type"] != "authenticated_library":
        raise RunnerValidationError("live MCP source must be an authenticated library")
    wire["source_identity"] = identity
    if wire["source_hash"] != content_hash(identity):
        raise RunnerConflict("live MCP source_hash does not bind source_identity")
    if not isinstance(wire["parameters"], Mapping):
        raise RunnerValidationError("live MCP parameters must be an object")
    wire["parameters"] = json.loads(canonical_json(wire["parameters"]))
    if wire["query_hash"] != content_hash(
        {"operation": wire["operation"], "parameters": wire["parameters"]}
    ):
        raise RunnerConflict("live MCP query_hash mismatch")
    if (
        wire["bridge_ref"] != OPENCLAW_ALPHAENGINE_BRIDGE_REF
        or wire["bridge_hash"] != OPENCLAW_ALPHAENGINE_BRIDGE_HASH
        or wire["tool_name"] != _ALPHAENGINE_TOOL_NAMES.get(wire["operation"])
    ):
        raise RunnerConflict("live MCP adapter request bridge drifted")
    _validate_hash(wire, "LiveMcpAdapterRequest")
    return wire


class LiveMcpRunnerAdmissionGate(ConnectorRunnerAdmissionGate):
    """Use-time gate for exact compiled AlphaEngine live calls."""

    def __init__(
        self,
        *,
        credential_authority: CredentialAuthorityPort,
        transport_plans: Sequence[Mapping[str, Any]],
        compiled_plans: Sequence[Mapping[str, Any]],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if credential_authority is None:
            raise RunnerValidationError("live MCP runner requires credential authority")
        plans = [validate_live_mcp_transport_plan(item) for item in transport_plans]
        compiled = [validate_compiled_connector_plan(item) for item in compiled_plans]
        self.transport_plans = MappingProxyType({item["id"]: item for item in plans})
        self.compiled_plans = MappingProxyType({item["id"]: item for item in compiled})
        if (
            not plans or not compiled
            or len(self.transport_plans) != len(plans)
            or len(self.compiled_plans) != len(compiled)
        ):
            raise RunnerValidationError("live MCP plans must be non-empty and unique")
        for plan in plans:
            installed = self.compiled_plans.get(plan["compiled_connector_plan_ref"])
            if (
                installed is None
                or installed["content_hash"] != plan["compiled_connector_plan_hash"]
            ):
                raise RunnerConflict("live MCP transport plan lacks compiled authority")
        self.credential_authority = credential_authority
        self._template = load_packaged_connector_inventory()["templates"]["alphaengine"]

    def _validate_runner_request_protocol(self, wire: Mapping[str, Any]) -> None:
        if wire["schema_version"] != "0.2":
            raise RunnerConflict("live MCP runner requires RunnerRequest wire 0.2")
        required = {
            "compiled_connector_plan_ref", "compiled_connector_plan_hash",
            "compiled_step_ref", "compiled_step_hash",
        }
        if not required.issubset(wire):
            raise RunnerConflict("live MCP RunnerRequest requires compiled-plan authority")

    def _plan_for_request(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        plan = self.transport_plans.get(str(request.get("transport_plan_ref")))
        if plan is None or plan["content_hash"] != request.get("transport_plan_hash"):
            raise RunnerConflict("RunnerRequest does not bind an installed live MCP plan")
        return plan

    def _compiled_for_request(
        self, request: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        plan = self.compiled_plans.get(str(request.get("compiled_connector_plan_ref")))
        if plan is None or plan["content_hash"] != request.get("compiled_connector_plan_hash"):
            raise RunnerConflict("RunnerRequest does not bind installed compiled authority")
        matches = [
            step for step in plan["steps"]
            if step["id"] == request.get("compiled_step_ref")
            and step["content_hash"] == request.get("compiled_step_hash")
        ]
        if len(matches) != 1:
            raise RunnerConflict("RunnerRequest compiled step is not exact")
        return plan, matches[0]

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

    def _validate_live_binding(
        self,
        admission: ValidatedRunnerAdmission,
        transport: Mapping[str, Any],
        compiled: Mapping[str, Any],
        step: Mapping[str, Any],
    ) -> None:
        operation = next(
            item for item in self._template["operations"]
            if item["operation"] == transport["operation"]
        )
        profile = admission.profile
        expected_profile = (
            self._template["connector_ref"], self._template["source_identity"],
            self._template["transport"]["target_ref"], "mcp_managed",
            ["credential-slot:alphaengine"], [], None,
            [operation["operation"]],
            {operation["operation"]: operation["input_schema_ref"]},
            {operation["operation"]: operation["input_schema_hash"]},
            {operation["operation"]: operation["output_schema_ref"]},
            {operation["operation"]: operation["output_schema_hash"]},
            {
                "mode": operation["pagination"]["mode"],
                "cursor_field": operation["pagination"]["cursor_field"],
                "max_pages": operation["pagination"]["max_pages"],
            },
            {operation["operation"]: operation["completeness_ceiling"]},
        )
        actual_profile = (
            profile["connector_ref"], profile["source_identity"],
            profile["adapter_ref"], profile["auth_mode"],
            profile["credential_slot_refs"], profile["allowed_hosts"],
            profile["network_policy"], profile["allowed_operations"],
            profile["input_schema_refs"], profile["input_schema_hashes"],
            profile["output_schema_refs"], profile["output_schema_hashes"],
            profile["pagination"], profile["completeness"],
        )
        if expected_profile != actual_profile:
            raise RunnerConflict("runtime profile is not an operation-scoped AlphaEngine projection")
        binding = admission.binding
        if (
            binding["auth_mode"] != "mcp_managed"
            or binding["credential_slot_refs"] != ["credential-slot:alphaengine"]
            or binding["adapter_ref"] != self._template["transport"]["target_ref"]
            or binding["operation"] != operation["operation"]
            or binding["input_schema_hash"] != operation["input_schema_hash"]
            or binding["output_schema_hash"] != operation["output_schema_hash"]
        ):
            raise RunnerConflict("resolver binding is not the exact AlphaEngine live route")
        request = admission.request
        expected_compiled = (
            compiled["id"], compiled["content_hash"], step["id"],
            step["content_hash"], compiled["task_ref"], compiled["task_hash"],
            step["connector_profile_ref"], step["connector_profile_hash"],
            step["source_ref"], step["source_hash"], step["operation"],
            step["parameters"], step["query_hash"], step["input_schema_ref"],
            step["input_schema_hash"], step["output_schema_ref"],
            step["output_schema_hash"], step["completeness_required"],
        )
        actual_compiled = (
            request["compiled_connector_plan_ref"],
            request["compiled_connector_plan_hash"], request["compiled_step_ref"],
            request["compiled_step_hash"], admission.work_order.id,
            content_hash(admission.work_order.to_dict()), profile["id"],
            profile["content_hash"], profile["source_identity"]["source_ref"],
            profile["source_hash"], admission.call_spec["operation"],
            admission.call_spec["parameters"], admission.call_spec["query_hash"],
            profile["input_schema_refs"][operation["operation"]],
            profile["input_schema_hashes"][operation["operation"]],
            profile["output_schema_refs"][operation["operation"]],
            profile["output_schema_hashes"][operation["operation"]],
            profile["completeness"][operation["operation"]],
        )
        if expected_compiled != actual_compiled:
            raise RunnerConflict("compiled plan does not bind exact live runtime authority")
        expected_transport = (
            compiled["id"], compiled["content_hash"], step["id"],
            step["content_hash"], step["operation"], step["parameters"],
            step["query_hash"], operation["input_schema_ref"],
            operation["input_schema_hash"], operation["output_schema_ref"],
            operation["output_schema_hash"],
        )
        actual_transport = (
            transport["compiled_connector_plan_ref"],
            transport["compiled_connector_plan_hash"],
            transport["compiled_step_ref"], transport["compiled_step_hash"],
            transport["operation"], transport["parameters"],
            transport["query_hash"], transport["input_schema_ref"],
            transport["input_schema_hash"], transport["output_schema_ref"],
            transport["output_schema_hash"],
        )
        if expected_transport != actual_transport:
            raise RunnerConflict("live transport plan differs from compiled authority")

    def validate(
        self,
        request: Mapping[str, Any],
        *,
        scheduler_lease_token: str,
    ) -> ValidatedRunnerAdmission:
        admission = super().validate(
            request, scheduler_lease_token=scheduler_lease_token
        )
        transport = self._plan_for_request(admission.request)
        compiled, step = self._compiled_for_request(admission.request)
        self._validate_live_binding(admission, transport, compiled, step)
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
            or final.capability_lease.content_hash != initial.capability_lease.content_hash
        ):
            raise RunnerConflict("live MCP authority changed between admission gates")
        policy_row = self.connectors.connection.execute(
            "SELECT policy_ref FROM connector_rate_policy_versions "
            "WHERE policy_version_id=?",
            (reservation["policy_version_ref"],),
        ).fetchone()
        if policy_row is None or policy_row["policy_ref"] != final.binding["rate_policy_ref"]:
            raise RunnerConflict("live MCP reservation uses another rate policy")
        transport = self._plan_for_request(final.request)
        use = self.credential_authority.authorize_use(
            runner_request_ref=final.request["id"],
            runner_request_hash=final.request["content_hash"],
            connector_invocation_ref=final.invocation["id"],
            connector_invocation_hash=final.invocation["content_hash"],
            reservation_ref=reservation["id"],
            reservation_hash=reservation["content_hash"],
            physical_attempt_number=reservation["physical_attempt_number"],
            idempotency_key=(
                f"live-mcp-credential-use:{final.request['id']}:"
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
                "transport_plan_hash": transport["content_hash"],
                "bridge_hash": transport["bridge_hash"],
            }
        )
        operation = final.call_spec["operation"]
        wire = {
            "protocol_version": LIVE_MCP_ADAPTER_PROTOCOL_VERSION,
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
            "transport_target_ref": transport["transport_target_ref"],
            "transport_target_hash": transport["transport_target_hash"],
            "transport_plan_ref": transport["id"],
            "transport_plan_hash": transport["content_hash"],
            "bridge_ref": transport["bridge_ref"],
            "bridge_hash": transport["bridge_hash"],
            "compiled_connector_plan_ref": transport["compiled_connector_plan_ref"],
            "compiled_connector_plan_hash": transport["compiled_connector_plan_hash"],
            "compiled_step_ref": transport["compiled_step_ref"],
            "compiled_step_hash": transport["compiled_step_hash"],
            "credential_grant_ref": use["grant_ref"],
            "credential_grant_hash": use["grant_hash"],
            "credential_use_ref": use["id"],
            "credential_use_hash": use["content_hash"],
            "operation": operation,
            "tool_name": transport["tool_name"],
            "parameters": dict(final.call_spec["parameters"]),
            "query_hash": final.call_spec["query_hash"],
            "input_schema_ref": final.profile["input_schema_refs"][operation],
            "input_schema_hash": final.profile["input_schema_hashes"][operation],
            "output_schema_ref": final.profile["output_schema_refs"][operation],
            "output_schema_hash": final.profile["output_schema_hashes"][operation],
            "deadline_at": deadline_at,
            "max_response_bytes": final.profile["max_response_bytes"],
            "max_records": final.profile["max_records"],
            "raw_sink_ref": raw_sink_ref,
        }
        return validate_live_mcp_adapter_request(
            {**wire, "content_hash": content_hash(wire)}
        )

    def _validate_adapter_request_authority(
        self,
        request: Mapping[str, Any],
        transport: Mapping[str, Any],
    ) -> None:
        expected_transport = (
            transport["transport_target_ref"], transport["transport_target_hash"],
            transport["bridge_ref"], transport["bridge_hash"],
            transport["compiled_connector_plan_ref"],
            transport["compiled_connector_plan_hash"],
            transport["compiled_step_ref"], transport["compiled_step_hash"],
            transport["operation"], transport["tool_name"],
            transport["parameters"], transport["query_hash"],
            transport["input_schema_ref"], transport["input_schema_hash"],
            transport["output_schema_ref"], transport["output_schema_hash"],
        )
        actual_transport = (
            request["transport_target_ref"], request["transport_target_hash"],
            request["bridge_ref"], request["bridge_hash"],
            request["compiled_connector_plan_ref"],
            request["compiled_connector_plan_hash"], request["compiled_step_ref"],
            request["compiled_step_hash"], request["operation"],
            request["tool_name"], request["parameters"], request["query_hash"],
            request["input_schema_ref"], request["input_schema_hash"],
            request["output_schema_ref"], request["output_schema_hash"],
        )
        if expected_transport != actual_transport:
            raise RunnerConflict("live MCP adapter request differs from frozen plan")
        profile = self.connectors.get_profile(request["profile_ref"])
        call_spec = self.connectors.get_call_spec(request["call_spec_ref"])
        invocation = self.connectors.get_invocation(request["connector_invocation_ref"])
        self.connectors.validate_reservation_for_use(
            request["reservation_ref"],
            reservation_hash=request["reservation_hash"],
            connector_invocation_ref=request["connector_invocation_ref"],
            physical_attempt_number=request["physical_attempt_number"],
        )
        expected_authority = (
            profile["content_hash"], call_spec["content_hash"],
            invocation["content_hash"], invocation["connector_profile_ref"],
            invocation["call_spec_ref"], invocation["capability_lease_ref"],
            invocation["capability_lease_hash"], profile["source_identity"],
            profile["source_hash"], profile["adapter_ref"],
            profile["adapter_hash"], self.resolver.manifest["resolver_ref"],
            self.resolver.content_hash,
        )
        actual_authority = (
            request["profile_hash"], request["call_spec_hash"],
            request["connector_invocation_hash"], request["profile_ref"],
            request["call_spec_ref"], request["capability_lease_ref"],
            request["capability_lease_hash"], request["source_identity"],
            request["source_hash"], request["adapter_ref"],
            request["adapter_hash"], request["resolver_ref"],
            request["resolver_manifest_hash"],
        )
        if expected_authority != actual_authority:
            raise RunnerConflict("live MCP adapter request authority drifted")
        compiled, step = self._compiled_for_request(request)
        if (
            compiled["id"] != transport["compiled_connector_plan_ref"]
            or step["id"] != transport["compiled_step_ref"]
        ):
            raise RunnerConflict("live MCP compiled authority drifted")

    def credential_handle_for_use(
        self, adapter_request: Mapping[str, Any]
    ) -> Any:
        request = validate_live_mcp_adapter_request(adapter_request)
        transport = self.transport_plans.get(request["transport_plan_ref"])
        if transport is None or transport["content_hash"] != request["transport_plan_hash"]:
            raise RunnerConflict("live MCP adapter request names an uninstalled plan")
        self._validate_adapter_request_authority(request, transport)
        profile = self.connectors.get_profile(request["profile_ref"])
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
            credential_slot_refs=profile["credential_slot_refs"],
            operation=request["operation"],
        )
        if (
            authorization.grant.id != request["credential_grant_ref"]
            or authorization.grant.content_hash != request["credential_grant_hash"]
        ):
            raise RunnerConflict("live MCP grant authority drifted")
        return authorization.handle

    def validate_observation(
        self,
        value: Mapping[str, Any],
        adapter_request: Mapping[str, Any],
    ) -> dict[str, Any]:
        request = validate_live_mcp_adapter_request(adapter_request)
        observation = validate_mcp_managed_transport_observation(value)
        if observation["request_hash"] != request["content_hash"]:
            raise RunnerConflict("live MCP observation does not bind AdapterRequest")
        if len(observation["source_record_refs"]) > request["max_records"]:
            raise RunnerValidationError("live MCP adapter exceeded max_records")
        transport = self.transport_plans.get(request["transport_plan_ref"])
        if transport is None or transport["content_hash"] != request["transport_plan_hash"]:
            raise RunnerConflict("live MCP observation lacks transport authority")
        self._validate_adapter_request_authority(request, transport)
        if observation["outcome"] == "succeeded":
            documents = {
                item["schema_ref"]: item for item in self._template["schema_documents"]
            }
            document = documents.get(request["output_schema_ref"])
            if document is None or document["schema_hash"] != request["output_schema_hash"]:
                raise RunnerConflict("live MCP output schema authority is missing")
            validate_mcp_schema_instance(
                observation["structured_output"], document["document"]
            )
            if observation["structured_output"] != {
                "source_record_refs": observation["source_record_refs"],
                "next_cursor": observation["cursor"],
                "provider_status": observation["provider_status_code"],
            }:
                raise RunnerConflict("live MCP normalized output is not exact")
            refs = observation["source_record_refs"]
            cursor = observation["cursor"]
            if request["operation"] == "search_library":
                expected = (
                    "empty" if not refs else "partial" if cursor is not None else "complete",
                    "ranked",
                )
            else:
                expected = (
                    "empty" if not refs else "partial" if cursor is not None else "complete",
                    "partial" if cursor is not None else "enumerated",
                )
            if (observation["source_status"], observation["completeness"]) != expected:
                raise RunnerConflict("live MCP source semantics are inconsistent")
        return observation


def _tool_text_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    content = result.get("content")
    if not isinstance(content, list):
        raise RunnerValidationError("AlphaEngine MCP result lacks content blocks")
    text_blocks = [
        item.get("text") for item in content
        if isinstance(item, Mapping) and item.get("type") == "text"
    ]
    if len(text_blocks) != 1 or not isinstance(text_blocks[0], str):
        raise RunnerValidationError("AlphaEngine MCP result requires one text block")
    try:
        payload = json.loads(text_blocks[0])
    except json.JSONDecodeError as exc:
        raise RunnerValidationError("AlphaEngine MCP text is not JSON") from exc
    if not isinstance(payload, Mapping):
        raise RunnerValidationError("AlphaEngine MCP payload must be an object")
    return dict(payload)


def alphaengine_document_page_from_raw_response(
    raw_response: bytes,
    *,
    expected_doc_id: str,
    expected_offset: int,
    max_chars: int,
) -> tuple[str, dict[str, Any]]:
    """Recover and validate one document page from its exact JSON-RPC bytes."""

    if not isinstance(raw_response, bytes):
        raise RunnerValidationError("AlphaEngine raw response must be bytes")
    try:
        rpc = json.loads(raw_response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerValidationError(
            "AlphaEngine raw response is not strict UTF-8 JSON"
        ) from exc
    if (
        not isinstance(rpc, Mapping)
        or rpc.get("jsonrpc") != "2.0"
        or not isinstance(rpc.get("id"), str)
        or not rpc["id"]
        or not isinstance(rpc.get("result"), Mapping)
        or "error" in rpc
    ):
        raise RunnerValidationError(
            "AlphaEngine raw response is not a successful JSON-RPC result"
        )
    page = validate_alphaengine_document_page(
        _tool_text_payload(rpc["result"]),
        expected_doc_id=expected_doc_id,
        expected_offset=expected_offset,
        max_chars=max_chars,
    )
    return rpc["id"], page


def validate_alphaengine_document_page(
    payload: Mapping[str, Any],
    *,
    expected_doc_id: str,
    expected_offset: int,
    max_chars: int,
) -> dict[str, Any]:
    """Validate one exact, contiguous AlphaEngine document page."""

    if not isinstance(payload, Mapping):
        raise RunnerValidationError("AlphaEngine document payload must be an object")
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        raise RunnerValidationError("AlphaEngine document lacks metadata")
    doc_id = metadata.get("doc_id")
    if doc_id != expected_doc_id:
        raise RunnerConflict("AlphaEngine document id differs from request")
    digest = payload.get("content_sha256")
    if not isinstance(digest, str) or _HASH_RE.fullmatch(digest) is None:
        raise RunnerValidationError("AlphaEngine document hash is invalid")
    text = payload.get("text")
    if not isinstance(text, str):
        raise RunnerValidationError("AlphaEngine document text is missing")
    content_chars = payload.get("content_chars")
    offset = payload.get("offset")
    returned_chars = payload.get("returned_chars")
    for value, name in (
        (content_chars, "content_chars"),
        (offset, "offset"),
        (returned_chars, "returned_chars"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RunnerValidationError(
                f"AlphaEngine document {name} must be a non-negative integer"
            )
    if offset != expected_offset:
        raise RunnerConflict("AlphaEngine document offset is not contiguous")
    if returned_chars != len(text) or returned_chars > max_chars:
        raise RunnerValidationError(
            "AlphaEngine returned_chars differs from text or page limit"
        )
    if offset + returned_chars > content_chars:
        raise RunnerValidationError("AlphaEngine document page exceeds content_chars")
    complete = payload.get("complete")
    if type(complete) is not bool:
        raise RunnerValidationError("AlphaEngine document complete flag is invalid")
    next_offset = payload.get("next_offset")
    terminal_by_bounds = (
        next_offset is None and offset + returned_chars == content_chars
    )
    if not complete and terminal_by_bounds:
        # AlphaEngine currently returns complete=false on some exact final
        # pages.  The immutable raw response is retained; normalize only when
        # length, offset, and null continuation independently prove terminal.
        complete = True
    if complete:
        if not terminal_by_bounds:
            raise RunnerValidationError(
                "complete AlphaEngine document page does not end at content_chars"
            )
        cursor = None
        completeness = "enumerated"
    else:
        if (
            isinstance(next_offset, bool)
            or not isinstance(next_offset, int)
            or returned_chars < 1
            or next_offset != offset + returned_chars
            or next_offset >= content_chars
        ):
            raise RunnerValidationError(
                "partial AlphaEngine document next_offset is not contiguous"
            )
        cursor = str(next_offset)
        completeness = "partial"
    return {
        "doc_id": doc_id,
        "content_chars": content_chars,
        "content_sha256": digest,
        "offset": offset,
        "returned_chars": returned_chars,
        "text": text,
        "next_offset": next_offset,
        "complete": complete,
        "cursor": cursor,
        "completeness": completeness,
        "source_record_ref": f"alphaengine-doc:{doc_id}:sha256:{digest}",
    }


_DOCUMENT_CATEGORY_MAP = {
    "research_report": ["foreign_report", "domestic_report", "sell_side_report"],
    "foreign_report": ["foreign_report"],
    "domestic_report": ["domestic_report"],
    "sell_side_report": ["sell_side_report"],
    "sell_side_comment": ["sell_side_comment"],
    "meeting_minutes": ["meeting_minutes"],
    "announcement": ["announcement"],
    "news": ["news"],
}


def alphaengine_tool_arguments(request: Mapping[str, Any]) -> dict[str, Any]:
    wire = validate_live_mcp_adapter_request(request)
    parameters = wire["parameters"]
    if wire["operation"] == "search_library":
        query = parameters["query"]
        filters = parameters["filters"]
        company = filters.get("company")
        if company and company.casefold() not in query.casefold():
            query = f"{company} {query}"
        date_from = filters.get("date_from")
        date_to = filters.get("date_to")
        if (date_from is None) != (date_to is None):
            raise RunnerValidationError("AlphaEngine search requires both date bounds")
        document_type = filters.get("document_type")
        if document_type not in _DOCUMENT_CATEGORY_MAP:
            raise RunnerValidationError("AlphaEngine document_type is not mapped")
        geography = filters.get("geography")
        if geography not in {None, "US", "HK", "A"}:
            raise RunnerValidationError("AlphaEngine geography is not mapped")
        arguments: dict[str, Any] = {
            "query": query,
            "document_categories": _DOCUMENT_CATEGORY_MAP[document_type],
            "sort": "relevance",
            "limit": min(wire["max_records"], 100),
            "include_snippets": True,
            "optional_only": False,
        }
        if parameters.get("cursor") is not None:
            arguments["cursor"] = parameters["cursor"]
        if date_from is not None:
            arguments["date_from"] = date_from
            arguments["date_to"] = date_to
        if geography is not None:
            arguments["markets"] = [geography]
        if filters.get("industry") is not None:
            arguments["industry_names"] = [filters["industry"]]
        return arguments
    document_ref = parameters["document_ref"]
    doc_id = document_ref.removeprefix("alphaengine-doc:")
    if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", doc_id) is None:
        raise RunnerValidationError("AlphaEngine document_ref is invalid")
    cursor = parameters.get("cursor")
    offset = 0
    if cursor is not None:
        if re.fullmatch(r"[0-9]+", cursor) is None:
            raise RunnerValidationError("AlphaEngine document cursor must be an offset")
        offset = int(cursor)
    return {
        "doc_id": doc_id,
        "offset": offset,
        "max_chars": min(100_000, max(1, wire["max_response_bytes"] // 6)),
        "mode": "auto",
    }


class AlphaEngineLiveAdapter:
    """Normalize exact AlphaEngine MCP results into Dalton's frozen output."""

    def __call__(
        self,
        request: Mapping[str, Any],
        raw_sink: Any,
        credential_handle: Any,
    ) -> dict[str, Any]:
        wire = validate_live_mcp_adapter_request(request)
        invoke = getattr(credential_handle, "invoke", None)
        if not callable(invoke):
            raise RunnerValidationError("host-owned MCP handle lacks invoke")
        arguments = alphaengine_tool_arguments(wire)
        try:
            invocation = invoke(
                wire["tool_name"],
                arguments,
                call_ref=wire["credential_use_ref"],
                deadline_at=wire["deadline_at"],
                max_response_bytes=wire["max_response_bytes"],
            )
        except BridgeRateLimited as exc:
            return self._failure(
                wire,
                outcome="rate_limited",
                code="rate_limited",
                message=str(exc),
                retryable=True,
                provider_status=429,
                retry_after_ms=exc.retry_after_ms,
            )
        except BridgePermissionDenied as exc:
            return self._failure(
                wire,
                outcome="failed",
                code="permission_denied",
                message=str(exc),
                retryable=False,
                provider_status=403,
                retry_after_ms=None,
            )
        if not isinstance(invocation, HostToolInvocationResult):
            raise RunnerValidationError("host-owned MCP handle returned another type")
        if len(invocation.raw_response) > wire["max_response_bytes"]:
            raise RunnerValidationError("AlphaEngine raw response exceeds byte limit")
        result = invocation.result
        if result.get("isError") is True:
            message = "AlphaEngine MCP tool returned an error"
            content = result.get("content")
            if isinstance(content, list):
                parts = [
                    str(item.get("text")) for item in content
                    if isinstance(item, Mapping) and item.get("type") == "text"
                ]
                if parts:
                    message = "\n".join(parts)[:1000]
            lowered = message.lower()
            permission = any(word in lowered for word in ("permission", "login", "auth"))
            return self._failure(
                wire,
                outcome="failed",
                code="permission_denied" if permission else "source_error",
                message=message,
                retryable=not permission,
                provider_status=403 if permission else 502,
                retry_after_ms=None,
            )
        payload = _tool_text_payload(result)
        raw_sink.write(invocation.raw_response)
        if wire["operation"] == "search_library":
            results = payload.get("results")
            if not isinstance(results, list):
                raise RunnerValidationError("AlphaEngine search results must be an array")
            doc_ids: list[str] = []
            for index, item in enumerate(results):
                if not isinstance(item, Mapping):
                    raise RunnerValidationError(
                        f"AlphaEngine search result[{index}] must be an object"
                    )
                doc_id = item.get("doc_id")
                if not isinstance(doc_id, str) or not doc_id:
                    raise RunnerValidationError("AlphaEngine search result lacks doc_id")
                doc_ids.append(doc_id)
            if len(doc_ids) != len(set(doc_ids)):
                raise RunnerValidationError("AlphaEngine search returned duplicate doc_id")
            if len(doc_ids) > wire["max_records"]:
                raise RunnerValidationError("AlphaEngine search exceeded max_records")
            cursor = payload.get("cursor")
            if cursor is not None and not isinstance(cursor, str):
                raise RunnerValidationError("AlphaEngine search cursor must be a string")
            has_more = payload.get("has_more")
            if type(has_more) is not bool or has_more != (cursor is not None):
                raise RunnerValidationError("AlphaEngine search cursor semantics drifted")
            refs = [f"alphaengine-doc:{doc_id}" for doc_id in doc_ids]
            completeness = "ranked"
        else:
            page = validate_alphaengine_document_page(
                payload,
                expected_doc_id=arguments["doc_id"],
                expected_offset=arguments["offset"],
                max_chars=arguments["max_chars"],
            )
            cursor = page["cursor"]
            completeness = page["completeness"]
            refs = [page["source_record_ref"]]
        source_status = (
            "empty" if not refs else "partial" if cursor is not None else "complete"
        )
        structured = {
            "source_record_refs": refs,
            "next_cursor": cursor,
            "provider_status": 200,
        }
        base = {
            "protocol_version": "0.2",
            "request_hash": wire["content_hash"],
            "outcome": "succeeded",
            "provider_request_id": invocation.request_id,
            "provider_status_code": 200,
            "retry_after_ms": None,
            "structured_output": structured,
            "source_record_refs": refs,
            "cursor": cursor,
            "provider_usage": None,
            "source_status": source_status,
            "completeness": completeness,
            "error": None,
        }
        return validate_mcp_managed_transport_observation(
            {**base, "content_hash": content_hash(base)}
        )

    @staticmethod
    def _failure(
        wire: Mapping[str, Any],
        *,
        outcome: str,
        code: str,
        message: str,
        retryable: bool,
        provider_status: int,
        retry_after_ms: int | None,
    ) -> dict[str, Any]:
        base = {
            "protocol_version": "0.2",
            "request_hash": wire["content_hash"],
            "outcome": outcome,
            "provider_request_id": None,
            "provider_status_code": provider_status,
            "retry_after_ms": retry_after_ms,
            "structured_output": None,
            "source_record_refs": [],
            "cursor": None,
            "provider_usage": None,
            "source_status": None,
            "completeness": None,
            "error": {"code": code, "message": message[:1000], "retryable": retryable},
        }
        return validate_mcp_managed_transport_observation(
            {**base, "content_hash": content_hash(base)}
        )


__all__ = [
    "AlphaEngineLiveAdapter",
    "LIVE_MCP_ADAPTER_PROTOCOL_VERSION",
    "LIVE_MCP_TRANSPORT_PLAN_VERSION",
    "LiveMcpRunnerAdmissionGate",
    "OPENCLAW_ALPHAENGINE_BRIDGE_HASH",
    "OPENCLAW_ALPHAENGINE_BRIDGE_REF",
    "alphaengine_document_page_from_raw_response",
    "alphaengine_tool_arguments",
    "build_live_mcp_transport_plan",
    "validate_live_mcp_adapter_request",
    "validate_live_mcp_transport_plan",
]
