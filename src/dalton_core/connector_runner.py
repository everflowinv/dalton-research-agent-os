"""Closed control-plane seam for Dalton's trusted connector runner.

This module deliberately stops before transport execution.  It validates the
exact Scheduler and Capability leases, resolves only operator-injected adapter
objects, and derives the internal adapter request from authority records.  The
durable journal, raw spool, credential grants, and writer commit loop are the
next P0-2 slice.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .capability_catalog import CapabilityLease, CapabilityPermissions
from .contracts import ExecutionInvocation, WorkOrder
from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
PROTOCOL_VERSION = "0.1"
RUNNER_RESPONSE_SCHEMA_VERSION = "0.2"


class ConnectorRunnerError(Exception):
    """Base error for trusted-runner control-plane failures."""


class RunnerValidationError(ConnectorRunnerError):
    pass


class RunnerConflict(ConnectorRunnerError):
    pass


class AdapterNotResolved(ConnectorRunnerError):
    pass


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RunnerValidationError(f"{name} must be an object")
    return dict(value)


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    obj = _mapping(value, name)
    if set(obj) != fields:
        missing = sorted(fields - set(obj))
        unknown = sorted(set(obj) - fields)
        raise RunnerValidationError(f"{name} closed shape mismatch: missing={missing}, unknown={unknown}")
    return obj


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RunnerValidationError(f"{name} must be a non-empty string")
    return value


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise RunnerValidationError(f"{name} must be lowercase SHA-256 hex")
    return value


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RunnerValidationError(f"{name} must be an integer >= {minimum}")
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


def _optional_timestamp(value: Any, name: str) -> str | None:
    return None if value is None else _timestamp(value, name)


def _refs(value: Any, name: str) -> list[str]:
    if not isinstance(value, list):
        raise RunnerValidationError(f"{name} must be an array")
    result = [_text(item, f"{name}[]") for item in value]
    if len(result) != len(set(result)):
        raise RunnerValidationError(f"{name} must contain unique values")
    return result


def _with_hash(wire: Mapping[str, Any], name: str) -> dict[str, Any]:
    obj = dict(wire)
    declared = _hash(obj.pop("content_hash"), f"{name}.content_hash")
    expected = content_hash(obj)
    if declared != expected:
        raise RunnerConflict(f"{name} content_hash mismatch")
    obj["content_hash"] = declared
    return obj


_RUNNER_REQUEST_FIELDS = {
    "schema_version", "id", "created_at", "connector_invocation_ref",
    "connector_invocation_hash", "execution_ref", "execution_hash",
    "work_order_ref", "work_order_hash", "scheduler_attempt_number",
    "scheduler_lease_revision_ref", "scheduler_lease_hash",
    "connector_profile_ref", "connector_profile_hash", "call_spec_ref",
    "call_spec_hash", "capability_lease_ref", "capability_lease_hash",
    "principal_ref", "runner_runtime_ref", "runner_actor_ref",
    "runner_environment_hash", "idempotency_key", "content_hash",
}
_MCP_RUNNER_REQUEST_FIELDS = _RUNNER_REQUEST_FIELDS | {
    "transport_plan_ref", "transport_plan_hash",
}
_COMPILED_PLAN_BINDING_FIELDS = {
    "compiled_connector_plan_ref", "compiled_connector_plan_hash",
    "compiled_step_ref", "compiled_step_hash",
}


def validate_connector_runner_request(value: Mapping[str, Any]) -> dict[str, Any]:
    version = value.get("schema_version") if isinstance(value, Mapping) else None
    base_fields = (
        _RUNNER_REQUEST_FIELDS if version == SCHEMA_VERSION
        else _MCP_RUNNER_REQUEST_FIELDS if version == "0.2"
        else None
    )
    if base_fields is None:
        raise RunnerValidationError("unsupported ConnectorRunnerRequest schema_version")
    supplied_fields = set(value) if isinstance(value, Mapping) else set()
    plan_fields = supplied_fields & _COMPILED_PLAN_BINDING_FIELDS
    if plan_fields and plan_fields != _COMPILED_PLAN_BINDING_FIELDS:
        raise RunnerValidationError(
            "ConnectorRunnerRequest compiled plan binding must be complete"
        )
    fields = base_fields | (_COMPILED_PLAN_BINDING_FIELDS if plan_fields else set())
    wire = _closed(value, fields, "ConnectorRunnerRequest")
    for name in (
        "id", "connector_invocation_ref", "execution_ref", "work_order_ref",
        "scheduler_lease_revision_ref", "connector_profile_ref", "call_spec_ref",
        "capability_lease_ref", "principal_ref", "runner_runtime_ref",
        "runner_actor_ref", "idempotency_key",
    ):
        wire[name] = _text(wire[name], name)
    for name in (
        "connector_invocation_hash", "execution_hash", "work_order_hash",
        "scheduler_lease_hash", "connector_profile_hash", "call_spec_hash",
        "capability_lease_hash", "runner_environment_hash",
    ):
        wire[name] = _hash(wire[name], name)
    if version == "0.2":
        wire["transport_plan_ref"] = _text(
            wire["transport_plan_ref"], "transport_plan_ref"
        )
        wire["transport_plan_hash"] = _hash(
            wire["transport_plan_hash"], "transport_plan_hash"
        )
    if plan_fields:
        wire["compiled_connector_plan_ref"] = _text(
            wire["compiled_connector_plan_ref"], "compiled_connector_plan_ref"
        )
        wire["compiled_connector_plan_hash"] = _hash(
            wire["compiled_connector_plan_hash"], "compiled_connector_plan_hash"
        )
        wire["compiled_step_ref"] = _text(
            wire["compiled_step_ref"], "compiled_step_ref"
        )
        wire["compiled_step_hash"] = _hash(
            wire["compiled_step_hash"], "compiled_step_hash"
        )
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    wire["scheduler_attempt_number"] = _integer(
        wire["scheduler_attempt_number"], "scheduler_attempt_number", minimum=1
    )
    return _with_hash(wire, "ConnectorRunnerRequest")


_BINDING_FIELDS = {
    "binding_ref", "descriptor_revision_ref", "descriptor_hash", "adapter_ref",
    "adapter_hash", "source_ref", "source_hash", "operation", "input_schema_ref",
    "input_schema_hash", "output_schema_ref", "output_schema_hash", "auth_mode",
    "credential_slot_refs", "required_permissions", "side_effects", "rate_policy_ref",
}


def _validate_binding(value: Mapping[str, Any], index: int) -> dict[str, Any]:
    wire = _closed(value, _BINDING_FIELDS, f"bindings[{index}]")
    for name in (
        "binding_ref", "descriptor_revision_ref", "adapter_ref", "source_ref",
        "operation", "input_schema_ref", "output_schema_ref", "auth_mode",
        "rate_policy_ref",
    ):
        wire[name] = _text(wire[name], name)
    for name in (
        "descriptor_hash", "adapter_hash", "source_hash", "input_schema_hash",
        "output_schema_hash",
    ):
        wire[name] = _hash(wire[name], name)
    if wire["auth_mode"] not in {"none", "credential_slot", "mcp_managed"}:
        raise RunnerValidationError("binding auth_mode is invalid")
    wire["credential_slot_refs"] = sorted(
        _refs(wire["credential_slot_refs"], "credential_slot_refs")
    )
    if wire["auth_mode"] == "none" and wire["credential_slot_refs"]:
        raise RunnerValidationError("auth_mode none cannot declare credential slots")
    if wire["auth_mode"] != "none" and not wire["credential_slot_refs"]:
        raise RunnerValidationError("authenticated binding requires credential slots")
    try:
        permissions = CapabilityPermissions.from_dict(wire["required_permissions"])
    except Exception as exc:
        raise RunnerValidationError(str(exc)) from exc
    wire["required_permissions"] = permissions.to_dict()
    wire["side_effects"] = sorted(_refs(wire["side_effects"], "side_effects"))
    return wire


_MANIFEST_FIELDS = {
    "schema_version", "id", "created_at", "runner_runtime_ref", "runner_actor_ref",
    "resolver_ref", "resolver_version", "package_manifest_ref", "package_manifest_hash",
    "bindings", "content_hash",
}


def validate_runner_environment_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = _closed(value, _MANIFEST_FIELDS, "RunnerEnvironmentManifest")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise RunnerValidationError("unsupported RunnerEnvironmentManifest schema_version")
    for name in (
        "id", "runner_runtime_ref", "runner_actor_ref", "resolver_ref",
        "resolver_version", "package_manifest_ref",
    ):
        wire[name] = _text(wire[name], name)
    wire["package_manifest_hash"] = _hash(
        wire["package_manifest_hash"], "package_manifest_hash"
    )
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    if not isinstance(wire["bindings"], list) or not wire["bindings"]:
        raise RunnerValidationError("manifest bindings must be a non-empty array")
    bindings = [_validate_binding(item, index) for index, item in enumerate(wire["bindings"])]
    binding_refs = [item["binding_ref"] for item in bindings]
    resolve_keys = [
        (
            item["adapter_ref"], item["adapter_hash"], item["source_hash"],
            item["operation"], item["input_schema_hash"], item["output_schema_hash"],
        )
        for item in bindings
    ]
    if len(binding_refs) != len(set(binding_refs)):
        raise RunnerValidationError("binding_ref values must be unique")
    if len(resolve_keys) != len(set(resolve_keys)):
        raise RunnerValidationError("static resolver keys must be unique")
    wire["bindings"] = sorted(bindings, key=lambda item: item["binding_ref"])
    return _with_hash(wire, "RunnerEnvironmentManifest")


_ADAPTER_REQUEST_FIELDS = {
    "protocol_version", "runner_request_ref", "runner_request_hash",
    "connector_invocation_ref", "profile_ref", "profile_hash", "call_spec_ref",
    "call_spec_hash", "reservation_ref", "reservation_hash", "physical_attempt_number",
    "source_identity", "source_hash", "adapter_ref", "adapter_hash", "resolver_ref",
    "resolver_manifest_hash", "operation", "parameters", "query_hash",
    "input_schema_ref", "input_schema_hash", "output_schema_ref", "output_schema_hash",
    "allowed_hosts", "network_policy", "credential_grant_ref", "deadline_at",
    "max_response_bytes", "max_records", "raw_sink_ref", "content_hash",
}
_PAGINATED_ADAPTER_REQUEST_FIELDS = _ADAPTER_REQUEST_FIELDS | {"pagination_authority"}


def validate_connector_adapter_request(value: Mapping[str, Any]) -> dict[str, Any]:
    version = value.get("protocol_version") if isinstance(value, Mapping) else None
    fields = (
        _ADAPTER_REQUEST_FIELDS
        if version == PROTOCOL_VERSION
        else _PAGINATED_ADAPTER_REQUEST_FIELDS
        if version == "0.2"
        else None
    )
    if fields is None:
        raise RunnerValidationError("unsupported ConnectorAdapterRequest protocol_version")
    wire = _closed(value, fields, "ConnectorAdapterRequest")
    for name in (
        "runner_request_ref", "connector_invocation_ref", "profile_ref", "call_spec_ref",
        "reservation_ref", "adapter_ref", "resolver_ref", "operation", "input_schema_ref",
        "output_schema_ref", "raw_sink_ref",
    ):
        wire[name] = _text(wire[name], name)
    if not re.fullmatch(r"raw-sink:[0-9a-f]{64}", wire["raw_sink_ref"]):
        raise RunnerValidationError("raw_sink_ref must be a Runner-derived opaque handle")
    for name in (
        "runner_request_hash", "profile_hash", "call_spec_hash", "reservation_hash",
        "source_hash", "adapter_hash", "resolver_manifest_hash", "query_hash",
        "input_schema_hash", "output_schema_hash",
    ):
        wire[name] = _hash(wire[name], name)
    wire["physical_attempt_number"] = _integer(
        wire["physical_attempt_number"], "physical_attempt_number", minimum=1
    )
    wire["max_response_bytes"] = _integer(
        wire["max_response_bytes"], "max_response_bytes", minimum=1
    )
    wire["max_records"] = _integer(wire["max_records"], "max_records", minimum=1)
    wire["deadline_at"] = _timestamp(wire["deadline_at"], "deadline_at")
    wire["credential_grant_ref"] = (
        None if wire["credential_grant_ref"] is None
        else _text(wire["credential_grant_ref"], "credential_grant_ref")
    )
    if wire["credential_grant_ref"] is not None:
        raise RunnerValidationError(
            "credential grants are not supported by connector protocol 0.1"
        )
    wire["parameters"] = json.loads(canonical_json(_mapping(wire["parameters"], "parameters")))
    if version == "0.2":
        pagination = _closed(
            wire["pagination_authority"],
            {
                "mode", "cursor_field", "page_ordinal", "request_cursor",
                "parent_query_hash", "prior_adapter_request_hash",
                "prior_observation_hash", "prior_physical_attempt_ref",
                "prior_physical_attempt_hash", "recorded_scenario_ref",
                "recorded_scenario_hash", "recorded_behavior",
            },
            "pagination_authority",
        )
        if pagination["mode"] not in {"page", "cursor"}:
            raise RunnerValidationError("pagination authority mode is invalid")
        expected_cursor_field = "page" if pagination["mode"] == "page" else "cursor"
        if pagination["cursor_field"] != expected_cursor_field:
            raise RunnerValidationError("pagination cursor field is inconsistent")
        pagination["page_ordinal"] = _integer(
            pagination["page_ordinal"], "pagination_authority.page_ordinal", minimum=1
        )
        if pagination["page_ordinal"] != wire["physical_attempt_number"]:
            raise RunnerValidationError("pagination ordinal must bind physical attempt")
        pagination["request_cursor"] = (
            None if pagination["request_cursor"] is None
            else _text(pagination["request_cursor"], "pagination_authority.request_cursor")
        )
        pagination["parent_query_hash"] = _hash(
            pagination["parent_query_hash"], "pagination_authority.parent_query_hash"
        )
        pagination["recorded_scenario_ref"] = _text(
            pagination["recorded_scenario_ref"],
            "pagination_authority.recorded_scenario_ref",
        )
        pagination["recorded_scenario_hash"] = _hash(
            pagination["recorded_scenario_hash"],
            "pagination_authority.recorded_scenario_hash",
        )
        if pagination["recorded_behavior"] not in {
            "return", "rate_limited", "timeout", "normalize_error",
        }:
            raise RunnerValidationError("recorded scenario behavior is invalid")
        prior_names = (
            "prior_adapter_request_hash", "prior_observation_hash",
            "prior_physical_attempt_ref", "prior_physical_attempt_hash",
        )
        for name in prior_names:
            prior = pagination[name]
            if prior is None:
                continue
            pagination[name] = (
                _text(prior, f"pagination_authority.{name}")
                if name == "prior_physical_attempt_ref"
                else _hash(prior, f"pagination_authority.{name}")
            )
        prior_values = [pagination[name] for name in prior_names]
        if pagination["page_ordinal"] == 1:
            if pagination["request_cursor"] is not None or any(
                prior is not None for prior in prior_values
            ):
                raise RunnerValidationError("first page cannot claim prior pagination authority")
        elif pagination["request_cursor"] is None or any(
            prior is None for prior in prior_values
        ):
            raise RunnerValidationError("continued page requires complete prior authority")
        expected_query_hash = content_hash(
            {"operation": wire["operation"], "parameters": wire["parameters"]}
        )
        if wire["query_hash"] != expected_query_hash:
            raise RunnerValidationError("paginated request query_hash is not page-specific")
        wire["pagination_authority"] = pagination
    identity = _closed(
        wire["source_identity"],
        {"source_ref", "source_type", "source_version"},
        "source_identity",
    )
    for name in identity:
        identity[name] = _text(identity[name], f"source_identity.{name}")
    if identity["source_type"] not in {
        "official_filing", "authenticated_library", "social_enumeration",
        "social_search", "public_web", "market_data",
    }:
        raise RunnerValidationError("source_identity.source_type is invalid")
    wire["source_identity"] = identity
    network = _closed(
        wire["network_policy"],
        {"allowed_schemes", "allow_redirects", "max_redirects", "resolve_public_only"},
        "network_policy",
    )
    schemes = _refs(network["allowed_schemes"], "network_policy.allowed_schemes")
    if not schemes or any(scheme != "https" for scheme in schemes):
        raise RunnerValidationError("network policy only permits https")
    if type(network["allow_redirects"]) is not bool or type(network["resolve_public_only"]) is not bool:
        raise RunnerValidationError("network policy flags must be boolean")
    network["max_redirects"] = _integer(
        network["max_redirects"], "network_policy.max_redirects"
    )
    if not network["allow_redirects"] and network["max_redirects"] != 0:
        raise RunnerValidationError("disabled redirects require max_redirects=0")
    if not network["resolve_public_only"]:
        raise RunnerValidationError("connector protocol 0.1 requires public-only resolution")
    network["allowed_schemes"] = schemes
    wire["network_policy"] = network
    wire["allowed_hosts"] = sorted(_refs(wire["allowed_hosts"], "allowed_hosts"))
    return _with_hash(wire, "ConnectorAdapterRequest")


_OBSERVATION_FIELDS = {
    "protocol_version", "request_hash", "outcome", "provider_request_id",
    "provider_status_code", "retry_after_ms", "structured_output",
    "source_record_refs", "cursor", "provider_usage", "error", "content_hash",
}


def validate_adapter_transport_observation(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = _closed(value, _OBSERVATION_FIELDS, "AdapterTransportObservation")
    if wire["protocol_version"] != PROTOCOL_VERSION:
        raise RunnerValidationError("unsupported AdapterTransportObservation protocol_version")
    wire["request_hash"] = _hash(wire["request_hash"], "request_hash")
    if wire["outcome"] not in {"succeeded", "rate_limited", "failed"}:
        raise RunnerValidationError("adapter observation outcome is invalid")
    wire["provider_request_id"] = (
        None if wire["provider_request_id"] is None
        else _text(wire["provider_request_id"], "provider_request_id")
    )
    wire["provider_status_code"] = (
        None if wire["provider_status_code"] is None
        else _integer(wire["provider_status_code"], "provider_status_code", minimum=100)
    )
    wire["retry_after_ms"] = (
        None if wire["retry_after_ms"] is None
        else _integer(wire["retry_after_ms"], "retry_after_ms", minimum=1)
    )
    if wire["outcome"] == "rate_limited" and wire["retry_after_ms"] is None:
        raise RunnerValidationError("rate_limited observation requires retry_after_ms")
    if wire["outcome"] != "rate_limited" and wire["retry_after_ms"] is not None:
        raise RunnerValidationError("retry_after_ms is only valid for rate_limited")
    if wire["structured_output"] is not None and not isinstance(
        wire["structured_output"], (Mapping, list)
    ):
        raise RunnerValidationError("structured_output must be object, array, or null")
    wire["structured_output"] = json.loads(canonical_json(wire["structured_output"]))
    wire["source_record_refs"] = _refs(wire["source_record_refs"], "source_record_refs")
    wire["cursor"] = None if wire["cursor"] is None else _text(wire["cursor"], "cursor")
    if wire["provider_usage"] is not None:
        wire["provider_usage"] = json.loads(
            canonical_json(_mapping(wire["provider_usage"], "provider_usage"))
        )
    if wire["error"] is not None:
        error = _closed(wire["error"], {"code", "message", "retryable"}, "error")
        error["code"] = _text(error["code"], "error.code")
        error["message"] = _text(error["message"], "error.message")
        if type(error["retryable"]) is not bool:
            raise RunnerValidationError("error.retryable must be boolean")
        wire["error"] = error
    if wire["outcome"] == "succeeded" and wire["error"] is not None:
        raise RunnerValidationError("succeeded observation cannot contain error")
    if wire["outcome"] == "failed" and wire["error"] is None:
        raise RunnerValidationError("failed observation requires error")
    return _with_hash(wire, "AdapterTransportObservation")


_RUNNER_RESPONSE_FIELDS = {
    "schema_version", "id", "created_at", "runner_request_ref", "runner_request_hash",
    "idempotency_status", "connector_invocation_ref", "connector_invocation_hash",
    "physical_attempt_ref", "physical_attempt_hash", "usage_entry_ref", "usage_entry_hash",
    "cost_entry_ref", "cost_entry_hash", "quota_settlement_ref", "quota_settlement_hash",
    "raw_artifact_version_ref", "raw_artifact_version_hash", "source_envelope_ref",
    "source_envelope_hash", "result_envelope_ref", "result_envelope_hash", "outcome",
    "retry_at", "content_hash",
}


def validate_connector_runner_response(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = _closed(value, _RUNNER_RESPONSE_FIELDS, "ConnectorRunnerResponse")
    if wire["schema_version"] != RUNNER_RESPONSE_SCHEMA_VERSION:
        raise RunnerValidationError("unsupported ConnectorRunnerResponse schema_version")
    for name in (
        "id", "runner_request_ref", "connector_invocation_ref", "physical_attempt_ref",
        "usage_entry_ref", "cost_entry_ref", "quota_settlement_ref",
        "result_envelope_ref",
    ):
        wire[name] = _text(wire[name], name)
    for name in (
        "runner_request_hash", "connector_invocation_hash", "physical_attempt_hash",
        "usage_entry_hash", "cost_entry_hash", "quota_settlement_hash",
        "result_envelope_hash",
    ):
        wire[name] = _hash(wire[name], name)
    for ref_name, hash_name in (
        ("raw_artifact_version_ref", "raw_artifact_version_hash"),
        ("source_envelope_ref", "source_envelope_hash"),
    ):
        if (wire[ref_name] is None) != (wire[hash_name] is None):
            raise RunnerValidationError(f"{ref_name}/{hash_name} must be null together")
        if wire[ref_name] is not None:
            wire[ref_name] = _text(wire[ref_name], ref_name)
            wire[hash_name] = _hash(wire[hash_name], hash_name)
    wire["created_at"] = _timestamp(wire["created_at"], "created_at")
    if wire["idempotency_status"] not in {"fresh", "duplicate"}:
        raise RunnerValidationError("idempotency_status is invalid")
    if wire["outcome"] not in {"succeeded", "retryable", "failed"}:
        raise RunnerValidationError("runner outcome is invalid")
    wire["retry_at"] = _optional_timestamp(wire["retry_at"], "retry_at")
    if wire["outcome"] == "retryable" and wire["retry_at"] is None:
        raise RunnerValidationError("retryable response requires retry_at")
    if wire["outcome"] != "retryable" and wire["retry_at"] is not None:
        raise RunnerValidationError("retry_at is only valid for retryable response")
    if wire["source_envelope_ref"] is not None and wire["raw_artifact_version_ref"] is None:
        raise RunnerValidationError("SourceEnvelope requires a raw artifact")
    if wire["outcome"] == "succeeded" and (
        wire["raw_artifact_version_ref"] is None
        or wire["source_envelope_ref"] is None
    ):
        raise RunnerValidationError(
            "succeeded response requires raw artifact and SourceEnvelope"
        )
    if wire["outcome"] != "succeeded" and (
        wire["raw_artifact_version_ref"] is not None
        or wire["source_envelope_ref"] is not None
    ):
        raise RunnerValidationError(
            "non-success response cannot claim raw artifact or SourceEnvelope"
        )
    return _with_hash(wire, "ConnectorRunnerResponse")


class StaticAdapterResolver:
    """Resolve only exact operator-installed objects from one frozen manifest."""

    def __init__(
        self,
        manifest: Mapping[str, Any],
        adapters: Mapping[str, Any],
        input_validators: Mapping[str, Callable[[Mapping[str, Any]], bool]],
    ):
        self.manifest = validate_runner_environment_manifest(manifest)
        supplied = dict(adapters)
        supplied_validators = dict(input_validators)
        expected = {item["binding_ref"] for item in self.manifest["bindings"]}
        if set(supplied) != expected:
            raise RunnerValidationError("adapter objects must match manifest binding refs exactly")
        if set(supplied_validators) != expected:
            raise RunnerValidationError(
                "input validators must match manifest binding refs exactly"
            )
        if any(not callable(adapter) for adapter in supplied.values()):
            raise RunnerValidationError("every static adapter object must be callable")
        if any(not callable(validator) for validator in supplied_validators.values()):
            raise RunnerValidationError("every static input validator must be callable")
        self._adapters = MappingProxyType(supplied)
        self._input_validators = MappingProxyType(supplied_validators)

    @property
    def content_hash(self) -> str:
        return self.manifest["content_hash"]

    def resolve(
        self,
        profile: Mapping[str, Any],
        call_spec: Mapping[str, Any],
        lease: CapabilityLease,
    ) -> tuple[dict[str, Any], Any]:
        if profile["runner_environment_hash"] != self.content_hash:
            raise AdapterNotResolved("profile runner environment is not installed")
        if (
            profile["runner_runtime_ref"] != self.manifest["runner_runtime_ref"]
            or profile["runner_actor_ref"] != self.manifest["runner_actor_ref"]
        ):
            raise AdapterNotResolved("profile runner identity does not match manifest")
        candidates = [
            binding for binding in self.manifest["bindings"]
            if (
                binding["adapter_ref"] == profile["adapter_ref"]
                and binding["adapter_hash"] == profile["adapter_hash"]
                and binding["source_ref"] == profile["source_identity"]["source_ref"]
                and binding["source_hash"] == profile["source_hash"]
                and binding["operation"] == call_spec["operation"]
                and binding["input_schema_hash"]
                == profile["input_schema_hashes"][call_spec["operation"]]
                and binding["output_schema_hash"]
                == profile["output_schema_hashes"][call_spec["operation"]]
            )
        ]
        if len(candidates) != 1:
            raise AdapterNotResolved("no unique exact adapter binding")
        binding = candidates[0]
        exact = (
            binding["descriptor_revision_ref"] == profile["descriptor_revision_ref"]
            and binding["descriptor_hash"] == profile["descriptor_hash"]
            and binding["input_schema_ref"]
            == profile["input_schema_refs"][call_spec["operation"]]
            and binding["output_schema_ref"]
            == profile["output_schema_refs"][call_spec["operation"]]
            and binding["auth_mode"] == profile["auth_mode"]
            and binding["credential_slot_refs"] == sorted(profile["credential_slot_refs"])
            and binding["required_permissions"] == lease.permissions.to_dict()
            and binding["descriptor_revision_ref"] == lease.revision_ref
            and binding["descriptor_hash"] == lease.descriptor_hash
            and binding["source_hash"] == lease.source_hash
            and profile["schema_hash"] == lease.schema_hash
        )
        if not exact:
            raise AdapterNotResolved("adapter binding authority does not match profile/lease")
        if not set(binding["side_effects"]).issubset(lease.allowed_side_effects):
            raise AdapterNotResolved("adapter side effects exceed capability lease")
        return binding, self._adapters[binding["binding_ref"]]

    def validate_input(
        self, binding: Mapping[str, Any], parameters: Mapping[str, Any]
    ) -> None:
        try:
            accepted = self._input_validators[binding["binding_ref"]](
                json.loads(canonical_json(parameters))
            )
        except Exception as exc:
            raise RunnerValidationError("connector input schema validation failed") from exc
        if accepted is not True:
            raise RunnerValidationError("connector input schema validation failed")


@dataclass(frozen=True, slots=True)
class ValidatedRunnerAdmission:
    request: Mapping[str, Any]
    work_order: WorkOrder
    scheduler_lease: Mapping[str, Any]
    capability_lease: CapabilityLease
    profile: Mapping[str, Any]
    call_spec: Mapping[str, Any]
    invocation: Mapping[str, Any]
    execution: ExecutionInvocation
    descriptor: Any
    binding: Mapping[str, Any]
    adapter: Callable[..., Any]
    deadline_at: str


class ConnectorRunnerAdmissionGate:
    """Validate every authority epoch before reservation or adapter execution."""

    def __init__(
        self,
        *,
        scheduler: Any,
        catalog: Any,
        connectors: Any,
        resolver: StaticAdapterResolver,
        visibility_scopes: Sequence[str],
        clock: Callable[[], datetime] | None = None,
    ):
        self.scheduler = scheduler
        self.catalog = catalog
        self.connectors = connectors
        self.resolver = resolver
        if (
            not isinstance(visibility_scopes, Sequence)
            or isinstance(visibility_scopes, (str, bytes))
            or not visibility_scopes
            or any(not isinstance(item, str) or not item for item in visibility_scopes)
        ):
            raise RunnerValidationError("visibility_scopes must be non-empty strings")
        self.visibility_scopes = tuple(visibility_scopes)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _reject_unverified_compiled_plan_binding(
        wire: Mapping[str, Any]
    ) -> None:
        if any(field in wire for field in _COMPILED_PLAN_BINDING_FIELDS):
            raise RunnerConflict(
                "connector runner has no compiled-plan authority resolver"
            )

    def _validate_runner_request_protocol(self, wire: Mapping[str, Any]) -> None:
        if wire["schema_version"] != SCHEMA_VERSION:
            raise RunnerConflict(
                "public connector runner requires RunnerRequest wire 0.1"
            )
        self._reject_unverified_compiled_plan_binding(wire)

    def validate(
        self, request: Mapping[str, Any], *, scheduler_lease_token: str
    ) -> ValidatedRunnerAdmission:
        wire = validate_connector_runner_request(request)
        self._validate_runner_request_protocol(wire)
        scheduler_state = self.scheduler.validate_lease_for_use(
            wire["work_order_ref"],
            wire["scheduler_attempt_number"],
            wire["runner_actor_ref"],
            scheduler_lease_token,
            lease_revision_ref=wire["scheduler_lease_revision_ref"],
            lease_hash=wire["scheduler_lease_hash"],
            work_order_hash=wire["work_order_hash"],
        )
        work_order = WorkOrder.from_dict(scheduler_state["work_order"])
        invocation = self.connectors.get_invocation(wire["connector_invocation_ref"])
        profile = self.connectors.get_profile(wire["connector_profile_ref"])
        call_spec = self.connectors.get_call_spec(wire["call_spec_ref"])
        execution_row = self.connectors.connection.execute(
            "SELECT execution_json,content_hash FROM execution_invocations WHERE execution_id=?",
            (wire["execution_ref"],),
        ).fetchone()
        if execution_row is None:
            raise RunnerConflict("runner execution authority is missing")
        execution = ExecutionInvocation.from_dict(json.loads(execution_row["execution_json"]))
        expected = (
            invocation["content_hash"], invocation["execution_ref"],
            invocation["execution_hash"], invocation["work_order_ref"],
            invocation["work_order_hash"], invocation["connector_profile_ref"],
            invocation["connector_profile_hash"], invocation["call_spec_ref"],
            invocation["call_spec_hash"], invocation["capability_lease_ref"],
            invocation["capability_lease_hash"], profile["content_hash"],
            call_spec["content_hash"], execution_row["content_hash"],
        )
        actual = (
            wire["connector_invocation_hash"], wire["execution_ref"], wire["execution_hash"],
            wire["work_order_ref"], wire["work_order_hash"], wire["connector_profile_ref"],
            wire["connector_profile_hash"], wire["call_spec_ref"], wire["call_spec_hash"],
            wire["capability_lease_ref"], wire["capability_lease_hash"],
            wire["connector_profile_hash"], wire["call_spec_hash"], wire["execution_hash"],
        )
        if expected != actual:
            raise RunnerConflict("RunnerRequest refs/hashes do not match connector authority")
        if (
            call_spec["work_order_ref"] != work_order.id
            or call_spec["work_order_hash"] != wire["work_order_hash"]
            or call_spec["connector_profile_ref"] != profile["id"]
            or call_spec["id"] not in work_order.input_refs
        ):
            raise RunnerConflict("CallSpec does not belong to the admitted WorkOrder/Profile")
        if (
            execution.work_order_ref != work_order.id
            or execution.profile_ref != profile["id"]
            or execution.capability != profile["capability_id"]
            or execution.runtime_ref != wire["runner_runtime_ref"]
            or execution.actor_ref != wire["runner_actor_ref"]
            or execution.environment_hash != wire["runner_environment_hash"]
            or profile["runner_runtime_ref"] != wire["runner_runtime_ref"]
            or profile["runner_actor_ref"] != wire["runner_actor_ref"]
            or profile["runner_environment_hash"] != wire["runner_environment_hash"]
        ):
            raise RunnerConflict("Execution/Profile runner identity is not exact")
        historical_lease = self.catalog.get_lease(wire["capability_lease_ref"])
        if historical_lease.content_hash != wire["capability_lease_hash"]:
            raise RunnerConflict("CapabilityLease hash does not match invocation")
        binding, adapter = self.resolver.resolve(profile, call_spec, historical_lease)
        capability_lease = self.catalog.validate_lease_for_use(
            historical_lease.id,
            work_order=work_order,
            principal_ref=wire["principal_ref"],
            permissions=binding["required_permissions"],
            side_effects=binding["side_effects"],
        )
        if capability_lease.content_hash != historical_lease.content_hash:
            raise RunnerConflict("CapabilityLease changed during use-time validation")
        descriptor = self.catalog.describe(
            capability_lease.capability_id,
            visibility_scopes=self.visibility_scopes,
            catalog_epoch=capability_lease.catalog_epoch,
        )
        if (
            capability_lease.capability_id != profile["capability_id"]
            or capability_lease.catalog_epoch != profile["catalog_epoch"]
            or tuple(sorted(capability_lease.credential_slot_refs))
            != tuple(sorted(profile["credential_slot_refs"]))
        ):
            raise RunnerConflict("CapabilityLease does not match ConnectorProfile")
        if (
            descriptor.kind != "connector"
            or descriptor.contract.mode != "typed_call"
            or descriptor.contract.instruction_ref is not None
            or descriptor.revision_ref != profile["descriptor_revision_ref"]
            or descriptor.content_hash != profile["descriptor_hash"]
            or descriptor.contract.adapter_ref != profile["adapter_ref"]
            or descriptor.contract.input_schema_ref
            != profile["input_schema_refs"][call_spec["operation"]]
            or descriptor.contract.output_schema_ref
            != profile["output_schema_refs"][call_spec["operation"]]
        ):
            raise RunnerConflict(
                "live capability contract does not match ConnectorProfile"
            )
        self.resolver.validate_input(binding, call_spec["parameters"])
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise RunnerValidationError("runner clock must return aware datetime")
        now = now.astimezone(timezone.utc)
        deadlines = [
            datetime.fromisoformat(scheduler_state["lease"]["expires_at"]),
            datetime.fromisoformat(capability_lease.expires_at),
            now + timedelta(milliseconds=int(profile["timeout_ms"])),
        ]
        max_seconds = work_order.budget.get("max_seconds")
        if isinstance(max_seconds, (int, float)) and not isinstance(max_seconds, bool) and max_seconds > 0:
            deadlines.append(now + timedelta(seconds=float(max_seconds)))
        deadline = min(value.astimezone(timezone.utc) for value in deadlines)
        if deadline <= now:
            raise RunnerConflict("effective runner deadline has expired")
        return ValidatedRunnerAdmission(
            request=MappingProxyType(wire),
            work_order=work_order,
            scheduler_lease=MappingProxyType(dict(scheduler_state["lease"])),
            capability_lease=capability_lease,
            profile=MappingProxyType(profile),
            call_spec=MappingProxyType(call_spec),
            invocation=MappingProxyType(invocation),
            execution=execution,
            descriptor=descriptor,
            binding=MappingProxyType(binding),
            adapter=adapter,
            deadline_at=deadline.isoformat(timespec="microseconds"),
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
        revalidated = self.validate(
            admission.request, scheduler_lease_token=scheduler_lease_token
        )
        if (
            revalidated.invocation["content_hash"] != initial.invocation["content_hash"]
            or revalidated.binding != initial.binding
            or revalidated.capability_lease.content_hash
            != initial.capability_lease.content_hash
        ):
            raise RunnerConflict("runner authority changed between admission gates")
        policy_row = self.connectors.connection.execute(
            "SELECT policy_ref FROM connector_rate_policy_versions WHERE policy_version_id=?",
            (reservation["policy_version_ref"],),
        ).fetchone()
        if (
            policy_row is None
            or policy_row["policy_ref"] != revalidated.binding["rate_policy_ref"]
        ):
            raise RunnerConflict("reservation does not use the resolver-bound rate policy")
        if reservation["expires_at"] < revalidated.deadline_at:
            deadline_at = reservation["expires_at"]
        else:
            deadline_at = revalidated.deadline_at
        if revalidated.profile["auth_mode"] != "none":
            raise RunnerConflict(
                "authenticated connector profiles require a future credential authority"
            )
        raw_sink_ref = "raw-sink:" + content_hash(
            {
                "connector_invocation_ref": revalidated.invocation["id"],
                "reservation_ref": reservation["id"],
                "physical_attempt_number": reservation["physical_attempt_number"],
            }
        )
        wire = {
            "protocol_version": PROTOCOL_VERSION,
            "runner_request_ref": revalidated.request["id"],
            "runner_request_hash": revalidated.request["content_hash"],
            "connector_invocation_ref": revalidated.invocation["id"],
            "profile_ref": revalidated.profile["id"],
            "profile_hash": revalidated.profile["content_hash"],
            "call_spec_ref": revalidated.call_spec["id"],
            "call_spec_hash": revalidated.call_spec["content_hash"],
            "reservation_ref": reservation["id"],
            "reservation_hash": reservation["content_hash"],
            "physical_attempt_number": reservation["physical_attempt_number"],
            "source_identity": dict(revalidated.profile["source_identity"]),
            "source_hash": revalidated.profile["source_hash"],
            "adapter_ref": revalidated.profile["adapter_ref"],
            "adapter_hash": revalidated.profile["adapter_hash"],
            "resolver_ref": self.resolver.manifest["resolver_ref"],
            "resolver_manifest_hash": self.resolver.content_hash,
            "operation": revalidated.call_spec["operation"],
            "parameters": dict(revalidated.call_spec["parameters"]),
            "query_hash": revalidated.call_spec["query_hash"],
            "input_schema_ref": revalidated.profile["input_schema_refs"][revalidated.call_spec["operation"]],
            "input_schema_hash": revalidated.profile["input_schema_hashes"][revalidated.call_spec["operation"]],
            "output_schema_ref": revalidated.profile["output_schema_refs"][revalidated.call_spec["operation"]],
            "output_schema_hash": revalidated.profile["output_schema_hashes"][revalidated.call_spec["operation"]],
            "allowed_hosts": sorted(revalidated.profile["allowed_hosts"]),
            "network_policy": dict(revalidated.profile["network_policy"]),
            "credential_grant_ref": None,
            "deadline_at": deadline_at,
            "max_response_bytes": revalidated.profile["max_response_bytes"],
            "max_records": revalidated.profile["max_records"],
            "raw_sink_ref": raw_sink_ref,
        }
        wire["content_hash"] = content_hash(wire)
        return validate_connector_adapter_request(wire)

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
        if revalidated.profile["auth_mode"] != "none":
            raise RunnerConflict(
                "authenticated connector profiles require a future credential authority"
            )
        return self.connectors.reserve_quota_for_runner(
            revalidated.invocation["id"],
            revalidated.binding["rate_policy_ref"],
            revalidated.deadline_at,
            idempotency_key=idempotency_key,
        )

    def credential_handle_for_use(
        self, adapter_request: Mapping[str, Any]
    ) -> Any | None:
        """Return no credential for the credential-free public runner wire."""

        validate_connector_adapter_request(adapter_request)
        return None

    def validate_observation(
        self,
        value: Mapping[str, Any],
        adapter_request: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate the public/recorded observation against its exact request."""

        request = validate_connector_adapter_request(adapter_request)
        observation = validate_adapter_transport_observation(value)
        if observation["request_hash"] != request["content_hash"]:
            raise RunnerConflict(
                "adapter observation does not bind the exact AdapterRequest"
            )
        if len(observation["source_record_refs"]) > request["max_records"]:
            raise RunnerValidationError("adapter exceeded max_records")
        return observation


__all__ = [
    "AdapterNotResolved", "ConnectorRunnerAdmissionGate", "ConnectorRunnerError",
    "RunnerConflict", "RunnerValidationError", "StaticAdapterResolver",
    "ValidatedRunnerAdmission", "validate_adapter_transport_observation",
    "validate_connector_adapter_request", "validate_connector_runner_request",
    "validate_connector_runner_response", "validate_runner_environment_manifest",
]
