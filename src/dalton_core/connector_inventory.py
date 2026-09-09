"""Closed, offline-only inventory for the first ten connector profiles.

Inventory membership is not execution authority.  These templates and their
synthetic fixture manifests intentionally cannot produce a CapabilityLease or
resolve a Runner binding.
"""

from __future__ import annotations

import ipaddress
import copy
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .connector import ConnectorError, validate_connector_proposal_manifest
from .store import canonical_json, content_hash


CREATED_AT = "2026-08-14T20:00:00.000000+00:00"
SCHEMA_VERSION = "0.1"
INVENTORY_DIR = Path(__file__).with_name("connector_inventory")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_COMPLETENESS = frozenset({"enumerated", "ranked", "partial", "unknown"})
_TRANSPORT_KINDS = frozenset({"public_https", "host_tool", "mcp_managed"})
_COMMON_SCENARIOS = frozenset(
    {"success", "empty", "partial", "schema_drift", "rate_limited", "timeout", "malformed"}
)
_AUTH_SCENARIOS = frozenset({"permission_denied", "revoked"})
_FORBIDDEN_SERIALIZED_MARKERS = (
    "xq_a_token", "refresh_token", "access_token", "authorization:",
    "cookie:", "oauth_config", "server_config", "127.0.0.1:", "/users/",
)
_UNSAFE_COMPACT_MARKERS = (
    "systemprompt", "promptbody", "instructionbody", "apikey",
    "serverconfig", "oauthconfig", "xqatoken", "refreshtoken",
    "accesstoken", "clientsecret", "secretmaterial",
)
_INVENTORY_REF_RE = re.compile(
    r"[a-z][a-z0-9-]*(?::[a-z0-9][a-z0-9._-]*)+", re.ASCII
)
_PROPOSAL_PACKAGE_FILES = frozenset({"profile.json", "fixture.json", "proposal.json"})
_MAX_PROPOSAL_PACKAGE_FILE_BYTES = 1_000_000
_SCHEMA_TYPES = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)
_PROPOSAL_REQUIRED_GATES = {
    ("public_https", "none"): frozenset(
        {"killable_total_deadline_public_transport", "recorded_public_reference_shadow"}
    ),
    ("host_tool", "none"): frozenset({"host_tool_runner_v0.2"}),
    ("host_tool", "host_owned"): frozenset(
        {"host_tool_runner_v0.2_and_credential_authority"}
    ),
    ("mcp_managed", "host_owned"): frozenset(
        {"mcp_managed_runner_v0.2_and_credential_authority"}
    ),
}


class ConnectorInventoryError(ValueError):
    pass


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConnectorInventoryError(f"{name} must be an object")
    unknown = set(value) - fields
    missing = fields - set(value)
    if unknown or missing:
        raise ConnectorInventoryError(
            f"{name} has invalid closed shape; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    return json.loads(canonical_json(value))


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConnectorInventoryError(f"{name} must be a non-empty string")
    return value.strip()


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if not _HASH_RE.fullmatch(value):
        raise ConnectorInventoryError(f"{name} must be lowercase SHA-256 hex")
    return value


def _canonical_timestamp(value: Any, name: str) -> str:
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConnectorInventoryError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ConnectorInventoryError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _inventory_ref(value: Any, name: str) -> str:
    ref = _text(value, name)
    if _INVENTORY_REF_RE.fullmatch(ref) is None:
        raise ConnectorInventoryError(f"{name} is not a canonical inventory ref")
    return ref


def _unique_texts(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ConnectorInventoryError(f"{name} must be an array")
    items = [_text(item, f"{name}[]") for item in value]
    if nonempty and not items:
        raise ConnectorInventoryError(f"{name} must not be empty")
    if len(items) != len(set(items)):
        raise ConnectorInventoryError(f"{name} must be unique")
    return items


def _public_host(value: Any, name: str) -> str:
    host = _text(value, name).lower().rstrip(".")
    if "/" in host or "://" in host or host == "localhost" or host.endswith(".local"):
        raise ConnectorInventoryError(f"{name} must be a public hostname")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if not address.is_global:
        raise ConnectorInventoryError(f"{name} must not be a private IP")
    return host


def _without_hash(wire: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in wire.items() if key != "content_hash"}


def _with_hash(wire: Mapping[str, Any]) -> dict[str, Any]:
    result = json.loads(canonical_json(wire))
    result["content_hash"] = content_hash(result)
    return result


def _validate_content_hash(wire: Mapping[str, Any], name: str) -> None:
    declared = _hash(wire["content_hash"], f"{name}.content_hash")
    if declared != content_hash(_without_hash(wire)):
        raise ConnectorInventoryError(f"{name}.content_hash mismatch")


def _assert_no_sensitive_material(wire: Mapping[str, Any], name: str) -> None:
    serialized = canonical_json(wire).lower()
    for marker in _FORBIDDEN_SERIALIZED_MARKERS:
        if marker in serialized:
            raise ConnectorInventoryError(f"{name} contains forbidden material: {marker}")
    compact = re.sub(r"[^a-z0-9]+", "", serialized)
    if "/" in serialized or "%" in serialized or "\\\\" in serialized or any(
        marker in compact for marker in _UNSAFE_COMPACT_MARKERS
    ):
        raise ConnectorInventoryError(
            f"{name} contains path, prompt, config, or credential material"
        )


def _schema_types(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        types = (_text(value, name),)
    elif isinstance(value, list):
        types = tuple(_unique_texts(value, name, nonempty=True))
        if len(types) != 2 or "null" not in types:
            raise ConnectorInventoryError(
                f"{name} unions are limited to one type plus null"
            )
    else:
        raise ConnectorInventoryError(f"{name} must be a JSON Schema type or nullable pair")
    if not set(types).issubset(_SCHEMA_TYPES):
        raise ConnectorInventoryError(f"{name} contains an unsupported JSON Schema type")
    return types


def _schema_integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConnectorInventoryError(f"{name} must be an integer >= {minimum}")
    return value


def _schema_number(value: Any, name: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConnectorInventoryError(f"{name} must be a number")
    return value


def _matches_schema_type(value: Any, schema_type: str) -> bool:
    if schema_type == "null":
        return value is None
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "string":
        return isinstance(value, str)
    return False


def _validate_schema_node(value: Any, name: str, *, depth: int = 0) -> dict[str, Any]:
    if depth > 32:
        raise ConnectorInventoryError(f"{name} exceeds the schema nesting limit")
    if not isinstance(value, Mapping):
        raise ConnectorInventoryError(f"{name} must be a JSON Schema object")
    node = json.loads(canonical_json(value))
    if "type" not in node:
        raise ConnectorInventoryError(f"{name}.type is required")
    types = _schema_types(node["type"], f"{name}.type")
    non_null = tuple(item for item in types if item != "null")
    base_type = non_null[0] if non_null else "null"
    fields = {"type", "enum"}
    if base_type == "object":
        fields.update({"additionalProperties", "properties", "required"})
    elif base_type == "array":
        fields.update({"items", "maxItems", "minItems", "uniqueItems"})
    elif base_type == "string":
        fields.update({"maxLength", "minLength", "pattern"})
    elif base_type in {"integer", "number"}:
        fields.update({"maximum", "minimum"})
    unknown = set(node) - fields
    if unknown:
        raise ConnectorInventoryError(
            f"{name} contains unsupported JSON Schema keywords: {sorted(unknown)}"
        )

    if "enum" in node:
        enum = node["enum"]
        if not isinstance(enum, list) or not enum:
            raise ConnectorInventoryError(f"{name}.enum must be a non-empty array")
        encoded = [canonical_json(item) for item in enum]
        if len(encoded) != len(set(encoded)):
            raise ConnectorInventoryError(f"{name}.enum must contain unique values")
        if base_type in {"array", "object"} or any(
            not any(_matches_schema_type(item, schema_type) for schema_type in types)
            for item in enum
        ):
            raise ConnectorInventoryError(f"{name}.enum values do not match its type")

    if base_type == "object":
        if node.get("additionalProperties") is not False:
            raise ConnectorInventoryError(f"{name} object schemas must be closed")
        if not isinstance(node.get("properties"), Mapping):
            raise ConnectorInventoryError(f"{name}.properties must be an object")
        required = _unique_texts(node.get("required", []), f"{name}.required")
        if not set(required).issubset(node["properties"]):
            raise ConnectorInventoryError(f"{name}.required is not covered by properties")
        if "required" in node:
            node["required"] = required
        node["properties"] = {
            _text(property_name, f"{name}.properties key"): _validate_schema_node(
                child, f"{name}.properties.{property_name}", depth=depth + 1
            )
            for property_name, child in node["properties"].items()
        }
    elif base_type == "array":
        if "items" not in node:
            raise ConnectorInventoryError(f"{name}.items is required for array schemas")
        node["items"] = _validate_schema_node(
            node["items"], f"{name}.items", depth=depth + 1
        )
        if "uniqueItems" in node and not isinstance(node["uniqueItems"], bool):
            raise ConnectorInventoryError(f"{name}.uniqueItems must be boolean")
        for field in ("minItems", "maxItems"):
            if field in node:
                node[field] = _schema_integer(node[field], f"{name}.{field}")
        if node.get("minItems", 0) > node.get("maxItems", node.get("minItems", 0)):
            raise ConnectorInventoryError(f"{name} has an invalid item-count interval")
    elif base_type == "string":
        for field in ("minLength", "maxLength"):
            if field in node:
                node[field] = _schema_integer(node[field], f"{name}.{field}")
        if node.get("minLength", 0) > node.get("maxLength", node.get("minLength", 0)):
            raise ConnectorInventoryError(f"{name} has an invalid string-length interval")
        if "pattern" in node:
            pattern = _text(node["pattern"], f"{name}.pattern")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConnectorInventoryError(f"{name}.pattern is invalid") from exc
            node["pattern"] = pattern
    elif base_type in {"integer", "number"}:
        for field in ("minimum", "maximum"):
            if field in node:
                node[field] = _schema_number(node[field], f"{name}.{field}")
        if (
            "minimum" in node
            and "maximum" in node
            and node["minimum"] > node["maximum"]
        ):
            raise ConnectorInventoryError(f"{name} has an invalid numeric interval")
    return node


def _validate_schema_document(value: Any, name: str) -> dict[str, Any]:
    document = _validate_schema_node(value, name)
    if document["type"] != "object":
        raise ConnectorInventoryError("operation schemas must be closed objects")
    return document


def _validate_connector_fixture_manifest(
    spec: Mapping[str, Any], *, frozen: bool
) -> dict[str, Any]:
    fields = {
        "schema_version", "id", "created_at", "connector_template_ref",
        "recording_boundary", "authenticated", "synthetic", "operations", "cases",
        "content_hash",
    }
    wire = _closed(spec, fields, "ConnectorFixtureManifest")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ConnectorInventoryError("unsupported ConnectorFixtureManifest schema_version")
    for name in ("id", "connector_template_ref"):
        wire[name] = _inventory_ref(wire[name], name)
    wire["created_at"] = _canonical_timestamp(wire["created_at"], "created_at")
    if frozen and wire["created_at"] != CREATED_AT:
        raise ConnectorInventoryError("fixture created_at differs from the frozen build")
    match = re.fullmatch(
        r"connector-profile-template:([a-z0-9][a-z0-9-]*):0\.1",
        wire["connector_template_ref"],
    )
    if match is None:
        raise ConnectorInventoryError("fixture template ref is not canonical")
    fixture_slug = match.group(1)
    if wire["id"] != f"connector-fixture-manifest:{fixture_slug}:0.1":
        raise ConnectorInventoryError("fixture id does not bind its template")
    if wire["recording_boundary"] not in {"public_provider", "host_gateway"}:
        raise ConnectorInventoryError("recording_boundary is invalid")
    if not isinstance(wire["authenticated"], bool):
        raise ConnectorInventoryError("authenticated must be boolean")
    if wire["recording_boundary"] == "public_provider" and wire["authenticated"]:
        raise ConnectorInventoryError("P1-0 public provider fixtures are credential-free")
    if wire["synthetic"] is not True:
        raise ConnectorInventoryError("inventory fixtures must be synthetic")
    if not isinstance(wire["operations"], list) or not wire["operations"]:
        raise ConnectorInventoryError("fixture operation matrix must be non-empty")
    operation_modes: dict[str, str] = {}
    normalized_operations: list[dict[str, str]] = []
    for index, item in enumerate(wire["operations"]):
        operation = _closed(item, {"operation", "pagination_mode"}, f"operations[{index}]")
        name = _text(operation["operation"], f"operations[{index}].operation")
        mode = _text(operation["pagination_mode"], f"operations[{index}].pagination_mode")
        if name in operation_modes:
            raise ConnectorInventoryError("fixture operation names must be unique")
        if mode not in {"none", "cursor", "page"}:
            raise ConnectorInventoryError("fixture pagination mode is invalid")
        operation_modes[name] = mode
        normalized_operations.append({"operation": name, "pagination_mode": mode})
    wire["operations"] = normalized_operations
    if not isinstance(wire["cases"], list) or len(wire["cases"]) < 7:
        raise ConnectorInventoryError("fixture matrix must contain at least seven cases")
    case_fields = {
        "case_ref", "scenario", "operation", "outcome", "provider_status",
        "source_status", "completeness", "page", "next_cursor",
        "source_record_refs", "raw_payload_hash", "error_code",
    }
    cases: list[dict[str, Any]] = []
    case_refs: set[str] = set()
    scenarios_by_operation: dict[str, set[str]] = {
        operation: set() for operation in operation_modes
    }
    for index, item in enumerate(wire["cases"]):
        case = _closed(item, case_fields, f"cases[{index}]")
        for name in ("case_ref", "scenario", "operation", "outcome"):
            case[name] = _text(case[name], f"cases[{index}].{name}")
        if case["scenario"] not in _COMMON_SCENARIOS | _AUTH_SCENARIOS | {"pagination"}:
            raise ConnectorInventoryError("fixture scenario is invalid")
        if case["case_ref"] in case_refs:
            raise ConnectorInventoryError("fixture case_ref must be unique")
        case_refs.add(case["case_ref"])
        if case["operation"] not in operation_modes:
            raise ConnectorInventoryError("fixture case names an undeclared operation")
        if case["case_ref"] != (
            f"fixture:{fixture_slug}:{case['operation']}:{case['scenario']}:0.1"
        ):
            raise ConnectorInventoryError("fixture case_ref is not authority-derived")
        scenarios_by_operation[case["operation"]].add(case["scenario"])
        if case["outcome"] not in {"succeeded", "rate_limited", "timeout", "failed"}:
            raise ConnectorInventoryError("fixture outcome is invalid")
        status = case["provider_status"]
        if status is not None and (
            isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599
        ):
            raise ConnectorInventoryError("fixture provider_status must be HTTP-like or null")
        if case["outcome"] == "succeeded" and (
            status is None or not 200 <= status < 300
        ):
            raise ConnectorInventoryError("successful fixture requires 2xx provider status")
        if case["outcome"] == "rate_limited" and status != 429:
            raise ConnectorInventoryError("rate-limited fixture requires 429")
        if case["outcome"] == "timeout" and status is not None:
            raise ConnectorInventoryError("timeout fixture cannot claim provider status")
        expected_status_code = {"permission_denied": 403, "revoked": 401}.get(
            case["scenario"]
        )
        if expected_status_code is not None and status != expected_status_code:
            raise ConnectorInventoryError("auth failure fixture has the wrong status")
        if case["completeness"] is not None and (
            not isinstance(case["completeness"], str)
            or case["completeness"] not in _COMPLETENESS
        ):
            raise ConnectorInventoryError("fixture completeness is invalid")
        if case["source_status"] is not None and (
            not isinstance(case["source_status"], str)
            or case["source_status"] not in {"complete", "partial", "empty", "error"}
        ):
            raise ConnectorInventoryError("fixture source_status is invalid")
        page_value = case["page"]
        if page_value is not None and (
            isinstance(page_value, bool) or not isinstance(page_value, int) or page_value < 1
        ):
            raise ConnectorInventoryError("fixture page must be positive or null")
        if case["next_cursor"] is not None:
            case["next_cursor"] = _text(case["next_cursor"], "next_cursor")
        if case["error_code"] is not None:
            case["error_code"] = _text(case["error_code"], "error_code")
        case["source_record_refs"] = _unique_texts(
            case["source_record_refs"], f"cases[{index}].source_record_refs"
        )
        expected_refs = (
            [f"record:{fixture_slug}:synthetic:1"]
            if case["scenario"] in {"success", "pagination", "partial"}
            else []
        )
        if case["source_record_refs"] != expected_refs:
            raise ConnectorInventoryError(
                "fixture source_record_refs are not deterministic synthetic refs"
            )
        if case["raw_payload_hash"] is not None:
            case["raw_payload_hash"] = _hash(
                case["raw_payload_hash"], f"cases[{index}].raw_payload_hash"
            )
        if case["scenario"] == "pagination":
            if operation_modes[case["operation"]] == "none":
                raise ConnectorInventoryError("non-paginated operation cannot have pagination fixture")
            if case["page"] is None or case["next_cursor"] is None:
                raise ConnectorInventoryError("pagination fixture requires page and next cursor")
        elif case["page"] is not None or case["next_cursor"] is not None:
            raise ConnectorInventoryError("non-pagination fixture cannot carry page state")
        scenario = case["scenario"]
        expected: dict[str, tuple[Any, ...]] = {
            "success": ("succeeded", "complete"),
            "empty": ("succeeded", "empty"),
            "pagination": ("succeeded", "partial"),
            "partial": ("succeeded", "partial"),
            "schema_drift": ("failed", None),
            "rate_limited": ("rate_limited", None),
            "timeout": ("timeout", None),
            "malformed": ("failed", None),
            "permission_denied": ("failed", None),
            "revoked": ("failed", None),
        }
        expected_outcome, expected_status = expected[scenario]
        if case["outcome"] != expected_outcome or case["source_status"] != expected_status:
            raise ConnectorInventoryError("fixture scenario outcome/status is inconsistent")
        succeeded = expected_outcome == "succeeded"
        if succeeded != (case["raw_payload_hash"] is not None):
            raise ConnectorInventoryError("fixture raw payload presence is inconsistent")
        if succeeded != (case["completeness"] is not None):
            raise ConnectorInventoryError("fixture completeness presence is inconsistent")
        if succeeded == (case["error_code"] is not None):
            raise ConnectorInventoryError("fixture error presence is inconsistent")
        if not succeeded and case["error_code"] != scenario:
            raise ConnectorInventoryError("fixture error_code must exactly bind scenario")
        if succeeded:
            expected_raw_hash = content_hash(
                {
                    "synthetic": True, "connector": fixture_slug,
                    "operation": case["operation"], "scenario": scenario,
                }
            )
            if case["raw_payload_hash"] != expected_raw_hash:
                raise ConnectorInventoryError(
                    "synthetic fixture raw hash is not reproducible"
                )
        if scenario == "success" and not case["source_record_refs"]:
            raise ConnectorInventoryError("success fixture requires records")
        if scenario == "empty" and case["source_record_refs"]:
            raise ConnectorInventoryError("empty fixture cannot claim records")
        if scenario in {"pagination", "partial"} and case["completeness"] != "partial":
            raise ConnectorInventoryError("incomplete fixture must declare partial completeness")
        if not succeeded and case["source_record_refs"]:
            raise ConnectorInventoryError("failed fixture cannot claim source records")
        cases.append(case)
    for operation, mode in operation_modes.items():
        scenarios = scenarios_by_operation[operation]
        required = set(_COMMON_SCENARIOS)
        if mode != "none":
            required.add("pagination")
        if not required.issubset(scenarios):
            raise ConnectorInventoryError(
                f"fixture matrix lacks required scenarios for {operation}"
            )
        if wire["authenticated"] and not _AUTH_SCENARIOS.issubset(scenarios):
            raise ConnectorInventoryError(
                f"authenticated fixtures require deny and revoke for {operation}"
            )
        if not wire["authenticated"] and scenarios & _AUTH_SCENARIOS:
            raise ConnectorInventoryError(
                f"credential-free fixture cannot contain auth scenarios for {operation}"
            )
    wire["cases"] = cases
    _validate_content_hash(wire, "ConnectorFixtureManifest")
    _assert_no_sensitive_material(wire, "ConnectorFixtureManifest")
    return wire


def validate_connector_fixture_manifest(spec: Mapping[str, Any]) -> dict[str, Any]:
    return _validate_connector_fixture_manifest(spec, frozen=True)


def _validate_connector_profile_template(
    spec: Mapping[str, Any], *, frozen: bool
) -> dict[str, Any]:
    fields = {
        "schema_version", "id", "created_at", "connector_ref", "source_identity",
        "transport", "auth_boundary", "route_restrictions", "schema_documents",
        "operations", "fixture_manifest_ref", "fixture_manifest_hash", "readiness",
        "content_hash",
    }
    wire = _closed(spec, fields, "ConnectorProfileTemplate")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ConnectorInventoryError("unsupported ConnectorProfileTemplate schema_version")
    for name in ("id", "connector_ref", "fixture_manifest_ref"):
        wire[name] = _inventory_ref(wire[name], name)
    wire["created_at"] = _canonical_timestamp(wire["created_at"], "created_at")
    if frozen and wire["created_at"] != CREATED_AT:
        raise ConnectorInventoryError("profile created_at differs from the frozen build")
    wire["fixture_manifest_hash"] = _hash(
        wire["fixture_manifest_hash"], "fixture_manifest_hash"
    )
    identity = _closed(
        wire["source_identity"], {"source_ref", "source_type", "source_version"},
        "source_identity",
    )
    for name in identity:
        identity[name] = _text(identity[name], f"source_identity.{name}")
    if identity["source_type"] not in {
        "official_filing", "authenticated_library", "social_enumeration",
        "social_search", "public_web", "market_data",
    }:
        raise ConnectorInventoryError("source type is invalid")
    wire["source_identity"] = identity

    transport = _closed(
        wire["transport"],
        {"kind", "target_ref", "target_hash", "host_policy", "allowed_hosts"},
        "transport",
    )
    if transport["kind"] not in _TRANSPORT_KINDS:
        raise ConnectorInventoryError("transport kind is invalid")
    transport["target_ref"] = _text(transport["target_ref"], "transport.target_ref")
    transport["target_hash"] = _hash(transport["target_hash"], "transport.target_hash")
    if transport["host_policy"] not in {
        "literal_allowlist", "per_call_authority", "not_applicable"
    }:
        raise ConnectorInventoryError("transport host_policy is invalid")
    hosts = _unique_texts(transport["allowed_hosts"], "transport.allowed_hosts")
    if transport["kind"] == "public_https":
        if transport["host_policy"] == "literal_allowlist" and not hosts:
            raise ConnectorInventoryError("literal public transport requires allowed hosts")
        if transport["host_policy"] == "per_call_authority" and hosts:
            raise ConnectorInventoryError("per-call host authority cannot freeze a placeholder host")
        if transport["host_policy"] == "not_applicable":
            raise ConnectorInventoryError("public transport requires a host authority policy")
        hosts = [_public_host(host, "transport.allowed_hosts[]") for host in hosts]
    elif hosts or transport["host_policy"] != "not_applicable":
        raise ConnectorInventoryError(
            "host_tool and mcp_managed cannot declare HTTP host authority"
        )
    transport["allowed_hosts"] = hosts
    expected_target_hash = content_hash(
        {
            "kind": transport["kind"], "target_ref": transport["target_ref"],
            "host_policy": transport["host_policy"], "allowed_hosts": hosts,
        }
    )
    if transport["target_hash"] != expected_target_hash:
        raise ConnectorInventoryError("transport target hash does not bind host authority")
    wire["transport"] = transport

    auth = _closed(
        wire["auth_boundary"],
        {"mode", "owner", "credential_material", "use_time_authority"},
        "auth_boundary",
    )
    none_auth = {
        "mode": "none", "owner": "none", "credential_material": "forbidden",
        "use_time_authority": "none",
    }
    host_auth = {
        "mode": "host_owned", "owner": "host", "credential_material": "forbidden",
        "use_time_authority": "required_future",
    }
    if auth not in (none_auth, host_auth):
        raise ConnectorInventoryError("auth boundary is inconsistent")
    if transport["kind"] == "public_https" and auth != none_auth:
        raise ConnectorInventoryError("P1-0 public_https inventory must be credential-free")
    wire["auth_boundary"] = auth

    routes = _closed(
        wire["route_restrictions"],
        {"allowed_target_refs", "forbidden_target_refs", "fallback_routes", "provenance_label_required"},
        "route_restrictions",
    )
    for name in ("allowed_target_refs", "forbidden_target_refs"):
        routes[name] = _unique_texts(
            routes[name], f"route_restrictions.{name}", nonempty=name == "allowed_target_refs"
        )
    if routes["allowed_target_refs"] != [transport["target_ref"]]:
        raise ConnectorInventoryError("allowed target must bind the exact transport target")
    if routes["provenance_label_required"] is not True:
        raise ConnectorInventoryError("inventory routes always require explicit provenance labels")
    if not isinstance(routes["fallback_routes"], list):
        raise ConnectorInventoryError("fallback_routes must be an array")
    fallback_fields = {
        "operation", "target_ref", "source_ref", "adapter_ref", "provenance_label"
    }
    normalized_fallbacks: list[dict[str, str]] = []
    fallback_keys: set[tuple[str, str]] = set()
    for index, item in enumerate(routes["fallback_routes"]):
        fallback = _closed(item, fallback_fields, f"fallback_routes[{index}]")
        for name in fallback_fields:
            fallback[name] = _text(fallback[name], f"fallback_routes[{index}].{name}")
        key = (fallback["operation"], fallback["target_ref"])
        if key in fallback_keys:
            raise ConnectorInventoryError("fallback routes must be unique per operation and target")
        fallback_keys.add(key)
        normalized_fallbacks.append(fallback)
    routes["fallback_routes"] = normalized_fallbacks
    wire["route_restrictions"] = routes

    if not isinstance(wire["schema_documents"], list) or not wire["schema_documents"]:
        raise ConnectorInventoryError("schema_documents must be non-empty")
    documents: dict[str, dict[str, Any]] = {}
    normalized_documents: list[dict[str, Any]] = []
    for index, item in enumerate(wire["schema_documents"]):
        document = _closed(
            item, {"schema_ref", "schema_hash", "document"},
            f"schema_documents[{index}]",
        )
        schema_ref = _text(document["schema_ref"], "schema_ref")
        if schema_ref in documents:
            raise ConnectorInventoryError("schema_ref must be unique")
        schema_hash = _hash(document["schema_hash"], "schema_hash")
        schema = _validate_schema_document(
            document["document"], f"schema_documents[{index}].document"
        )
        if schema_hash != content_hash(schema):
            raise ConnectorInventoryError("schema_hash does not bind schema document")
        document = {"schema_ref": schema_ref, "schema_hash": schema_hash, "document": schema}
        documents[schema_ref] = document
        normalized_documents.append(document)
    wire["schema_documents"] = normalized_documents

    if not isinstance(wire["operations"], list) or not wire["operations"]:
        raise ConnectorInventoryError("operations must be non-empty")
    operation_fields = {
        "operation", "source_method", "input_schema_ref", "input_schema_hash",
        "output_schema_ref", "output_schema_hash", "completeness_ceiling",
        "pagination", "side_effects",
    }
    operations: list[dict[str, Any]] = []
    operation_names: set[str] = set()
    for index, item in enumerate(wire["operations"]):
        operation = _closed(item, operation_fields, f"operations[{index}]")
        for name in ("operation", "source_method", "input_schema_ref", "output_schema_ref"):
            operation[name] = _text(operation[name], f"operations[{index}].{name}")
        if operation["operation"] in operation_names:
            raise ConnectorInventoryError("operation names must be unique")
        operation_names.add(operation["operation"])
        for ref_name, hash_name in (
            ("input_schema_ref", "input_schema_hash"),
            ("output_schema_ref", "output_schema_hash"),
        ):
            operation[hash_name] = _hash(operation[hash_name], hash_name)
            document = documents.get(operation[ref_name])
            if document is None or document["schema_hash"] != operation[hash_name]:
                raise ConnectorInventoryError("operation schema binding is not exact")
        if operation["completeness_ceiling"] not in _COMPLETENESS:
            raise ConnectorInventoryError("completeness ceiling is invalid")
        pagination = _closed(
            operation["pagination"],
            {"mode", "cursor_field", "bounded_window_required", "max_pages"},
            f"operations[{index}].pagination",
        )
        if pagination["mode"] not in {"none", "cursor", "page"}:
            raise ConnectorInventoryError("pagination mode is invalid")
        if pagination["mode"] == "none" and pagination["cursor_field"] is not None:
            raise ConnectorInventoryError("non-paginated operation cannot name a cursor")
        if pagination["mode"] != "none":
            pagination["cursor_field"] = _text(
                pagination["cursor_field"], "pagination.cursor_field"
            )
        if isinstance(pagination["max_pages"], bool) or not isinstance(pagination["max_pages"], int) or pagination["max_pages"] < 1:
            raise ConnectorInventoryError("pagination.max_pages must be positive")
        if not isinstance(pagination["bounded_window_required"], bool):
            raise ConnectorInventoryError("bounded_window_required must be boolean")
        if (
            operation["completeness_ceiling"] == "enumerated"
            and pagination["mode"] != "none"
            and pagination["bounded_window_required"] is not True
        ):
            raise ConnectorInventoryError(
                "paginated enumerated operation requires a bounded window"
            )
        operation["pagination"] = pagination
        operation["side_effects"] = _unique_texts(operation["side_effects"], "side_effects")
        operations.append(operation)
    wire["operations"] = operations
    for fallback in routes["fallback_routes"]:
        if fallback["operation"] not in operation_names:
            raise ConnectorInventoryError("fallback route names an undeclared operation")
    if transport["kind"] == "mcp_managed" and auth != host_auth:
        raise ConnectorInventoryError("mcp_managed profiles require host-owned auth")
    if frozen and transport["kind"] == "host_tool":
        # S1: which host routes need no credential is a declaration, not one
        # named exception. It was Reddit alone until two feeds arrived that
        # read bytes a host skill had already written to this disk -- the
        # credential was spent before Dalton saw them, so there is nothing for
        # a credential authority to hold. Reading the answer out of the frozen
        # definitions keeps it in one place; a packaged profile whose auth
        # disagrees with its own definition is still refused here.
        keyless_targets = {
            definition["target"] for definition in PROFILE_DEFINITIONS
            if definition["transport"] == "host_tool" and definition["auth"] == "none"
        }
        if (transport["target_ref"] in keyless_targets) != (auth == none_auth):
            raise ConnectorInventoryError(
                "host route auth must match its frozen keyless declaration"
            )

    readiness = _closed(
        wire["readiness"],
        {"level", "lease_eligible", "live_execution_allowed", "required_gate"},
        "readiness",
    )
    if readiness != {
        "level": "inventory_connected", "lease_eligible": False,
        "live_execution_allowed": False, "required_gate": readiness["required_gate"],
    }:
        raise ConnectorInventoryError("inventory readiness cannot grant execution")
    readiness["required_gate"] = _text(readiness["required_gate"], "required_gate")
    wire["readiness"] = readiness
    if frozen:
        _validate_frozen_profile_contract(wire)
    _validate_content_hash(wire, "ConnectorProfileTemplate")
    _assert_no_sensitive_material(wire, "ConnectorProfileTemplate")
    return wire


def validate_connector_profile_template(spec: Mapping[str, Any]) -> dict[str, Any]:
    return _validate_connector_profile_template(spec, frozen=True)


def validate_connector_inventory_index(spec: Mapping[str, Any]) -> dict[str, Any]:
    wire = _closed(
        spec, {"schema_version", "id", "created_at", "profiles", "content_hash"},
        "ConnectorInventoryIndex",
    )
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ConnectorInventoryError("unsupported ConnectorInventoryIndex schema_version")
    wire["id"] = _inventory_ref(wire["id"], "id")
    if wire["id"] != "connector-inventory:p1-0:0.1":
        raise ConnectorInventoryError("inventory index id is not frozen")
    if wire["created_at"] != CREATED_AT:
        raise ConnectorInventoryError("index created_at differs from the frozen build")
    # P13ag: the count is the number of profiles this build defines, not a
    # literal repeated in three places. Adding a connector used to mean
    # editing a "10" here, a "10" in the file-set check and a "10" in the
    # completeness check, and forgetting one of them fails somewhere far from
    # the change -- the same shape as the identity bugs this codebase keeps
    # finding.
    if not isinstance(wire["profiles"], list) or len(wire["profiles"]) != len(PROFILE_DEFINITIONS):
        raise ConnectorInventoryError(
            "connector inventory profile count differs from the frozen build"
        )
    entry_fields = {
        "connector_ref", "profile_template_ref", "profile_template_hash",
        "fixture_manifest_ref", "fixture_manifest_hash", "proposal_manifest_ref",
        "proposal_manifest_hash",
    }
    entries: list[dict[str, Any]] = []
    connector_refs: set[str] = set()
    for index, item in enumerate(wire["profiles"]):
        entry = _closed(item, entry_fields, f"profiles[{index}]")
        for name in ("connector_ref", "profile_template_ref", "fixture_manifest_ref", "proposal_manifest_ref"):
            entry[name] = _inventory_ref(entry[name], name)
        for name in ("profile_template_hash", "fixture_manifest_hash", "proposal_manifest_hash"):
            entry[name] = _hash(entry[name], name)
        if entry["connector_ref"] in connector_refs:
            raise ConnectorInventoryError("connector_ref must be unique")
        connector_refs.add(entry["connector_ref"])
        entries.append(entry)
    if connector_refs != _REQUIRED_CONNECTOR_REFS:
        raise ConnectorInventoryError(
            "connector inventory does not match the frozen ten-profile allowlist"
        )
    wire["profiles"] = entries
    _validate_content_hash(wire, "ConnectorInventoryIndex")
    _assert_no_sensitive_material(wire, "ConnectorInventoryIndex")
    return wire


def _object_schema(properties: Mapping[str, Any], required: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "required": list(required), "properties": dict(properties),
    }


def _string() -> dict[str, Any]:
    return {"type": "string", "minLength": 1}


def _integer(minimum: int = 0) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum}


def _array_of_strings() -> dict[str, Any]:
    return {"type": "array", "uniqueItems": True, "items": _string()}


def _output_schema(slug: str, operation: str) -> dict[str, Any]:
    if (slug, operation) in {
        ("cninfo", "list_announcements"),
        ("sec", "list_filings"),
    }:
        normalized_record = _object_schema(
            {
                "record_ref": _string(),
                "revision_of_ref": {"type": ["string", "null"]},
                "record_hash": {
                    "type": "string", "pattern": "^[0-9a-f]{64}$",
                },
            },
            ("record_ref", "revision_of_ref", "record_hash"),
        )
        return _object_schema(
            {
                "records": {"type": "array", "items": normalized_record},
                "source_record_refs": _array_of_strings(),
                "request_cursor": {"type": ["string", "null"]},
                "next_cursor": {"type": ["string", "null"]},
                "page_ordinal": _integer(1),
                "provider_status": _integer(100),
            },
            (
                "records", "source_record_refs", "request_cursor",
                "next_cursor", "page_ordinal", "provider_status",
            ),
        )
    if (slug, operation) == ("sec", "get_company_facts"):
        decimal = {"type": "string", "pattern": "^-?(0|[1-9][0-9]*)([.][0-9]+)?$"}
        sha256 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
        fact = _object_schema(
            {
                "accession": {
                    "type": "string",
                    "pattern": "^[0-9]{10}-[0-9]{2}-[0-9]{6}$",
                },
                "start": _string(),
                "end": _string(),
                "filed": _string(),
                "fy": {"type": "integer", "minimum": 1900, "maximum": 2200},
                "fp": _string(),
                "form": {"type": "string", "enum": ["10-Q", "10-K"]},
                # P13z: SEC assigns a calendar frame to only the newest filing
                # that reports a period, so when a later 10-Q repeats the
                # prior-year quarter the frame moves and the original row loses
                # it. The adapter was widened to keep such rows -- requiring a
                # frame made every historical filing unusable -- but this
                # contract was not, so the adapter emitted a null the resolver
                # then refused: "output.current.frame does not match schema
                # type", on live Accenture data, on the first run that got far
                # enough to try. Still required, because the key is always
                # present; it is the value that may be absent.
                "frame": {"type": ["string", "null"],
                          "pattern": "^CY[0-9]{4}Q[1-4]$"},
                "value": decimal,
                "record_hash": sha256,
            },
            (
                "accession", "start", "end", "filed", "fy", "fp", "form",
                "frame", "value", "record_hash",
            ),
        )
        return _object_schema(
            {
                "schema_version": {"type": "string", "enum": ["0.1"]},
                "entity_name": _string(),
                "cik": {"type": "string", "pattern": "^[0-9]{10}$"},
                "taxonomy": {"type": "string", "enum": ["us-gaap"]},
                "concept_candidates": {
                    "type": "array", "minItems": 1, "maxItems": 8,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "pattern": "^[A-Za-z][A-Za-z0-9]{0,127}$",
                    },
                },
                "eligible_concepts": {
                    "type": "array", "minItems": 1, "maxItems": 8,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "pattern": "^[A-Za-z][A-Za-z0-9]{0,127}$",
                    },
                },
                "concept": {
                    "type": "string", "pattern": "^[A-Za-z][A-Za-z0-9]{0,127}$",
                },
                "label": _string(),
                "unit": {"type": "string", "enum": ["USD"]},
                "form": {"type": "string", "enum": ["10-Q", "10-K"]},
                "filed_from": _string(),
                "filed_to": _string(),
                "latest_accession": {
                    "type": "string",
                    "pattern": "^[0-9]{10}-[0-9]{2}-[0-9]{6}$",
                },
                "selection_basis": {
                    "type": "string",
                    "enum": [
                        "ordered_allowlist_latest_10-Q",
                        "ordered_allowlist_latest_10-K",
                    ],
                },
                "current": fact,
                "prior": fact,
                "growth_percent": decimal,
                "source_record_refs": {
                    "type": "array", "minItems": 2, "maxItems": 2,
                    "uniqueItems": True, "items": _string(),
                },
                "next_cursor": {"type": ["string", "null"]},
                "provider_status": _integer(100),
                "content_hash": sha256,
            },
            (
                "schema_version", "entity_name", "cik", "taxonomy", "concept",
                "concept_candidates", "eligible_concepts", "label", "unit", "form", "filed_from",
                "filed_to", "latest_accession", "selection_basis", "current", "prior",
                "growth_percent", "source_record_refs", "next_cursor",
                "provider_status", "content_hash",
            ),
        )
    if (slug, operation) == ("sec-financials", "get_financial_statements"):
        # P13ag: one row per statement line, per period, flat.
        #
        # The parser's own shape keys each period as a column
        # (``{"2026-06-30": 2814828000}``), which a closed schema cannot
        # describe -- the key is data. So the adapter normalises to one row per
        # (statement, concept, period) and the wire carries explicit
        # ``period_end``. The raw parser output is hashed as the artifact
        # regardless, so nothing is lost by normalising the part that is
        # validated.
        line = _object_schema(
            {
                "statement": {"type": "string", "enum": ["income", "balance", "cash"]},
                "concept": _string(),
                "label": _string(),
                # The disclosed structure: where the line sits and what it rolls
                # into. This is the part company-facts cannot answer.
                "level": _integer(0),
                "parent_concept": {"type": ["string", "null"]},
                "is_breakdown": {"type": "boolean"},
                "dimension_axis": {"type": ["string", "null"]},
                "dimension_member": {"type": ["string", "null"]},
                # P13ag: a 10-Q reports the quarter and the year to date with
                # the same period_end -- EPAM's Q2 revenue and its H1 revenue
                # both end 2026-06-30. Without the start they are one number
                # twice, and the whole 单季 / 累计 discipline rests on telling
                # them apart. Null on a balance-sheet line, which is an instant
                # and has no start.
                "period_start": {"type": ["string", "null"]},
                # A figure is text on the wire for the same reason every other
                # figure in this system is: a float is not what was filed.
                "period_end": _string(),
                "value": {
                    "type": ["string", "null"],
                    "pattern": "^-?(0|[1-9][0-9]*)([.][0-9]+)?$",
                },
                "unit": _string(),
                "balance": {"type": ["string", "null"]},
            },
            (
                "statement", "concept", "label", "level", "parent_concept",
                "is_breakdown", "dimension_axis", "dimension_member",
                "period_start", "period_end", "value", "unit", "balance",
            ),
        )
        filing = _object_schema(
            {
                "accession": {
                    "type": "string",
                    "pattern": "^[0-9]{10}-[0-9]{2}-[0-9]{6}$",
                },
                "form": {"type": "string", "enum": ["10-Q", "10-K"]},
                "filed": _string(),
                "report_date": _string(),
                "lines": {"type": "array", "items": line},
            },
            ("accession", "form", "filed", "report_date", "lines"),
        )
        return _object_schema(
            {
                "schema_version": {"type": "string", "enum": ["0.1"]},
                "cik": {"type": "string", "pattern": "^[0-9]{10}$"},
                "entity_name": _string(),
                "filings": {"type": "array", "items": filing},
                "source_record_refs": _array_of_strings(),
                "next_cursor": {"type": ["string", "null"]},
                "provider_status": _integer(100),
            },
            (
                "schema_version", "cik", "entity_name", "filings",
                "source_record_refs", "next_cursor", "provider_status",
            ),
        )
    if slug == "yfinance":
        decimal = {"type": "string", "pattern": "^-?(0|[1-9][0-9]*)([.][0-9]+)?$"}
        nullable_decimal = {
            "type": ["string", "null"],
            "pattern": "^-?(0|[1-9][0-9]*)([.][0-9]+)?$",
        }
        iso_date = {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}
        if operation == "daily_prices":
            # P11a: one row per trading day, and Close and Adj Close in
            # separate columns.
            #
            # This is the lesson the owner's other tooling paid for. Yahoo's
            # `auto_adjust=True` silently replaces Close with the
            # split-and-dividend-adjusted series and drops Adj Close, so a
            # later reader cannot tell which one it is holding -- and anyone
            # who then adds dividends on top of an adjusted price counts them
            # twice. Both columns are stored, always, and which is which is a
            # column name rather than a convention.
            bar = _object_schema(
                {
                    "date": iso_date,
                    "open": decimal, "high": decimal, "low": decimal,
                    "close": decimal, "adj_close": decimal,
                    "volume": decimal,
                },
                ("date", "open", "high", "low", "close", "adj_close", "volume"),
            )
            # Share count and market capitalisation are not properties of a
            # trading day: Yahoo reports the latest it knows, once, with no
            # history behind it. Carrying them as their own dated observations
            # keeps them from being read as "the shares outstanding on that
            # bar", which is a claim this source cannot support.
            observation = _object_schema(
                {
                    "observation": {
                        "type": "string",
                        "enum": ["shares_outstanding", "market_cap"],
                    },
                    "as_of": iso_date,
                    "value": decimal,
                    "unit": _string(),
                },
                ("observation", "as_of", "value", "unit"),
            )
            return _object_schema(
                {
                    "schema_version": {"type": "string", "enum": ["0.1"]},
                    "ticker": _string(),
                    "currency": _string(),
                    "requested_start": iso_date,
                    "requested_end": iso_date,
                    # Frozen false on the wire, so a run that adjusted the
                    # prices cannot be validated as one that did not.
                    "auto_adjust": {"type": "boolean", "enum": [False]},
                    # When the source was read. A window that includes today
                    # returns the last trade so far in the same shape as a
                    # settled close, and this is the only field that can tell
                    # a later reader which one it is holding.
                    "captured_at": _string(),
                    "bars": {"type": "array", "items": bar},
                    # Days the source returned with a hole in them. A frame
                    # that arrives entirely as NaN must not be indistinguishable
                    # from a genuinely quiet window.
                    "dropped_row_count": _integer(0),
                    "observations": {"type": "array", "items": observation},
                    "source_record_refs": _array_of_strings(),
                    "next_cursor": {"type": ["string", "null"]},
                    "provider_status": _integer(100),
                },
                (
                    "schema_version", "ticker", "currency", "requested_start",
                    "requested_end", "auto_adjust", "captured_at", "bars",
                    "dropped_row_count", "observations",
                    "source_record_refs", "next_cursor", "provider_status",
                ),
            )
        if operation == "analyst_estimates":
            # P11a: what sell-side analysts said, which is an opinion with a
            # date on it and never a fundamental. Every figure is nullable
            # because Yahoo drops whole blocks without warning, and an absent
            # estimate has to look absent rather than like a zero.
            price_target = _object_schema(
                {
                    "current": nullable_decimal, "high": nullable_decimal,
                    "low": nullable_decimal, "mean": nullable_decimal,
                    "median": nullable_decimal,
                    "number_of_analysts": {"type": ["integer", "null"], "minimum": 0},
                },
                ("current", "high", "low", "mean", "median", "number_of_analysts"),
            )
            # Nullable counts. "No analyst rates it a sell" and "Yahoo did not
            # say how many rate it a sell" are different facts, and a zero can
            # only express one of them.
            nullable_count = {"type": ["integer", "null"], "minimum": 0}
            recommendation = _object_schema(
                {
                    "period": _string(),
                    "strong_buy": nullable_count, "buy": nullable_count,
                    "hold": nullable_count, "sell": nullable_count,
                    "strong_sell": nullable_count,
                },
                ("period", "strong_buy", "buy", "hold", "sell", "strong_sell"),
            )
            estimate = _object_schema(
                {
                    "period": _string(),
                    "avg": nullable_decimal, "low": nullable_decimal,
                    "high": nullable_decimal,
                    "year_ago": nullable_decimal,
                    "growth": nullable_decimal,
                    "number_of_analysts": {"type": ["integer", "null"], "minimum": 0},
                    "currency": {"type": ["string", "null"]},
                },
                (
                    "period", "avg", "low", "high", "year_ago", "growth",
                    "number_of_analysts", "currency",
                ),
            )
            return _object_schema(
                {
                    "schema_version": {"type": "string", "enum": ["0.1"]},
                    "ticker": _string(),
                    "as_of": iso_date,
                    "price_target": price_target,
                    "recommendations": {"type": "array", "items": recommendation},
                    "eps_estimates": {"type": "array", "items": estimate},
                    "revenue_estimates": {"type": "array", "items": estimate},
                    "source_record_refs": _array_of_strings(),
                    "next_cursor": {"type": ["string", "null"]},
                    "provider_status": _integer(100),
                },
                (
                    "schema_version", "ticker", "as_of", "price_target",
                    "recommendations", "eps_estimates", "revenue_estimates",
                    "source_record_refs", "next_cursor", "provider_status",
                ),
            )
        if operation == "calendar":
            # C1: dates only, and every one of them nullable.
            #
            # No consensus figures here even though Yahoo serves them in the
            # same block. `analyst_estimates` already carries the EPS and
            # revenue consensus, and two operations claiming the same number
            # is how the two of them come to disagree.
            #
            # `earnings_dates` is an array because Yahoo says "some time
            # between these two days" when it cannot narrow the date, and
            # collapsing that to one day would invent a precision the source
            # did not offer. One entry is a day Yahoo names; two are the ends
            # of a window; none is Yahoo having nothing.
            #
            # `dividend_date` and `ex_dividend_date` are what Yahoo last knew
            # and are very often in the past -- DXC still reports an ex-date
            # from March 2020, six years after it stopped paying. They are
            # carried verbatim, and deciding that a past date is not a
            # forthcoming event is the reader's job, not this contract's.
            return _object_schema(
                {
                    "schema_version": {"type": "string", "enum": ["0.1"]},
                    "ticker": _string(),
                    "as_of": iso_date,
                    "captured_at": _string(),
                    "earnings_dates": {
                        "type": "array", "uniqueItems": True, "items": iso_date,
                    },
                    "dividend_date": {"type": ["string", "null"]},
                    "ex_dividend_date": {"type": ["string", "null"]},
                    "source_record_refs": _array_of_strings(),
                    "next_cursor": {"type": ["string", "null"]},
                    "provider_status": _integer(100),
                },
                (
                    "schema_version", "ticker", "as_of", "captured_at",
                    "earnings_dates", "dividend_date", "ex_dividend_date",
                    "source_record_refs", "next_cursor", "provider_status",
                ),
            )
    # S1: the two human / vendor feeds.
    #
    # Both enumerate documents that already exist as bytes on this machine, so
    # every header row carries the sha256 of the document body. That hash is
    # the join between "the connector said this note exists" and "the
    # acquisition wrote these bytes into the spool"; without it a feed row is
    # a filename and a claim.
    #
    # `evidence_tier` is on the wire rather than derived downstream because
    # the tier is a fact about the source, not about the text.
    #
    # `next_cursor` is where a listing admits it did not finish. A local feed
    # can hold more documents in one window than a bounded response may carry,
    # and a listing that stopped at its cap is `partial`, never `enumerated`.
    # The cursor is the day of the oldest row returned: everything before it
    # is still unread.
    if slug in {"sales-notes", "company-wiki"}:
        sha256 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
        instant = {
            "type": "string",
            "pattern": (
                "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                "([.][0-9]{1,6})?[+][0-9]{2}:[0-9]{2}$"
            ),
        }
        day = {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}
        nullable_day = {"type": ["string", "null"],
                        "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}
        if slug == "sales-notes":
            note = _object_schema(
                {
                    "note_id": {"type": "string", "pattern": "^sales-note:[0-9a-z]{6,40}$"},
                    "sender": _string(),
                    "sender_address": _string(),
                    "sender_domain": _string(),
                    "subject": {"type": "string"},
                    "sent_at": instant,
                    "is_priority": {"type": "boolean"},
                    "body_sha256": sha256,
                    "body_chars": _integer(0),
                    "digest_ref": _string(),
                    "evidence_tier": {"type": "string", "enum": ["sell_side"]},
                    "analyst_named": {"type": "boolean"},
                },
                (
                    "note_id", "sender", "sender_address", "sender_domain",
                    "subject", "sent_at", "is_priority", "body_sha256",
                    "body_chars", "digest_ref", "evidence_tier", "analyst_named",
                ),
            )
            if operation == "list_notes":
                return _object_schema(
                    {
                        "schema_version": {"type": "string", "enum": ["0.1"]},
                        "since": day,
                        "until": day,
                        "sender_domain": {"type": ["string", "null"]},
                        "notes": {"type": "array", "items": note},
                        "note_count": _integer(0),
                        "truncated": {"type": "boolean"},
                        "source_record_refs": _array_of_strings(),
                        "next_cursor": nullable_day,
                        "provider_status": _integer(100),
                    },
                    (
                        "schema_version", "since", "until", "sender_domain",
                        "notes", "note_count", "truncated", "source_record_refs",
                        "next_cursor", "provider_status",
                    ),
                )
            return _object_schema(
                {
                    "schema_version": {"type": "string", "enum": ["0.1"]},
                    "note": note,
                    "body": {"type": "string"},
                    "source_record_refs": _array_of_strings(),
                    "next_cursor": {"type": ["string", "null"]},
                    "provider_status": _integer(100),
                },
                (
                    "schema_version", "note", "body", "source_record_refs",
                    "next_cursor", "provider_status",
                ),
            )
        document = _object_schema(
            {
                "document_id": {
                    "type": "string",
                    "pattern": "^company-wiki-doc:sha256:[0-9a-f]{64}$",
                },
                "doc_type": _string(),
                "doc_type_key": {
                    "type": "string",
                    "enum": [
                        "management_meeting_minutes", "expert_interview",
                        "broker_report", "quarterly_note", "ndr",
                        "buy_side_note", "research_note", "other",
                    ],
                },
                "evidence_tier": {
                    "type": "string",
                    "enum": [
                        "management_statement", "expert", "sell_side",
                        "internal", "unclassified",
                    ],
                },
                "doc_date": day,
                "category_type": {"type": "string", "enum": ["company", "sector"]},
                "category_name": _string(),
                # An industry note carries no company tag and is not forced
                # to have one. An empty list here is the honest answer.
                "company_tags": {"type": "array", "uniqueItems": True, "items": _string()},
                "sector_tags": {"type": "array", "uniqueItems": True, "items": _string()},
                "topic_tags": {"type": "array", "uniqueItems": True, "items": _string()},
                "text_sha256": sha256,
                "text_chars": _integer(0),
            },
            (
                "document_id", "doc_type", "doc_type_key", "evidence_tier",
                "doc_date", "category_type", "category_name", "company_tags",
                "sector_tags", "topic_tags", "text_sha256", "text_chars",
            ),
        )
        if operation == "list_documents":
            return _object_schema(
                {
                    "schema_version": {"type": "string", "enum": ["0.1"]},
                    "since": day,
                    "until": day,
                    "company": {"type": ["string", "null"]},
                    "industry": {"type": ["string", "null"]},
                    "documents": {"type": "array", "items": document},
                    "document_count": _integer(0),
                    "truncated": {"type": "boolean"},
                    "source_record_refs": _array_of_strings(),
                    "next_cursor": nullable_day,
                    "provider_status": _integer(100),
                },
                (
                    "schema_version", "since", "until", "company", "industry",
                    "documents", "document_count", "truncated",
                    "source_record_refs", "next_cursor", "provider_status",
                ),
            )
        return _object_schema(
            {
                "schema_version": {"type": "string", "enum": ["0.1"]},
                "document": document,
                "text": {"type": "string"},
                "source_record_refs": _array_of_strings(),
                "next_cursor": {"type": ["string", "null"]},
                "provider_status": _integer(100),
            },
            (
                "schema_version", "document", "text", "source_record_refs",
                "next_cursor", "provider_status",
            ),
        )
    return _object_schema(
        {
            "source_record_refs": _array_of_strings(),
            "next_cursor": {"type": ["string", "null"]},
            "provider_status": _integer(100),
        },
        ("source_record_refs", "next_cursor", "provider_status"),
    )


def _operation(
    name: str,
    *,
    completeness: str,
    pagination: str = "none",
    input_fields: Sequence[str] = ("query",),
    optional_fields: Sequence[str] | None = None,
) -> dict[str, Any]:
    optional = (
        tuple(field for field in input_fields if field == "cursor")
        if optional_fields is None
        else tuple(optional_fields)
    )
    unknown_optional = set(optional) - set(input_fields)
    if unknown_optional:
        raise ConnectorInventoryError(
            f"operation {name} optional fields are not input fields: "
            f"{sorted(unknown_optional)}"
        )
    return {
        "name": name,
        "source_method": name,
        "completeness": completeness,
        "pagination": pagination,
        "input_fields": tuple(input_fields),
        "optional_fields": optional,
    }


PROFILE_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "slug": "cninfo", "connector_ref": "connector:cninfo-announcements",
        "source_ref": "source:cninfo", "source_type": "official_filing",
        "transport": "public_https", "target": "transport:public-http:0.1",
        "hosts": ("www.cninfo.com.cn",), "auth": "none",
        "forbidden": (), "fallbacks": (),
        "operations": (
            _operation("list_announcements", completeness="enumerated", pagination="page", input_fields=("stock_code", "date_from", "date_to", "page", "page_size")),
            _operation("get_announcement_document", completeness="enumerated", input_fields=("announcement_id",)),
            _operation("get_announcement_text", completeness="enumerated", input_fields=("announcement_id",)),
            _operation("list_revisions", completeness="enumerated", pagination="page", input_fields=("announcement_id", "page")),
        ),
        "gate": "recorded_public_reference_shadow",
    },
    {
        "slug": "sec", "connector_ref": "connector:sec-edgar",
        "source_ref": "source:sec-edgar", "source_type": "official_filing",
        "transport": "public_https", "target": "transport:public-http:0.1",
        "hosts": ("www.sec.gov", "data.sec.gov"), "auth": "none",
        "forbidden": ("route:arbitrary-attachment-url",), "fallbacks": (),
        "operations": (
            _operation("list_filings", completeness="enumerated", pagination="cursor", input_fields=("issuer", "form", "date_from", "date_to", "cursor", "limit")),
            _operation("list_official_attachments", completeness="enumerated", input_fields=("accession",)),
            _operation("get_official_attachment", completeness="enumerated", input_fields=("accession", "attachment_ref")),
            _operation("read_item", completeness="enumerated", input_fields=("accession", "item")),
            _operation(
                "get_company_facts",
                completeness="enumerated",
                input_fields=(
                    "cik", "taxonomy", "concept_candidates", "unit", "form",
                    "filed_from", "filed_to",
                ),
            ),
        ),
        "gate": "recorded_public_reference_shadow",
    },
    # P13ag: SEC financial statements, read through `edgartools` rather than
    # one XBRL concept at a time.
    #
    # The same SEC is the same source, so `source_ref` is shared with the
    # filings connector and the two source hashes match. What differs is the
    # shape of what comes back: the statement *structure* -- which concept is
    # which line, its level and parent, the dimension axes a company reports
    # against -- which the company-facts operation does not carry and which a
    # model needs before it can have line items at all.
    #
    # It does not replace reading the filings. Anything the parser does not
    # reach still has to come from the original text, and that lane stays.
    {
        "slug": "sec-financials",
        "connector_ref": "connector:sec-financial-statements",
        "source_ref": "source:sec-edgar", "source_type": "official_filing",
        "transport": "public_https", "target": "transport:public-http:0.1",
        "hosts": ("www.sec.gov", "data.sec.gov"), "auth": "none",
        "forbidden": ("route:arbitrary-attachment-url",), "fallbacks": (),
        "operations": (
            _operation(
                "get_financial_statements",
                completeness="enumerated",
                input_fields=("cik", "ticker", "form", "statement", "limit"),
                optional_fields=("ticker", "statement", "limit"),
            ),
        ),
        "gate": "recorded_public_reference_shadow",
    },
    # P13ah: earnings call transcripts from roic.ai.
    #
    # The Playbook's Initial Screen asks for four quarters of calls and the
    # Deep Insight Gate rests on what operators said. AlphaEngine carries them
    # too, and is capped at 130 calls a day -- live, CTSH sat at one call of
    # four with the cap exhausted. A second, independent source for the same
    # requirement is the difference between waiting a day and not.
    #
    # Public web, no credential: the transcript is a page on roic.ai. Split in
    # two the way every library here is -- listing what exists and reading one
    # are different permissions.
    {
        "slug": "roic-transcript", "connector_ref": "connector:roic-transcript",
        "source_ref": "source:roic", "source_type": "public_web",
        "transport": "public_https", "target": "transport:public-http:0.1",
        "hosts": ("www.roic.ai",), "auth": "none",
        "forbidden": ("route:arbitrary-attachment-url",), "fallbacks": (),
        "operations": (
            _operation(
                "list_transcripts", completeness="enumerated",
                input_fields=("ticker",),
            ),
            _operation(
                "get_transcript", completeness="enumerated",
                input_fields=("ticker", "fiscal_year", "fiscal_quarter"),
            ),
        ),
        "gate": "recorded_public_reference_shadow",
    },
    # P11a: daily prices and street estimates from Yahoo Finance, read through
    # the `yfinance` library.
    #
    # This is an *unofficial* free source: Yahoo publishes no API and no terms
    # that cover this, the library scrapes endpoints that can move without
    # notice, and there is nobody to appeal to when they do. That is written
    # down here rather than discovered later, and it is why the daily quota is
    # a couple of hundred calls rather than a thousand: politeness towards a
    # source that has not agreed to serve us.
    #
    # Two operations, because a price and an analyst's opinion are not the same
    # kind of thing and a schema hash binds one operation. Prices are facts a
    # market printed; estimates are what sell-side analysts said, and they enter
    # the system as claims about opinion, never as fundamentals. Yahoo also
    # carries financial statements, and this connector deliberately does not
    # expose them: SEC is the primary source for a filed figure and a scraped
    # second-hand copy of one would be a worse number wearing the same clothes.
    {
        "slug": "yfinance", "connector_ref": "connector:yahoo-finance",
        "source_ref": "source:yahoo-finance", "source_type": "market_data",
        "transport": "public_https", "target": "transport:public-http:0.1",
        "hosts": ("query1.finance.yahoo.com", "query2.finance.yahoo.com"),
        "auth": "none",
        "forbidden": ("route:arbitrary-attachment-url",), "fallbacks": (),
        "operations": (
            _operation(
                "daily_prices", completeness="enumerated",
                input_fields=("ticker", "start", "end"),
            ),
            _operation(
                "analyst_estimates", completeness="ranked",
                input_fields=("ticker",),
            ),
            # C1: the dated corporate events Yahoo knows about. Its own
            # operation rather than a block inside `analyst_estimates`,
            # because a schema hash binds one operation and an approval to
            # read what analysts forecast should not silently widen into
            # reading when the company will next speak.
            _operation(
                "calendar", completeness="enumerated",
                input_fields=("ticker",),
            ),
        ),
        "gate": "recorded_public_reference_shadow",
    },
    {
        "slug": "alphaengine", "connector_ref": "connector:alphaengine-library",
        "source_ref": "source:alphaengine", "source_type": "authenticated_library",
        "transport": "mcp_managed", "target": "mcp-target:alphaengine",
        "hosts": (), "auth": "host_owned", "forbidden": ("route:public-http",), "fallbacks": (),
        "operations": (
            _operation("search_library", completeness="ranked", pagination="cursor", input_fields=("query", "filters", "cursor")),
            _operation("get_document", completeness="enumerated", pagination="cursor", input_fields=("document_ref", "cursor")),
        ),
        "gate": "mcp_managed_runner_v0.2_and_credential_authority",
    },
    {
        "slug": "x-xreach", "connector_ref": "connector:x-xreach",
        "source_ref": "source:x", "source_type": "social_enumeration",
        "transport": "host_tool", "target": "host-tool:xreach",
        "hosts": (), "auth": "host_owned", "forbidden": ("route:last30days-x",), "fallbacks": (),
        "operations": (
            _operation("timeline", completeness="enumerated", pagination="cursor", input_fields=("handle", "date_from", "date_to", "cursor")),
            _operation("get_post", completeness="enumerated", input_fields=("post_ref",)),
            _operation("get_thread", completeness="enumerated", pagination="cursor", input_fields=("post_ref", "cursor")),
        ),
        "gate": "host_tool_runner_v0.2_and_credential_authority",
    },
    {
        "slug": "x-x-search", "connector_ref": "connector:x-semantic-search",
        "source_ref": "source:x", "source_type": "social_search",
        "transport": "host_tool", "target": "host-tool:x-search",
        "hosts": (), "auth": "host_owned", "forbidden": ("route:xreach-semantic",), "fallbacks": (),
        "operations": (
            _operation("semantic_search", completeness="ranked", pagination="cursor", input_fields=("query", "allowed_handles", "date_from", "date_to", "cursor")),
            _operation("analyze_post_media", completeness="ranked", input_fields=("post_ref",)),
        ),
        "gate": "host_tool_runner_v0.2_and_credential_authority",
    },
    {
        "slug": "reddit-last30days", "connector_ref": "connector:reddit-last30days",
        "source_ref": "source:reddit", "source_type": "social_search",
        "transport": "host_tool", "target": "host-tool:last30days-reddit-keyless",
        "hosts": (), "auth": "none",
        "forbidden": ("route:agent-reach-reddit-json", "route:browser-cookie", "route:last30days-x"),
        "fallbacks": (),
        "operations": (
            _operation("search_posts", completeness="ranked", pagination="cursor", input_fields=("query", "subreddits", "date_from", "date_to", "cursor")),
        ),
        "gate": "host_tool_runner_v0.2",
    },
    {
        "slug": "guidepoint", "connector_ref": "connector:guidepoint-library",
        "source_ref": "source:guidepoint", "source_type": "authenticated_library",
        "transport": "mcp_managed", "target": "mcp-target:guidepoint",
        "hosts": (), "auth": "host_owned", "forbidden": ("route:public-http",), "fallbacks": (),
        "operations": (
            _operation("search_library", completeness="ranked", pagination="cursor", input_fields=("query", "filters", "cursor")),
            _operation("get_transcript", completeness="enumerated", pagination="cursor", input_fields=("document_ref", "cursor")),
        ),
        "gate": "mcp_managed_runner_v0.2_and_credential_authority",
    },
    {
        "slug": "gemini-web-search", "connector_ref": "connector:gemini-web-search",
        "source_ref": "source:public-web", "source_type": "public_web",
        "transport": "host_tool", "target": "host-tool:gemini-web-search",
        "hosts": (), "auth": "host_owned", "forbidden": ("route:web-fetch",), "fallbacks": (),
        "operations": (
            _operation(
                "search_web",
                completeness="ranked",
                input_fields=("query", "date_after", "date_before", "freshness"),
                optional_fields=("date_after", "date_before", "freshness"),
            ),
        ),
        "gate": "host_tool_runner_v0.2_and_credential_authority",
    },
    {
        "slug": "web-fetch", "connector_ref": "connector:web-fetch",
        "source_ref": "source:public-web", "source_type": "public_web",
        "transport": "public_https", "target": "transport:public-http:0.1",
        "hosts": (), "host_policy": "per_call_authority", "auth": "none",
        "forbidden": ("route:credential-channel", "route:private-network"), "fallbacks": (),
        "operations": (
            _operation("fetch_get", completeness="partial", input_fields=("url_ref",)),
            _operation("fetch_head", completeness="enumerated", input_fields=("url_ref",)),
        ),
        "gate": "killable_total_deadline_public_transport",
    },
    {
        "slug": "xueqiu", "connector_ref": "connector:xueqiu",
        "source_ref": "source:xueqiu", "source_type": "market_data",
        "transport": "host_tool", "target": "host-tool:agent-reach-xueqiu-channel",
        "hosts": (), "auth": "host_owned",
        "forbidden": ("route:public-http", "route:reddit-cookie",),
        "fallbacks": (
            {
                "operation": "get_hot_stocks",
                "target_ref": "host-tool:cn-hk-findata-xq-hot-rank",
                "source_ref": "source:xueqiu",
                "adapter_ref": "adapter:cn-hk-findata:xq-hot-rank",
                "provenance_label": "xueqiu_hot_stock_rank_fallback",
            },
        ),
        "operations": (
            _operation("get_stock_quote", completeness="enumerated", input_fields=("symbol",)),
            _operation("get_hot_posts", completeness="ranked", pagination="page", input_fields=("limit", "page")),
            _operation("get_hot_stocks", completeness="ranked", pagination="page", input_fields=("limit", "stock_type", "page")),
            _operation("search_stock", completeness="ranked", pagination="page", input_fields=("query", "limit", "page")),
        ),
        "gate": "host_tool_runner_v0.2_and_credential_authority",
    },
    # S1: the two feeds a human already brings into this machine.
    #
    # Both read files that are *already on disk*. The sell-side notes were
    # fetched from Gmail by a host skill hours earlier and written out
    # verbatim; the wiki is markdown a person wrote or pasted. Dalton never
    # authenticates to either upstream, which is why the auth boundary is
    # `none` and `network` is false: the only permission this connector needs
    # is to read one directory.
    #
    # They are `authenticated_library` because that is what they are -- a
    # curated body of documents behind someone's credential -- even though the
    # credential was spent before Dalton saw the bytes.
    {
        "slug": "sales-notes", "connector_ref": "connector:sales-notes",
        "source_ref": "source:sales-notes", "source_type": "authenticated_library",
        "transport": "host_tool", "target": "host-tool:market-digest-output",
        "hosts": (), "auth": "none",
        # The digest file also carries a model-written summary of the same
        # mail. That summary is not the note and must never be cited as one.
        "forbidden": ("route:gmail-api", "route:market-digest-ai-summary"),
        "fallbacks": (),
        "operations": (
            # `since` and `until` bound one window. Both are required for the
            # same reason a paged search needs a page: an unbounded listing of
            # a feed that grows every day cannot be reconciled, and a listing
            # that silently stops at a record cap is not `enumerated` -- it is
            # a truncation wearing an enumeration's word.
            _operation(
                "list_notes", completeness="enumerated",
                input_fields=("since", "until", "sender_domain", "limit"),
                optional_fields=("sender_domain", "limit"),
            ),
            # `digest_ref` is an optional locator hint, not a second way to
            # ask: the run a note first appeared in is already on every
            # enumerated header, and passing it back turns a scan of every run
            # into opening one file.
            _operation(
                "get_note", completeness="enumerated",
                input_fields=("note_id", "digest_ref"),
                optional_fields=("digest_ref",),
            ),
        ),
        "gate": "host_tool_runner_v0.2",
    },
    {
        "slug": "company-wiki", "connector_ref": "connector:company-wiki",
        "source_ref": "source:company-wiki", "source_type": "authenticated_library",
        "transport": "host_tool", "target": "host-tool:company-wiki-corpus",
        "hosts": (), "auth": "none",
        # The wiki ships an embedding index over the same corpus. Ranked
        # nearest-neighbour lookup cannot be reconciled against a bounded
        # window, so it can never be the route for an `enumerated` operation.
        "forbidden": ("route:wiki-embedding-search", "route:wiki-gemini-tagger"),
        "fallbacks": (),
        "operations": (
            _operation(
                "list_documents", completeness="enumerated",
                input_fields=("since", "until", "company", "industry", "limit"),
                optional_fields=("company", "industry", "limit"),
            ),
            _operation("get_document", completeness="enumerated",
                       input_fields=("document_id",)),
        ),
        "gate": "host_tool_runner_v0.2",
    },
)


def _field_schema(name: str) -> dict[str, Any]:
    if name in {"page", "page_size", "limit", "stock_type"}:
        return _integer(1)
    if name == "filters":
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                field: _string()
                for field in (
                    "company", "industry", "document_type", "geography",
                    "date_from", "date_to",
                )
            },
        }
    if name == "cursor":
        return {"type": ["string", "null"]}
    # P11a: ``start`` and ``end`` bound one price window and are dates, not
    # free text. A window whose ends cannot be parsed is a window nobody can
    # replay, and replaying the exact window is the whole point of binding a
    # bar to the invocation that produced it.
    if name in {"date_after", "date_before", "start", "end", "since", "until"}:
        return {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}
    # S1: the run a note first appeared in, ``market-digest:<date>:<AM|PM>``.
    # A free-text hint would let a caller point the reader at an arbitrary
    # string; the shape is fixed because the shape is what makes it a locator.
    if name == "digest_ref":
        return {"type": "string",
                "pattern": "^market-digest:[0-9]{4}-[0-9]{2}-[0-9]{2}:(AM|PM)$"}
    if name == "freshness":
        return {"type": "string", "enum": ["day", "week", "month", "year"]}
    if name in {"allowed_handles", "subreddits", "concept_candidates"}:
        return _array_of_strings()
    return _string()


def _schema_document(slug: str, operation: str, direction: str, document: Mapping[str, Any]) -> dict[str, Any]:
    schema_ref = f"schema:connector-inventory:{slug}:{operation}:{direction}:0.1"
    schema = json.loads(canonical_json(document))
    return {"schema_ref": schema_ref, "schema_hash": content_hash(schema), "document": schema}


def _validate_frozen_profile_contract(profile: Mapping[str, Any]) -> None:
    definitions = {
        definition["connector_ref"]: definition for definition in PROFILE_DEFINITIONS
    }
    definition = definitions.get(profile["connector_ref"])
    if definition is None:
        raise ConnectorInventoryError("profile connector_ref is not in the frozen inventory")
    slug = definition["slug"]
    expected_host_policy = definition.get(
        "host_policy",
        "literal_allowlist" if definition["transport"] == "public_https"
        else "not_applicable",
    )
    expected_auth = (
        {
            "mode": "none", "owner": "none", "credential_material": "forbidden",
            "use_time_authority": "none",
        }
        if definition["auth"] == "none"
        else {
            "mode": "host_owned", "owner": "host", "credential_material": "forbidden",
            "use_time_authority": "required_future",
        }
    )
    expected_fallbacks = json.loads(canonical_json(list(definition["fallbacks"])))
    if (
        profile["id"] != f"connector-profile-template:{slug}:0.1"
        or profile["fixture_manifest_ref"]
        != f"connector-fixture-manifest:{slug}:0.1"
        or profile["source_identity"]
        != {
            "source_ref": definition["source_ref"],
            "source_type": definition["source_type"],
            "source_version": "inventory-2026-08-14",
        }
        or profile["transport"]["kind"] != definition["transport"]
        or profile["transport"]["target_ref"] != definition["target"]
        or profile["transport"]["host_policy"] != expected_host_policy
        or profile["transport"]["allowed_hosts"] != list(definition["hosts"])
        or profile["auth_boundary"] != expected_auth
        or profile["route_restrictions"]["allowed_target_refs"]
        != [definition["target"]]
        or profile["route_restrictions"]["forbidden_target_refs"]
        != list(definition["forbidden"])
        or profile["route_restrictions"]["fallback_routes"] != expected_fallbacks
        or profile["readiness"]["required_gate"] != definition["gate"]
    ):
        raise ConnectorInventoryError("profile differs from the frozen connector definition")
    expected_operations = []
    expected_documents = []
    for operation_definition in definition["operations"]:
        name = operation_definition["name"]
        required_fields = tuple(
            field for field in operation_definition["input_fields"]
            if field not in operation_definition["optional_fields"]
        )
        input_document = _schema_document(
            slug, name, "input",
            _object_schema(
                {field: _field_schema(field) for field in operation_definition["input_fields"]},
                required_fields,
            ),
        )
        output_document = _schema_document(
            slug, name, "output",
            _output_schema(slug, name),
        )
        expected_documents.extend((input_document, output_document))
        mode = operation_definition["pagination"]
        expected_operations.append(
            {
                "operation": name,
                "source_method": operation_definition["source_method"],
                "input_schema_ref": input_document["schema_ref"],
                "input_schema_hash": input_document["schema_hash"],
                "output_schema_ref": output_document["schema_ref"],
                "output_schema_hash": output_document["schema_hash"],
                "completeness_ceiling": operation_definition["completeness"],
                "pagination": {
                    "mode": mode,
                    "cursor_field": None if mode == "none" else (
                        "page" if mode == "page" else "cursor"
                    ),
                    "bounded_window_required": (
                        operation_definition["completeness"] == "enumerated"
                    ),
                    "max_pages": 1 if mode == "none" else 20,
                },
                "side_effects": ["read:recorded-fixture"],
            }
        )
    if (
        profile["operations"] != expected_operations
        or profile["schema_documents"] != expected_documents
    ):
        raise ConnectorInventoryError("operation contracts differ from the frozen definition")


# P13ag: the allowlist is what this build defines, not a second copy of it.
#
# It guards the packaged index against a hand-edited or tampered file, and it
# still does: the index must match the frozen definitions above. What it no
# longer does is disagree with them silently, which is what a literal second
# list eventually does -- the same failure shape as every identity bug in this
# codebase.
_REQUIRED_CONNECTOR_REFS = frozenset(
    definition["connector_ref"] for definition in PROFILE_DEFINITIONS
)

def _fixture_case(
    slug: str, operation: str, scenario: str, completeness: str
) -> dict[str, Any]:
    succeeded = scenario in {"success", "empty", "pagination", "partial"}
    outcome = "succeeded" if succeeded else (
        "rate_limited" if scenario == "rate_limited" else
        "timeout" if scenario == "timeout" else "failed"
    )
    provider_status = {
        "rate_limited": 429, "permission_denied": 403, "revoked": 401,
        "timeout": None,
    }.get(scenario, 200)
    if scenario == "empty":
        source_status, effective_completeness, refs = "empty", completeness, []
    elif scenario == "partial" or scenario == "pagination":
        source_status, effective_completeness, refs = "partial", "partial", [f"record:{slug}:synthetic:1"]
    elif scenario == "success":
        source_status, effective_completeness, refs = "complete", completeness, [f"record:{slug}:synthetic:1"]
    else:
        source_status, effective_completeness, refs = None, None, []
    raw_hash = content_hash(
        {"synthetic": True, "connector": slug, "operation": operation, "scenario": scenario}
    ) if succeeded else None
    return {
        "case_ref": f"fixture:{slug}:{operation}:{scenario}:0.1",
        "scenario": scenario,
        "operation": operation,
        "outcome": outcome,
        "provider_status": provider_status,
        "source_status": source_status,
        "completeness": effective_completeness,
        "page": 1 if scenario == "pagination" else None,
        "next_cursor": "synthetic-next" if scenario == "pagination" else None,
        "source_record_refs": refs,
        "raw_payload_hash": raw_hash,
        "error_code": None if succeeded else scenario,
    }


def build_connector_inventory() -> dict[str, Any]:
    """Build deterministic templates, fixture manifests, proposals, and index."""

    templates: dict[str, dict[str, Any]] = {}
    fixtures: dict[str, dict[str, Any]] = {}
    proposals: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    for definition in PROFILE_DEFINITIONS:
        slug = definition["slug"]
        template_ref = f"connector-profile-template:{slug}:0.1"
        fixture_ref = f"connector-fixture-manifest:{slug}:0.1"
        fixture_operations = [
            {
                "operation": operation["name"],
                "pagination_mode": operation["pagination"],
            }
            for operation in definition["operations"]
        ]
        fixture_cases: list[dict[str, Any]] = []
        for operation in definition["operations"]:
            scenarios = list(sorted(_COMMON_SCENARIOS))
            if operation["pagination"] != "none":
                scenarios.append("pagination")
            if definition["auth"] == "host_owned":
                scenarios.extend(sorted(_AUTH_SCENARIOS))
            fixture_cases.extend(
                _fixture_case(
                    slug, operation["name"], scenario, operation["completeness"]
                )
                for scenario in scenarios
            )
        fixture = _with_hash(
            {
                "schema_version": "0.1", "id": fixture_ref, "created_at": CREATED_AT,
                "connector_template_ref": template_ref,
                "recording_boundary": (
                    "public_provider" if definition["transport"] == "public_https" else "host_gateway"
                ),
                "authenticated": definition["auth"] == "host_owned",
                "synthetic": True,
                "operations": fixture_operations,
                "cases": fixture_cases,
            }
        )
        fixture = validate_connector_fixture_manifest(fixture)
        fixtures[slug] = fixture

        documents: list[dict[str, Any]] = []
        operations: list[dict[str, Any]] = []
        for operation_definition in definition["operations"]:
            name = operation_definition["name"]
            input_document = _schema_document(
                slug, name, "input",
                _object_schema(
                    {field: _field_schema(field) for field in operation_definition["input_fields"]},
                    tuple(
                        field for field in operation_definition["input_fields"]
                        if field not in operation_definition["optional_fields"]
                    ),
                ),
            )
            output_document = _schema_document(
                slug, name, "output",
                _output_schema(slug, name),
            )
            documents.extend((input_document, output_document))
            mode = operation_definition["pagination"]
            operations.append(
                {
                    "operation": name,
                    "source_method": operation_definition["source_method"],
                    "input_schema_ref": input_document["schema_ref"],
                    "input_schema_hash": input_document["schema_hash"],
                    "output_schema_ref": output_document["schema_ref"],
                    "output_schema_hash": output_document["schema_hash"],
                    "completeness_ceiling": operation_definition["completeness"],
                    "pagination": {
                        "mode": mode,
                        "cursor_field": None if mode == "none" else ("page" if mode == "page" else "cursor"),
                        "bounded_window_required": operation_definition["completeness"] == "enumerated",
                        "max_pages": 1 if mode == "none" else 20,
                    },
                    "side_effects": ["read:recorded-fixture"],
                }
            )
        host_policy = definition.get(
            "host_policy",
            "literal_allowlist" if definition["transport"] == "public_https"
            else "not_applicable",
        )
        target_hash = content_hash(
            {
                "kind": definition["transport"],
                "target_ref": definition["target"],
                "host_policy": host_policy,
                "allowed_hosts": list(definition["hosts"]),
            }
        )
        auth_boundary = (
            {
                "mode": "none", "owner": "none", "credential_material": "forbidden",
                "use_time_authority": "none",
            }
            if definition["auth"] == "none"
            else {
                "mode": "host_owned", "owner": "host", "credential_material": "forbidden",
                "use_time_authority": "required_future",
            }
        )
        template = _with_hash(
            {
                "schema_version": "0.1", "id": template_ref, "created_at": CREATED_AT,
                "connector_ref": definition["connector_ref"],
                "source_identity": {
                    "source_ref": definition["source_ref"],
                    "source_type": definition["source_type"],
                    "source_version": "inventory-2026-08-14",
                },
                "transport": {
                    "kind": definition["transport"], "target_ref": definition["target"],
                    "target_hash": target_hash, "host_policy": host_policy,
                    "allowed_hosts": list(definition["hosts"]),
                },
                "auth_boundary": auth_boundary,
                "route_restrictions": {
                    "allowed_target_refs": [definition["target"]],
                    "forbidden_target_refs": list(definition["forbidden"]),
                    "fallback_routes": list(definition["fallbacks"]),
                    "provenance_label_required": True,
                },
                "schema_documents": documents,
                "operations": operations,
                "fixture_manifest_ref": fixture_ref,
                "fixture_manifest_hash": fixture["content_hash"],
                "readiness": {
                    "level": "inventory_connected", "lease_eligible": False,
                    "live_execution_allowed": False, "required_gate": definition["gate"],
                },
            }
        )
        template = validate_connector_profile_template(template)
        templates[slug] = template

        proposal_operations = [
            {
                "operation": item["operation"],
                "input_schema_ref": item["input_schema_ref"],
                "input_schema_hash": item["input_schema_hash"],
                "output_schema_ref": item["output_schema_ref"],
                "output_schema_hash": item["output_schema_hash"],
                "completeness": item["completeness_ceiling"],
                "pagination": item["pagination"]["mode"],
                "side_effects": item["side_effects"],
            }
            for item in operations
        ]
        proposal_base = {
            "schema_version": "0.2", "id": f"connector-proposal-manifest:{slug}:0.2",
            "created_at": CREATED_AT,
            "capability_proposal_ref": f"capability-proposal:connector:{slug}:0.1",
            "connector_ref": definition["connector_ref"],
            "source_identity": {
                "source": definition["source_ref"], "adapter": definition["target"],
                "source_version": "inventory-2026-08-14", "adapter_version": "inventory-0.1",
            },
            "adapter_package_ref": f"inventory-artifact:adapter-contract:{slug}:0.1",
            "adapter_source_hash": content_hash(
                {"target_ref": definition["target"], "operations": [item["name"] for item in definition["operations"]]}
            ),
            "profile_template_ref": template_ref,
            "profile_template_hash": template["content_hash"],
            "transport_kind": definition["transport"],
            "transport_target_ref": definition["target"],
            "transport_target_hash": target_hash,
            "auth_boundary": auth_boundary,
            "inventory_state": "proposal_only",
            "operations": proposal_operations,
            "fixture_manifest_ref": fixture_ref,
            "fixture_manifest_hash": fixture["content_hash"],
            "offline_attestation_policy_ref": "policy:connector-inventory-offline:0.1",
            "requested_canary": None,
            "promotion_policy_ref": "policy:connector-promotion:0.1",
            "builder_ref": "builder:dalton-connector-inventory:0.1",
        }
        proposal = dict(proposal_base)
        proposal["content_hash"] = content_hash(proposal_base)
        proposal = validate_connector_proposal_manifest(proposal)
        proposals[slug] = proposal
        entry = {
            "connector_ref": definition["connector_ref"],
            "profile_template_ref": template_ref,
            "profile_template_hash": template["content_hash"],
            "fixture_manifest_ref": fixture_ref,
            "fixture_manifest_hash": fixture["content_hash"],
            "proposal_manifest_ref": proposal["id"],
            "proposal_manifest_hash": proposal["content_hash"],
        }
        _validate_inventory_graph_entry(entry, template, fixture, proposal)
        entries.append(entry)
    index = validate_connector_inventory_index(
        _with_hash(
            {
                "schema_version": "0.1", "id": "connector-inventory:p1-0:0.1",
                "created_at": CREATED_AT, "profiles": entries,
            }
        )
    )
    return {"index": index, "templates": templates, "fixtures": fixtures, "proposals": proposals}


def _proposal_operations(profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "operation": item["operation"],
            "input_schema_ref": item["input_schema_ref"],
            "input_schema_hash": item["input_schema_hash"],
            "output_schema_ref": item["output_schema_ref"],
            "output_schema_hash": item["output_schema_hash"],
            "completeness": item["completeness_ceiling"],
            "pagination": item["pagination"]["mode"],
            "side_effects": item["side_effects"],
        }
        for item in profile["operations"]
    ]


def _read_proposal_package_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ConnectorInventoryError(f"proposal package member is not a regular file: {path.name}")
    if path.stat().st_size > _MAX_PROPOSAL_PACKAGE_FILE_BYTES:
        raise ConnectorInventoryError(f"proposal package member is too large: {path.name}")

    def closed_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ConnectorInventoryError(
                    f"proposal package member contains duplicate JSON key: {key}"
                )
            result[key] = value
        return result

    try:
        parsed = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=closed_pairs
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConnectorInventoryError(
            f"proposal package member is not canonical JSON: {path.name}"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise ConnectorInventoryError(
            f"proposal package member must contain one JSON object: {path.name}"
        )
    return dict(parsed)


def _validate_proposal_package_graph(
    profile: Mapping[str, Any],
    fixture: Mapping[str, Any],
    proposal: Mapping[str, Any],
) -> None:
    match = re.fullmatch(
        r"connector-profile-template:([a-z0-9][a-z0-9-]*):0\.1",
        profile["id"],
    )
    if match is None:
        raise ConnectorInventoryError("proposal profile id is not canonical")
    slug = match.group(1)
    frozen_slugs = {definition["slug"] for definition in PROFILE_DEFINITIONS}
    if slug in frozen_slugs or profile["connector_ref"] in _REQUIRED_CONNECTOR_REFS:
        raise ConnectorInventoryError(
            "connector proposal package cannot reuse a frozen inventory identity"
        )
    expected_fixture_operations = [
        {
            "operation": operation["operation"],
            "pagination_mode": operation["pagination"]["mode"],
        }
        for operation in profile["operations"]
    ]
    completeness_by_operation = {
        operation["operation"]: operation["completeness_ceiling"]
        for operation in profile["operations"]
    }
    fixture_semantics_bound = all(
        (
            case["completeness"]
            == completeness_by_operation[case["operation"]]
            if case["scenario"] in {"success", "empty"}
            else True
        )
        for case in fixture["cases"]
    )
    expected_recording_boundary = (
        "public_provider"
        if profile["transport"]["kind"] == "public_https"
        else "host_gateway"
    )
    expected_authenticated = profile["auth_boundary"]["mode"] == "host_owned"
    required_gates = _PROPOSAL_REQUIRED_GATES.get(
        (profile["transport"]["kind"], profile["auth_boundary"]["mode"]),
        frozenset(),
    )
    expected_adapter_source_hash = content_hash(
        {
            "target_ref": profile["transport"]["target_ref"],
            "operations": [item["operation"] for item in profile["operations"]],
        }
    )
    exact = (
        profile["created_at"] == fixture["created_at"] == proposal["created_at"]
        and fixture["connector_template_ref"] == profile["id"]
        and fixture["recording_boundary"] == expected_recording_boundary
        and fixture["authenticated"] is expected_authenticated
        and fixture["operations"] == expected_fixture_operations
        and fixture_semantics_bound
        and profile["fixture_manifest_ref"] == fixture["id"]
        and profile["fixture_manifest_hash"] == fixture["content_hash"]
        and proposal["connector_ref"] == profile["connector_ref"]
        and proposal["profile_template_ref"] == profile["id"]
        and proposal["profile_template_hash"] == profile["content_hash"]
        and proposal["fixture_manifest_ref"] == fixture["id"]
        and proposal["fixture_manifest_hash"] == fixture["content_hash"]
        and proposal["transport_kind"] == profile["transport"]["kind"]
        and proposal["transport_target_ref"] == profile["transport"]["target_ref"]
        and proposal["transport_target_hash"] == profile["transport"]["target_hash"]
        and proposal["auth_boundary"] == profile["auth_boundary"]
        and proposal["operations"] == _proposal_operations(profile)
        and proposal["source_identity"]["source"]
        == profile["source_identity"]["source_ref"]
        and proposal["source_identity"]["source_version"]
        == profile["source_identity"]["source_version"]
        and proposal["source_identity"]["adapter"]
        == profile["transport"]["target_ref"]
        and proposal["adapter_source_hash"] == expected_adapter_source_hash
        and proposal["id"] == f"connector-proposal-manifest:{slug}:0.2"
        and proposal["capability_proposal_ref"]
        == f"capability-proposal:connector:{slug}:0.1"
        and proposal["adapter_package_ref"]
        == f"inventory-artifact:adapter-contract:{slug}:0.1"
        and proposal["source_identity"]["adapter_version"] == "proposal-0.1"
        and proposal["offline_attestation_policy_ref"]
        == "policy:connector-inventory-offline:0.1"
        and proposal["promotion_policy_ref"] == "policy:connector-promotion:0.1"
        and proposal["inventory_state"] == "proposal_only"
        and proposal["requested_canary"] is None
        and profile["readiness"]["required_gate"] in required_gates
        and all(
            operation["side_effects"] == ["read:recorded-fixture"]
            for operation in profile["operations"]
        )
    )
    if not exact:
        raise ConnectorInventoryError("connector proposal package graph binding is not exact")


def load_connector_proposal_package(root: str | Path) -> dict[str, Any]:
    """Validate one offline proposal package without changing the frozen P1-0 inventory."""

    directory = Path(root)
    if directory.is_symlink() or not directory.is_dir():
        raise ConnectorInventoryError("connector proposal package root must be a directory")
    members = {member.name for member in directory.iterdir()}
    if members != _PROPOSAL_PACKAGE_FILES:
        raise ConnectorInventoryError(
            "connector proposal package must contain exactly profile.json, fixture.json, and proposal.json"
        )
    fixture = _validate_connector_fixture_manifest(
        _read_proposal_package_json(directory / "fixture.json"), frozen=False
    )
    profile = _validate_connector_profile_template(
        _read_proposal_package_json(directory / "profile.json"), frozen=False
    )
    try:
        proposal = validate_connector_proposal_manifest(
            _read_proposal_package_json(directory / "proposal.json")
        )
    except ConnectorError as exc:
        raise ConnectorInventoryError("connector proposal manifest is invalid") from exc
    if proposal["schema_version"] != "0.2":
        raise ConnectorInventoryError("connector proposal package requires manifest wire 0.2")
    _validate_proposal_package_graph(profile, fixture, proposal)
    return {"profile": profile, "fixture": fixture, "proposal": proposal}


def _validate_inventory_graph_entry(
    entry: Mapping[str, Any],
    profile: Mapping[str, Any],
    fixture: Mapping[str, Any],
    proposal: Mapping[str, Any],
) -> None:
    expected_fixture_operations = [
        {
            "operation": operation["operation"],
            "pagination_mode": operation["pagination"]["mode"],
        }
        for operation in profile["operations"]
    ]
    slug = profile["id"].removeprefix("connector-profile-template:").removesuffix(":0.1")
    definition = next(
        (
            item for item in PROFILE_DEFINITIONS
            if item["connector_ref"] == profile["connector_ref"]
        ),
        None,
    )
    if definition is None or definition["slug"] != slug:
        raise ConnectorInventoryError("profile cannot be resolved to a frozen definition")
    expected_recording_boundary = (
        "public_provider" if profile["transport"]["kind"] == "public_https"
        else "host_gateway"
    )
    expected_authenticated = profile["auth_boundary"]["mode"] == "host_owned"
    expected_adapter_source_hash = content_hash(
        {
            "target_ref": definition["target"],
            "operations": [item["name"] for item in definition["operations"]],
        }
    )
    completeness_by_operation = {
        item["operation"]: item["completeness_ceiling"]
        for item in profile["operations"]
    }
    fixture_semantics_bound = all(
        (
            case["completeness"] == completeness_by_operation[case["operation"]]
            if case["scenario"] in {"success", "empty"}
            else True
        )
        for case in fixture["cases"]
    )
    exact = (
        entry["connector_ref"] == profile["connector_ref"] == proposal["connector_ref"]
        and entry["profile_template_ref"] == profile["id"]
        and entry["profile_template_hash"] == profile["content_hash"]
        and entry["fixture_manifest_ref"] == fixture["id"]
        and entry["fixture_manifest_hash"] == fixture["content_hash"]
        and entry["proposal_manifest_ref"] == proposal["id"]
        and entry["proposal_manifest_hash"] == proposal["content_hash"]
        and fixture["connector_template_ref"] == profile["id"]
        and fixture["recording_boundary"] == expected_recording_boundary
        and fixture["authenticated"] is expected_authenticated
        and fixture["operations"] == expected_fixture_operations
        and fixture_semantics_bound
        and profile["fixture_manifest_ref"] == fixture["id"]
        and profile["fixture_manifest_hash"] == fixture["content_hash"]
        and proposal["profile_template_ref"] == profile["id"]
        and proposal["profile_template_hash"] == profile["content_hash"]
        and proposal["fixture_manifest_ref"] == fixture["id"]
        and proposal["fixture_manifest_hash"] == fixture["content_hash"]
        and proposal["transport_kind"] == profile["transport"]["kind"]
        and proposal["transport_target_ref"] == profile["transport"]["target_ref"]
        and proposal["transport_target_hash"] == profile["transport"]["target_hash"]
        and proposal["auth_boundary"] == profile["auth_boundary"]
        and proposal["operations"] == _proposal_operations(profile)
        and proposal["source_identity"]["source"] == profile["source_identity"]["source_ref"]
        and proposal["source_identity"]
        == {
            "source": definition["source_ref"], "adapter": definition["target"],
            "source_version": "inventory-2026-08-14",
            "adapter_version": "inventory-0.1",
        }
        and proposal["id"] == f"connector-proposal-manifest:{slug}:0.2"
        and proposal["capability_proposal_ref"]
        == f"capability-proposal:connector:{slug}:0.1"
        and proposal["adapter_package_ref"]
        == f"inventory-artifact:adapter-contract:{slug}:0.1"
        and proposal["adapter_source_hash"] == expected_adapter_source_hash
        and proposal["offline_attestation_policy_ref"]
        == "policy:connector-inventory-offline:0.1"
        and proposal["promotion_policy_ref"] == "policy:connector-promotion:0.1"
        and proposal["builder_ref"] == "builder:dalton-connector-inventory:0.1"
        and proposal["inventory_state"] == "proposal_only"
        and proposal["requested_canary"] is None
    )
    if not exact:
        raise ConnectorInventoryError("connector inventory graph binding is not exact")


# S7d: the packaged inventory is immutable for the life of a process, but
# every ``ResearchPlanAuthority.plan()`` read re-validated it from disk via
# ``_sec_template()`` (24 loads per SEC plan view).  On live, three SEC lane
# plans made ``thesis_impact_targets`` take 11.5s in a foreground profile and
# >30s in the ``ProcessType: Background`` writer, so the thesis-impact worker
# failed every run from 19:07Z on 2026-08-26.  The packaged load is cached
# once per process and handed out as a deep copy (1.5ms vs 150-420ms), so
# callers keep private, mutable results.  Explicit ``root`` loads stay
# uncached: tests point them at mutated copies and expect fresh validation.
_PACKAGED_INVENTORY_CACHE: dict[Path, dict[str, Any]] = {}


def load_packaged_connector_inventory(root: str | Path | None = None) -> dict[str, Any]:
    if root is not None:
        return _load_connector_inventory(Path(root))
    cached = _PACKAGED_INVENTORY_CACHE.get(INVENTORY_DIR)
    if cached is None:
        cached = _load_connector_inventory(INVENTORY_DIR)
        _PACKAGED_INVENTORY_CACHE[INVENTORY_DIR] = cached
    return copy.deepcopy(cached)


def _load_connector_inventory(directory: Path) -> dict[str, Any]:
    index = validate_connector_inventory_index(
        json.loads((directory / "index.json").read_text(encoding="utf-8"))
    )
    result: dict[str, Any] = {"index": index, "templates": {}, "fixtures": {}, "proposals": {}}
    directory_files = {
        name: sorted((directory / name).glob("*.json"))
        for name in ("profiles", "fixtures", "proposals")
    }
    if any(len(paths) != len(PROFILE_DEFINITIONS) for paths in directory_files.values()):
        raise ConnectorInventoryError("packaged inventory file set is partial or contains extras")
    used: dict[str, set[Path]] = {name: set() for name in directory_files}
    for entry in index["profiles"]:
        slug = entry["connector_ref"].split(":", 1)[1].replace(":", "-")
        # File names are resolved from immutable refs, never from external input.
        matches = [
            candidate for candidate in (directory / "profiles").glob("*.json")
            if json.loads(candidate.read_text(encoding="utf-8"))["connector_ref"] == entry["connector_ref"]
        ]
        if len(matches) != 1:
            raise ConnectorInventoryError(f"profile file resolution failed for {slug}")
        profile = validate_connector_profile_template(json.loads(matches[0].read_text(encoding="utf-8")))
        file_slug = matches[0].stem
        fixture = validate_connector_fixture_manifest(
            json.loads((directory / "fixtures" / f"{file_slug}.json").read_text(encoding="utf-8"))
        )
        proposal = validate_connector_proposal_manifest(
            json.loads((directory / "proposals" / f"{file_slug}.json").read_text(encoding="utf-8"))
        )
        _validate_inventory_graph_entry(entry, profile, fixture, proposal)
        used["profiles"].add(matches[0])
        used["fixtures"].add(directory / "fixtures" / f"{file_slug}.json")
        used["proposals"].add(directory / "proposals" / f"{file_slug}.json")
        result["templates"][file_slug] = profile
        result["fixtures"][file_slug] = fixture
        result["proposals"][file_slug] = proposal
    if len(result["templates"]) != len(PROFILE_DEFINITIONS):
        raise ConnectorInventoryError("packaged connector inventory is incomplete")
    if any(used[name] != set(paths) for name, paths in directory_files.items()):
        raise ConnectorInventoryError("packaged inventory contains an unbound file")
    if result != build_connector_inventory():
        raise ConnectorInventoryError(
            "packaged inventory differs from the deterministic frozen build"
        )
    return result


__all__ = [
    "ConnectorInventoryError", "PROFILE_DEFINITIONS", "build_connector_inventory",
    "load_connector_proposal_package", "load_packaged_connector_inventory",
    "validate_connector_fixture_manifest",
    "validate_connector_inventory_index", "validate_connector_profile_template",
]
