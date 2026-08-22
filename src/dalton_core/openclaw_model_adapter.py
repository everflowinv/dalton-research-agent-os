"""Strict external adapter for the Dalton OpenClaw model broker.

The adapter is deliberately a narrow authority boundary.  Callers provide a
``WorkOrder``, an accepted immutable model-route decision, and the exact
profile version selected by that decision.  They cannot choose an OpenClaw
agent, endpoint, header, credential, auth profile, or fallback model.

The broker is reached through one local Unix-domain socket request/response.
This module creates Core contracts in memory; it never writes Dalton state.
Provider-reported cost remains uncommitted telemetry for a later Usage/Cost
authority to adjudicate.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import socket
import stat
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Literal

from .contracts import (
    InvocationGranularity,
    ModelInvocation,
    ResultEnvelope,
    WorkOrder,
)
from .model_router import (
    ModelRouterValidationError,
    _profile_wire,
    canonical_hash as _dalton_hash,
    canonical_json as _dalton_json,
)


PROTOCOL_VERSION = "0.1"
BROKER_AGENT_ID = "dalton-model-broker"
BROKER_ACTOR_REF = "runtime:openclaw-model-broker"
PROVIDER_CONTROL_MODE_REQUIRED = "provider-controlled-v1"
PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC = "calibration-posthoc-v1"
ProviderControlMode = Literal[
    "provider-controlled-v1",
    "calibration-posthoc-v1",
]
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_BROKER_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_CLIENT_ID_RE = re.compile(r"^client:[A-Za-z0-9._-]+$")
_AUTH_SECRET_RE = re.compile(br"^[0-9a-f]{64}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_PROFILE_ID_RE = re.compile(r"^profile:[A-Za-z0-9._-]+$")
_WORK_ID_RE = re.compile(r"^work:[A-Za-z0-9._-]+$")
_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+._-]*:[^\s]+$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*$")
_ESTIMATED_COST_RE = re.compile(r"^[0-9]+\.[0-9]{6}$")
_SAFE_MODEL_RE = re.compile(
    r"^[A-Za-z0-9._-]+/[A-Za-z0-9][A-Za-z0-9._:/-]*$"
)
_CORE_REQUEST_KEYS = frozenset(
    {
        "schemaVersion",
        "invocationId",
        "workOrderId",
        "profileId",
        "model",
        "prompt",
        "maxTokens",
        "timeoutMs",
    }
)
_AUTH_KEYS = frozenset({"scheme", "clientId", "timestampMs", "nonce", "mac"})
_REQUEST_KEYS = _CORE_REQUEST_KEYS | {"auth"}
_RESPONSE_KEYS = frozenset(
    {
        "schemaVersion",
        "brokerVersion",
        "runtimeVersion",
        "invocationId",
        "workOrderId",
        "profileId",
        "requestHash",
        "idempotencyStatus",
        "ok",
        "provider",
        "model",
        "canonicalModel",
        "agentId",
        "text",
        "usage",
        "cost",
        "error",
        "contentHash",
    }
)
_USAGE_KEYS = frozenset(
    {
        "inputTokens",
        "outputTokens",
        "cacheReadTokens",
        "cacheWriteTokens",
        "totalTokens",
    }
)


class OpenClawModelAdapterError(RuntimeError):
    """Base class for admission, broker protocol, and budget failures."""


class ModelAdmissionError(OpenClawModelAdapterError):
    """The WorkOrder, route decision, or profile does not form an admitted route."""


class RouteAuthorityError(ModelAdmissionError):
    """The supplied route is missing from or differs from Router authority."""


class BrokerProtocolError(OpenClawModelAdapterError):
    """The broker response is malformed, unbound, or inconsistently attributed."""


class BrokerConnectionError(OpenClawModelAdapterError):
    """The configured local broker socket could not be used safely."""


class BrokerTimeout(BrokerConnectionError):
    """The one-shot broker exchange exceeded its trusted wall-clock budget."""


class BrokerFrameTooLarge(BrokerProtocolError):
    """A request or response exceeded the configured frame limit."""


class BrokerBudgetExceeded(OpenClawModelAdapterError):
    """Provider telemetry exceeds a WorkOrder or endpoint-profile limit."""


class BrokerIdempotencyConflict(BrokerProtocolError):
    """The broker has already bound the derived invocation id to other input."""


def _javascript_number(value: int | float) -> str:
    """Render a JSON number like JavaScript's ``JSON.stringify``.

    Broker hashes are produced by Node, whose integer-valued floats and
    exponent thresholds differ from Python's JSON encoder.  Python's float
    repr already supplies the shortest round-trippable digits; this function
    only applies ECMAScript's fixed/scientific presentation rules.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BrokerProtocolError("broker canonical JSON contains an invalid number")
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise BrokerProtocolError("broker canonical JSON contains a non-finite number")
    if value == 0:
        return "0"
    raw = repr(value).lower()
    absolute = abs(value)
    if 1e-6 <= absolute < 1e21:
        fixed = format(Decimal(raw), "f")
        if "." in fixed:
            fixed = fixed.rstrip("0").rstrip(".")
        return fixed
    if "e" not in raw:
        raw = format(Decimal(raw).normalize(), "e")
    mantissa, exponent = raw.split("e", 1)
    if mantissa.endswith(".0"):
        mantissa = mantissa[:-2]
    exponent_value = int(exponent)
    sign = "+" if exponent_value >= 0 else "-"
    return f"{mantissa}e{sign}{abs(exponent_value)}"


def _json_string(value: str) -> str:
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise BrokerProtocolError(
            "broker canonical JSON contains an invalid Unicode surrogate"
        ) from exc
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def canonical_json(value: Any) -> str:
    """Return the broker's Node-compatible recursively sorted JSON form."""

    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _javascript_number(value)
    if isinstance(value, str):
        return _json_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise BrokerProtocolError("broker canonical JSON object keys must be strings")
        for key in value:
            _json_string(key)
        return "{" + ",".join(
            f"{_json_string(key)}:{canonical_json(value[key])}"
            for key in sorted(value)
        ) + "}"
    raise BrokerProtocolError("value is not broker canonical JSON")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def owner_only_secret_file_provider(
    secret_path: str | os.PathLike[str],
) -> Callable[[], bytes]:
    """Build a key provider for one controller-supplied owner-only file.

    The caller supplies the exact path; this function never searches OpenClaw
    configuration or state.  Every read rechecks type, ownership, permissions,
    size, and secret shape without following symlinks.
    """

    path = Path(secret_path)
    if not path.is_absolute():
        raise ValueError("secret_path must be absolute")

    def provide() -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ModelAdmissionError("broker authentication key is unavailable") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ModelAdmissionError("broker authentication key must be a regular file")
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ModelAdmissionError(
                    "broker authentication key must be owner-only and owned by this process"
                )
            raw = os.read(descriptor, 257)
            if len(raw) > 256:
                raise ModelAdmissionError("broker authentication key file is oversized")
        except OSError as exc:
            raise ModelAdmissionError("broker authentication key could not be read") from exc
        finally:
            os.close(descriptor)
        secret = raw.strip()
        if not _AUTH_SECRET_RE.fullmatch(secret):
            raise ModelAdmissionError("broker authentication key has an invalid shape")
        return secret

    return provide


def _closed(
    value: Any,
    *,
    keys: frozenset[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BrokerProtocolError(f"{label} must be an object")
    if set(value) != set(keys):
        raise BrokerProtocolError(f"{label} has an unexpected shape")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ModelAdmissionError(f"{label} must be a positive integer")
    return value


def _nonnegative_number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
    ):
        raise BrokerProtocolError(f"{label} must be a finite non-negative number")
    return float(value)


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ModelAdmissionError(f"{label} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelAdmissionError(f"{label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ModelAdmissionError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ModelAdmissionError("adapter clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _work_order(value: WorkOrder | Mapping[str, Any]) -> WorkOrder:
    if isinstance(value, WorkOrder):
        return value
    if not isinstance(value, Mapping):
        raise ModelAdmissionError("work_order must be a WorkOrder or mapping")
    try:
        return WorkOrder.from_dict(value)
    except Exception as exc:
        raise ModelAdmissionError("work_order is not a valid frozen contract") from exc


def _hash_field(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise ModelAdmissionError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _validate_route(
    route: Mapping[str, Any], work: WorkOrder, profile: Mapping[str, Any], now: datetime
) -> dict[str, Any]:
    keys = frozenset(
        {
            "schema_version",
            "id",
            "created_at",
            "decision_kind",
            "outcome",
            "work_order_ref",
            "work_order_hash",
            "attempt_number",
            "capability",
            "policy_version_ref",
            "policy_hash",
            "candidate_snapshot_hash",
            "candidate_snapshot",
            "constraints",
            "selected_profile_version_ref",
            "selected_profile_hash",
            "selected_endpoint",
            "previous_decision_ref",
            "rejection_reasons",
            "request_hash",
            "content_hash",
        }
    )
    if not isinstance(route, Mapping) or set(route) != set(keys):
        raise ModelAdmissionError("route decision has an unexpected shape")
    wire = dict(route)
    if wire["schema_version"] != PROTOCOL_VERSION or wire["outcome"] != "selected":
        raise ModelAdmissionError("route decision is not an accepted v0.1 route")
    if (
        not isinstance(wire["id"], str)
        or not _REF_RE.fullmatch(wire["id"])
        or wire["decision_kind"] not in {"initial", "retry", "switch"}
    ):
        raise ModelAdmissionError("route decision identity or kind is invalid")
    _parse_time(wire["created_at"], "route created_at")
    asserted_hash = _hash_field(wire["content_hash"], "route content_hash")
    unhashed = dict(wire)
    del unhashed["content_hash"]
    if _dalton_hash(unhashed) != asserted_hash:
        raise ModelAdmissionError("route decision content_hash mismatch")
    if wire["work_order_ref"] != work.id:
        raise ModelAdmissionError("route decision belongs to another WorkOrder")
    if wire["work_order_hash"] != _dalton_hash(work.to_dict()):
        raise ModelAdmissionError("route decision WorkOrder hash mismatch")
    if (
        isinstance(wire["attempt_number"], bool)
        or not isinstance(wire["attempt_number"], int)
        or wire["attempt_number"] < 1
    ):
        raise ModelAdmissionError("route attempt_number is invalid")
    capability = wire["capability"]
    if not isinstance(capability, str) or capability not in work.requested_capabilities:
        raise ModelAdmissionError("route capability was not declared by the WorkOrder")
    for label in ("policy_hash", "request_hash", "candidate_snapshot_hash"):
        _hash_field(wire[label], f"route {label}")
    if not isinstance(wire["policy_version_ref"], str) or not _REF_RE.fullmatch(
        wire["policy_version_ref"]
    ):
        raise ModelAdmissionError("route policy_version_ref is invalid")
    if wire["previous_decision_ref"] is not None and (
        not isinstance(wire["previous_decision_ref"], str)
        or not _REF_RE.fullmatch(wire["previous_decision_ref"])
    ):
        raise ModelAdmissionError("route previous_decision_ref is invalid")
    if wire["rejection_reasons"] != []:
        raise ModelAdmissionError("accepted route must not retain rejection reasons")
    snapshot = wire["candidate_snapshot"]
    if not isinstance(snapshot, list) or not snapshot:
        raise ModelAdmissionError("route candidate_snapshot must be an array")
    if _dalton_hash(snapshot) != wire["candidate_snapshot_hash"]:
        raise ModelAdmissionError("route candidate snapshot hash mismatch")
    seen_profiles: set[str] = set()
    for candidate in snapshot:
        if not isinstance(candidate, Mapping) or set(candidate) != {
            "profile_version_ref",
            "profile_hash",
            "eligible",
            "rejection_reasons",
            "estimated_cost_usd",
        }:
            raise ModelAdmissionError("route candidate has an unexpected shape")
        candidate_ref = candidate["profile_version_ref"]
        if (
            not isinstance(candidate_ref, str)
            or not _REF_RE.fullmatch(candidate_ref)
            or candidate_ref in seen_profiles
        ):
            raise ModelAdmissionError("route candidate profile reference is invalid")
        seen_profiles.add(candidate_ref)
        _hash_field(candidate["profile_hash"], "route candidate profile_hash")
        if not isinstance(candidate["eligible"], bool):
            raise ModelAdmissionError("route candidate eligible must be boolean")
        reasons = candidate["rejection_reasons"]
        if (
            not isinstance(reasons, list)
            or len(set(reasons)) != len(reasons)
            or not all(isinstance(item, str) and item for item in reasons)
        ):
            raise ModelAdmissionError("route candidate rejection_reasons are invalid")
        if candidate["eligible"] == bool(reasons):
            raise ModelAdmissionError("route candidate eligibility contradicts rejection reasons")
        if not isinstance(candidate["estimated_cost_usd"], str) or not _ESTIMATED_COST_RE.fullmatch(
            candidate["estimated_cost_usd"]
        ):
            raise ModelAdmissionError("route candidate estimated cost is invalid")
    profile_ref = profile["profile_version_ref"]
    profile_hash = profile["content_hash"]
    if (
        wire["selected_profile_version_ref"] != profile_ref
        or wire["selected_profile_hash"] != profile_hash
    ):
        raise ModelAdmissionError("route selected profile does not match exact profile")
    expected_endpoint = {
        "provider": profile["provider"],
        "model": profile["model"],
        "family": profile["family"],
        "adapter_ref": profile["adapter_ref"],
        "credential_slot_ref": profile["credential_slot_ref"],
    }
    if wire["selected_endpoint"] != expected_endpoint:
        raise ModelAdmissionError("route selected endpoint does not match exact profile")
    selected = [
        item
        for item in snapshot
        if isinstance(item, Mapping)
        and item.get("profile_version_ref") == profile_ref
    ]
    if len(selected) != 1:
        raise ModelAdmissionError("route snapshot does not uniquely bind the selected profile")
    candidate = selected[0]
    if (
        set(candidate)
        != {
            "profile_version_ref",
            "profile_hash",
            "eligible",
            "rejection_reasons",
            "estimated_cost_usd",
        }
        or candidate["profile_hash"] != profile_hash
        or candidate["eligible"] is not True
        or candidate["rejection_reasons"] != []
    ):
        raise ModelAdmissionError("selected route candidate is not eligible and hash-bound")
    constraints = wire["constraints"]
    expected_constraint_keys = {
        "credential_slot_refs",
        "required_modalities",
        "required_context_tokens",
        "estimated_input_tokens",
        "estimated_output_tokens",
        "producer_family",
    }
    if not isinstance(constraints, Mapping) or set(constraints) != expected_constraint_keys:
        raise ModelAdmissionError("route constraints have an unexpected shape")
    slots = constraints["credential_slot_refs"]
    if (
        not isinstance(slots, list)
        or len(set(slots)) != len(slots)
        or not all(isinstance(item, str) and item for item in slots)
        or profile["credential_slot_ref"] not in slots
    ):
        raise ModelAdmissionError("selected route credential slot is unavailable")
    modalities = constraints["required_modalities"]
    if (
        not isinstance(modalities, list)
        or not modalities
        or len(set(modalities)) != len(modalities)
        or not all(isinstance(item, str) and _TOKEN_RE.fullmatch(item) for item in modalities)
        or not set(modalities).issubset(profile["modalities"])
    ):
        raise ModelAdmissionError("route required modalities are not admitted")
    for field in (
        "required_context_tokens",
        "estimated_input_tokens",
        "estimated_output_tokens",
    ):
        _positive_int(constraints[field], f"route constraints {field}")
    if constraints["required_context_tokens"] < constraints["estimated_input_tokens"]:
        raise ModelAdmissionError("route context is smaller than estimated input")
    if capability not in profile["capabilities"]:
        raise ModelAdmissionError("exact profile does not support the routed capability")
    producer_family = constraints["producer_family"]
    if producer_family is not None and (
        not isinstance(producer_family, str) or not _TOKEN_RE.fullmatch(producer_family)
    ):
        raise ModelAdmissionError("route producer_family is invalid")
    if producer_family is not None and producer_family == profile["family"]:
        raise ModelAdmissionError("verifier route is not model-family independent")
    availability = profile["availability"]
    if availability["state"] != "available":
        raise ModelAdmissionError("exact profile is not available")
    now = now.astimezone(timezone.utc)
    if _parse_time(availability["checked_at"], "profile checked_at") > now:
        raise ModelAdmissionError("profile availability check is in the future")
    if _parse_time(availability["valid_until"], "profile valid_until") <= now:
        raise ModelAdmissionError("profile availability has expired")
    return wire


def _budget(work: WorkOrder, profile: Mapping[str, Any]) -> tuple[int, float]:
    required = {
        "max_input_tokens",
        "max_output_tokens",
        "max_total_tokens",
        "max_cost_usd",
    }
    if not isinstance(work.budget, Mapping) or not required.issubset(work.budget):
        raise ModelAdmissionError("WorkOrder lacks the frozen model budget fields")
    for key in ("max_input_tokens", "max_output_tokens", "max_total_tokens"):
        _positive_int(work.budget[key], f"WorkOrder budget {key}")
    cost = work.budget["max_cost_usd"]
    if (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(float(cost))
        or cost <= 0
    ):
        raise ModelAdmissionError("WorkOrder budget max_cost_usd must be positive")
    max_tokens = min(
        work.budget["max_output_tokens"],
        profile["context"]["max_output_tokens"],
        profile["limits"]["max_output_tokens"],
    )
    if max_tokens < 1:
        raise ModelAdmissionError("effective output token budget is empty")
    return int(max_tokens), float(cost)


def _required_provider_controls(
    work: WorkOrder,
    route: Mapping[str, Any],
    max_output_tokens: int,
    provider_control_mode: ProviderControlMode,
) -> dict[str, Any] | None:
    """Bind independent verification to controls the current host must prove.

    General model work keeps the legacy completion path.  A route with a
    producer family is an independent verifier and may not silently degrade to
    prompt-only JSON or post-hoc budget checks.
    """

    if route["constraints"]["producer_family"] is None:
        return None
    if work.metadata.get("verifier_output_schema_version") != "0.2":
        raise ModelAdmissionError(
            "independent verifier WorkOrder lacks the required output schema version"
        )
    if provider_control_mode == PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC:
        if (
            work.metadata.get("phase") != "verification-calibration"
            or work.metadata.get("execution_tier")
            != PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC
        ):
            raise ModelAdmissionError(
                "post-hoc provider controls are restricted to exact calibration WorkOrders"
            )
        return None
    try:
        schema = json.loads(
            resources.files("dalton_core")
            .joinpath("thesis-impact-verifier-output-v0.2.schema.json")
            .read_text(encoding="utf-8")
        )
    except (OSError, TypeError, ValueError) as exc:
        raise ModelAdmissionError(
            "independent verifier output schema resource is unavailable"
        ) from exc
    if not isinstance(schema, Mapping):
        raise ModelAdmissionError("independent verifier output schema is invalid")
    max_input_tokens = _positive_int(
        work.budget["max_input_tokens"], "WorkOrder budget max_input_tokens"
    )
    max_total_tokens = _positive_int(
        work.budget["max_total_tokens"], "WorkOrder budget max_total_tokens"
    )
    if max_total_tokens < max_input_tokens + max_output_tokens:
        raise ModelAdmissionError(
            "WorkOrder total token budget is smaller than required input plus output controls"
        )
    max_cost_usd = float(work.budget["max_cost_usd"])
    return {
        "maxInputTokens": max_input_tokens,
        "maxOutputTokens": max_output_tokens,
        "maxTotalTokens": max_total_tokens,
        "maxCostUsd": max_cost_usd,
        "structuredOutput": {
            "schemaName": "thesis_impact_verifier_output_v0_2",
            "schemaHash": _dalton_hash(schema),
            "jsonSchema": dict(schema),
        },
    }


def _remaining_timeout(
    deadline: float, monotonic: Callable[[], float]
) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise BrokerTimeout("broker exchange exceeded its wall-clock budget")
    return remaining


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BrokerProtocolError("broker response contains a duplicate JSON key")
        result[key] = value
    return result


class OpenClawModelAdapter:
    """Call the installed local broker without exposing its host authorities."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        *,
        route_resolver: Callable[[str], Mapping[str, Any] | None],
        auth_client_id: str,
        auth_key_provider: Callable[[], bytes],
        timeout_seconds: float = 5.0,
        max_frame_bytes: int = 262_144,
        expected_agent_id: str = BROKER_AGENT_ID,
        provider_control_mode: ProviderControlMode = PROVIDER_CONTROL_MODE_REQUIRED,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        path = Path(socket_path)
        if not path.is_absolute():
            raise ValueError("socket_path must be absolute")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive and finite")
        if (
            isinstance(max_frame_bytes, bool)
            or not isinstance(max_frame_bytes, int)
            or max_frame_bytes < 1024
        ):
            raise ValueError("max_frame_bytes must be an integer of at least 1024")
        if not isinstance(expected_agent_id, str) or not _AGENT_ID_RE.fullmatch(
            expected_agent_id
        ):
            raise ValueError("expected_agent_id must be a canonical dedicated agent id")
        if not callable(route_resolver):
            raise ValueError("route_resolver must be a trusted read-only lookup")
        if not isinstance(auth_client_id, str) or not _CLIENT_ID_RE.fullmatch(
            auth_client_id
        ):
            raise ValueError("auth_client_id must be a canonical client reference")
        if not callable(auth_key_provider):
            raise ValueError("auth_key_provider must be a trusted secret-bytes provider")
        if provider_control_mode not in {
            PROVIDER_CONTROL_MODE_REQUIRED,
            PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC,
        }:
            raise ValueError("provider_control_mode is unsupported")
        self._socket_path = path
        self._timeout_seconds = float(timeout_seconds)
        self._max_frame_bytes = max_frame_bytes
        self._expected_agent_id = expected_agent_id
        self._route_resolver = route_resolver
        self._auth_client_id = auth_client_id
        self._auth_key_provider = auth_key_provider
        self._provider_control_mode = provider_control_mode
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic

    def _resolve_authoritative_route(
        self, supplied: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Bind an untrusted route argument to the Router's immutable wire.

        The resolver is installed when the adapter is assembled, outside the
        per-invocation caller boundary.  It exposes only lookup-by-id; this
        adapter neither receives nor opens a Router database path.
        """

        if not isinstance(supplied, Mapping):
            raise RouteAuthorityError("route decision must be an object")
        decision_id = supplied.get("id")
        if not isinstance(decision_id, str) or not _REF_RE.fullmatch(decision_id):
            raise RouteAuthorityError("route decision id is invalid")
        try:
            authoritative = self._route_resolver(decision_id)
        except Exception as exc:
            raise RouteAuthorityError(
                "route decision is missing from Router authority"
            ) from exc
        if authoritative is None:
            raise RouteAuthorityError("route decision is missing from Router authority")
        if not isinstance(authoritative, Mapping):
            raise RouteAuthorityError("Router authority returned an invalid route wire")
        try:
            supplied_canonical = _dalton_json(supplied)
            authoritative_canonical = _dalton_json(authoritative)
        except (TypeError, ValueError) as exc:
            raise RouteAuthorityError("route decision is not canonical JSON") from exc
        if supplied_canonical != authoritative_canonical:
            raise RouteAuthorityError(
                "route decision differs from Router authority"
            )
        # Continue with a detached JSON copy so a mutable resolver result
        # cannot change between authority comparison and broker admission.
        return json.loads(authoritative_canonical)

    def _authenticate_request(
        self,
        core_request: Mapping[str, Any],
        timestamp_ms: int,
        *,
        replay_only: bool = False,
    ) -> dict[str, Any]:
        provider_failed = False
        try:
            secret = self._auth_key_provider()
        except Exception:
            # Raise outside the except block so even ``__context__`` cannot
            # retain a provider exception containing secret material.
            provider_failed = True
            secret = b""
        if provider_failed:
            raise ModelAdmissionError(
                "trusted broker authentication key provider failed"
            )
        if not isinstance(secret, bytes) or not _AUTH_SECRET_RE.fullmatch(secret):
            raise ModelAdmissionError(
                "trusted broker authentication key provider returned invalid bytes"
            )
        nonce = secrets.token_hex(16)
        if not _NONCE_RE.fullmatch(nonce):  # defensive assertion around stdlib CSPRNG
            raise ModelAdmissionError("could not create broker authentication nonce")
        request = dict(core_request)
        if replay_only:
            request["replayOnly"] = True
        request["auth"] = {
            "scheme": "hmac-sha256-v1",
            "clientId": self._auth_client_id,
            "timestampMs": timestamp_ms,
            "nonce": nonce,
            "mac": "0" * 64,
        }
        unsigned = dict(request)
        unsigned_auth = dict(request["auth"])
        del unsigned_auth["mac"]
        unsigned["auth"] = unsigned_auth
        request["auth"]["mac"] = hmac.new(
            secret,
            canonical_json(unsigned).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return request

    def _assert_safe_socket(self) -> None:
        try:
            info = self._socket_path.lstat()
        except OSError as exc:
            raise BrokerConnectionError("broker socket is unavailable") from exc
        if not stat.S_ISSOCK(info.st_mode):
            raise BrokerConnectionError("broker endpoint is not a Unix-domain socket")
        if info.st_uid != os.geteuid():
            raise BrokerConnectionError("broker socket is owned by another OS identity")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise BrokerConnectionError("broker socket permissions must not grant group/world access")

    def _exchange(self, request: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
        frame = canonical_json(request).encode("utf-8") + b"\n"
        if len(frame) > self._max_frame_bytes:
            raise BrokerFrameTooLarge("broker request exceeds max_frame_bytes")
        self._assert_safe_socket()
        deadline = self._monotonic() + timeout
        response = bytearray()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(_remaining_timeout(deadline, self._monotonic))
            client.connect(os.fspath(self._socket_path))
            client.settimeout(_remaining_timeout(deadline, self._monotonic))
            client.sendall(frame)
            while True:
                client.settimeout(_remaining_timeout(deadline, self._monotonic))
                chunk = client.recv(min(16_384, self._max_frame_bytes + 1 - len(response)))
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > self._max_frame_bytes:
                    raise BrokerFrameTooLarge("broker response exceeds max_frame_bytes")
        except socket.timeout as exc:
            raise BrokerTimeout("broker exchange exceeded its wall-clock budget") from exc
        except BrokerProtocolError:
            raise
        except OSError as exc:
            raise BrokerConnectionError("broker socket exchange failed") from exc
        finally:
            client.close()
        if not response or response[-1:] != b"\n" or response.count(b"\n") != 1:
            raise BrokerProtocolError("broker must return exactly one non-empty JSONL frame")
        raw = bytes(response[:-1])
        if not raw:
            raise BrokerProtocolError("broker returned an empty JSONL frame")
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BrokerProtocolError("broker response is not UTF-8") from exc
        try:
            value = json.loads(
                decoded,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    BrokerProtocolError(f"broker response contains {value}")
                ),
            )
        except BrokerProtocolError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise BrokerProtocolError("broker response is not valid strict JSON") from exc
        return _closed(value, keys=_RESPONSE_KEYS, label="broker response")

    @staticmethod
    def _validate_response(
        response: Mapping[str, Any],
        request: Mapping[str, Any],
        profile: Mapping[str, Any],
        expected_agent_id: str,
    ) -> tuple[dict[str, int | None], dict[str, Any]]:
        content_hash = response["contentHash"]
        if not isinstance(content_hash, str) or not _HASH_RE.fullmatch(content_hash):
            raise BrokerProtocolError("broker contentHash is invalid")
        unhashed = dict(response)
        del unhashed["contentHash"]
        if canonical_hash(unhashed) != content_hash:
            raise BrokerProtocolError("broker contentHash mismatch")
        if response["schemaVersion"] != PROTOCOL_VERSION:
            raise BrokerProtocolError("broker schemaVersion is unsupported")
        if not isinstance(response["brokerVersion"], str) or not _BROKER_VERSION_RE.fullmatch(
            response["brokerVersion"]
        ):
            raise BrokerProtocolError("brokerVersion is invalid")
        if not isinstance(response["runtimeVersion"], str) or not response["runtimeVersion"]:
            raise BrokerProtocolError("runtimeVersion is invalid")
        for response_key, request_key in (
            ("invocationId", "invocationId"),
            ("workOrderId", "workOrderId"),
            ("profileId", "profileId"),
        ):
            if response[response_key] != request[request_key]:
                raise BrokerProtocolError(f"broker {response_key} is not bound to the request")
        expected_request_hash = canonical_hash(request)
        if response["requestHash"] != expected_request_hash:
            raise BrokerProtocolError("broker requestHash mismatch")
        status = response["idempotencyStatus"]
        if status not in {"fresh", "duplicate", "conflict"}:
            raise BrokerProtocolError("broker idempotencyStatus is invalid")
        if status == "conflict":
            raise BrokerIdempotencyConflict("broker rejected invocation id reuse")
        if not isinstance(response["ok"], bool):
            raise BrokerProtocolError("broker ok must be boolean")
        usage_obj = _closed(response["usage"], keys=_USAGE_KEYS, label="broker usage")
        usage: dict[str, int | None] = {}
        for key in _USAGE_KEYS:
            value = usage_obj[key]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise BrokerProtocolError(f"broker usage {key} must be integer or null")
            usage[key] = value
        cost_obj = _closed(
            response["cost"], keys=frozenset({"available", "usd"}), label="broker cost"
        )
        if not isinstance(cost_obj["available"], bool):
            raise BrokerProtocolError("broker cost.available must be boolean")
        usd = cost_obj["usd"]
        if cost_obj["available"]:
            _nonnegative_number(usd, "broker cost.usd")
        elif usd is not None:
            raise BrokerProtocolError("unavailable broker cost must have null usd")
        if response["ok"]:
            if response["provider"] != profile["provider"]:
                raise BrokerProtocolError("broker actual provider differs from accepted route")
            canonical_model = f"{profile['provider']}/{profile['model']}"
            if response["canonicalModel"] != canonical_model:
                raise BrokerProtocolError("broker canonical model differs from accepted route")
            if response["model"] not in {profile["model"], canonical_model}:
                raise BrokerProtocolError("broker actual model differs from accepted route")
            if response["agentId"] != expected_agent_id:
                raise BrokerProtocolError("broker actual agent differs from dedicated agent")
            if not isinstance(response["text"], str) or not response["text"]:
                raise BrokerProtocolError("successful broker response has no text")
            if response["error"] is not None:
                raise BrokerProtocolError("successful broker response must not include error")
        else:
            if any(
                response[key] is not None
                for key in ("provider", "model", "canonicalModel", "agentId", "text")
            ):
                raise BrokerProtocolError("failed broker response must not claim output attribution")
            if any(value is not None for value in usage.values()):
                raise BrokerProtocolError("failed broker response must have unknown usage")
            if cost_obj != {"available": False, "usd": None}:
                raise BrokerProtocolError("failed broker response must have unavailable cost")
            error = _closed(
                response["error"],
                keys=frozenset({"code", "message"}),
                label="broker error",
            )
            if not isinstance(error["code"], str) or not _ERROR_CODE_RE.fullmatch(error["code"]):
                raise BrokerProtocolError("broker error code is invalid")
            if (
                not isinstance(error["message"], str)
                or not error["message"]
                or len(error["message"]) > 512
                or error["message"] == request["prompt"]
                or (
                    len(request["prompt"]) >= 16
                    and request["prompt"] in error["message"]
                )
            ):
                raise BrokerProtocolError("broker error message is unsafe")
        return usage, dict(cost_obj)

    @staticmethod
    def _assert_budget(
        usage: Mapping[str, int | None],
        cost: Mapping[str, Any],
        work: WorkOrder,
        profile: Mapping[str, Any],
        max_tokens: int,
    ) -> None:
        input_tokens = usage["inputTokens"]
        output_tokens = usage["outputTokens"]
        total_tokens = usage["totalTokens"]
        known_io = sum(value or 0 for value in (input_tokens, output_tokens))
        if total_tokens is not None and total_tokens < known_io:
            raise BrokerProtocolError("broker totalTokens is smaller than known input/output")
        checks = (
            (input_tokens, "max_input_tokens"),
            (output_tokens, "max_output_tokens"),
            (total_tokens, "max_total_tokens"),
        )
        for used, limit_name in checks:
            if used is None:
                continue
            for limits, source in ((work.budget, "WorkOrder"), (profile["limits"], "profile")):
                if used > limits[limit_name]:
                    raise BrokerBudgetExceeded(
                        f"provider {limit_name} telemetry exceeds {source} budget"
                    )
        if output_tokens is not None and output_tokens > max_tokens:
            raise BrokerBudgetExceeded("provider output usage exceeds requested maxTokens")
        if cost["available"]:
            usd = float(cost["usd"])
            if usd > float(work.budget["max_cost_usd"]):
                raise BrokerBudgetExceeded("provider cost telemetry exceeds WorkOrder budget")
            if usd > float(profile["limits"]["max_cost_usd"]):
                raise BrokerBudgetExceeded("provider cost telemetry exceeds profile budget")

    def execute(
        self,
        work_order: WorkOrder | Mapping[str, Any],
        route_decision: Mapping[str, Any],
        model_profile: Mapping[str, Any],
    ) -> tuple[ModelInvocation, ResultEnvelope]:
        """Run exactly the selected route and return uncommitted Core contracts."""

        return self._execute(
            work_order,
            route_decision,
            model_profile,
            replay_only=False,
        )

    def replay(
        self,
        work_order: WorkOrder | Mapping[str, Any],
        route_decision: Mapping[str, Any],
        model_profile: Mapping[str, Any],
    ) -> tuple[ModelInvocation, ResultEnvelope]:
        """Read only the broker's durable result for the exact invocation.

        A journal miss is a failed broker result and must never call the host
        model.  This path exists only for recovery after a Scheduler lease
        expired without accepting the already-routed attempt.
        """

        return self._execute(
            work_order,
            route_decision,
            model_profile,
            replay_only=True,
        )

    def _execute(
        self,
        work_order: WorkOrder | Mapping[str, Any],
        route_decision: Mapping[str, Any],
        model_profile: Mapping[str, Any],
        *,
        replay_only: bool,
    ) -> tuple[ModelInvocation, ResultEnvelope]:

        work = _work_order(work_order)
        try:
            profile = _profile_wire(model_profile)
        except ModelRouterValidationError as exc:
            raise ModelAdmissionError("model_profile is not a valid exact profile") from exc
        now_dt = self._clock()
        if not isinstance(now_dt, datetime) or now_dt.tzinfo is None:
            raise ModelAdmissionError("adapter clock must return a timezone-aware datetime")
        authoritative_route = self._resolve_authoritative_route(route_decision)
        route = _validate_route(authoritative_route, work, profile, now_dt)
        if not _WORK_ID_RE.fullmatch(work.id):
            raise ModelAdmissionError("WorkOrder id is not accepted by broker protocol")
        if not _PROFILE_ID_RE.fullmatch(profile["id"]):
            raise ModelAdmissionError("selected profile id is not accepted by broker protocol")
        exact_model = f"{profile['provider']}/{profile['model']}"
        if not _SAFE_MODEL_RE.fullmatch(exact_model):
            raise ModelAdmissionError("selected exact provider/model is not broker-safe")
        max_tokens, _ = _budget(work, profile)
        timeout = self._timeout_seconds
        for key in ("max_seconds", "time_limit"):
            if key in work.budget:
                value = work.budget[key]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or value <= 0
                ):
                    raise ModelAdmissionError(f"WorkOrder budget {key} is invalid")
                timeout = min(timeout, float(value))
        required_controls = _required_provider_controls(
            work,
            route,
            max_tokens,
            self._provider_control_mode,
        )
        invocation_identity = {
            "route_decision_ref": route["id"],
            "work_order_ref": work.id,
            "profile_version_ref": profile["profile_version_ref"],
        }
        if required_controls is not None:
            invocation_identity["required_provider_controls_hash"] = _dalton_hash(
                required_controls
            )
        if route["constraints"]["producer_family"] is not None:
            invocation_identity["provider_control_mode"] = self._provider_control_mode
        invocation_id = "invocation:" + _dalton_hash(invocation_identity)[:32]
        core_request = {
            "schemaVersion": PROTOCOL_VERSION,
            "invocationId": invocation_id,
            "workOrderId": work.id,
            "profileId": profile["id"],
            "model": exact_model,
            "prompt": work.question,
            "maxTokens": max_tokens,
            "timeoutMs": max(1, int(timeout * 1000)),
        }
        if required_controls is not None:
            core_request["requiredControls"] = required_controls
        expected_core_keys = set(_CORE_REQUEST_KEYS) | (
            {"requiredControls"} if required_controls is not None else set()
        )
        if set(core_request) != expected_core_keys:  # defensive assertion
            raise AssertionError("internal broker request shape drift")
        timestamp_ms = int(now_dt.astimezone(timezone.utc).timestamp() * 1000)
        request = self._authenticate_request(
            core_request, timestamp_ms, replay_only=replay_only
        )
        expected_request_keys = set(_REQUEST_KEYS)
        if replay_only:
            expected_request_keys.add("replayOnly")
        if required_controls is not None:
            expected_request_keys.add("requiredControls")
        if set(request) != expected_request_keys:  # defensive assertion
            raise AssertionError("internal authenticated broker request shape drift")
        started_at = _timestamp(now_dt)
        response = self._exchange(request, timeout)
        usage, cost = self._validate_response(
            response, core_request, profile, self._expected_agent_id
        )
        if (
            replay_only
            and response["ok"] is True
            and response["idempotencyStatus"] != "duplicate"
        ):
            raise BrokerProtocolError(
                "replay-only broker success must be a durable duplicate"
            )
        budget_error: BrokerBudgetExceeded | None = None
        try:
            self._assert_budget(usage, cost, work, profile, max_tokens)
        except BrokerBudgetExceeded as exc:
            budget_error = exc
        completed_at = _timestamp(self._clock())
        result_id = "result:" + hashlib.sha256(invocation_id.encode("utf-8")).hexdigest()[:32]
        usage_ref = f"usage:{invocation_id}"
        environment_hash = _dalton_hash(
            {
                "adapter_ref": profile["adapter_ref"],
                "broker_version": response["brokerVersion"],
                "runtime_version": response["runtimeVersion"],
            }
        )
        all_unknown = all(value is None for value in usage.values()) and not cost["available"]
        invocation_usage = {
            "input_tokens": usage["inputTokens"],
            "output_tokens": usage["outputTokens"],
            "cache_read_tokens": usage["cacheReadTokens"],
            "cache_write_tokens": usage["cacheWriteTokens"],
            "total_tokens": usage["totalTokens"],
            "raw_provider_telemetry": {
                "cost": dict(cost),
                "broker_version": response["brokerVersion"],
                "runtime_version": response["runtimeVersion"],
                "response_content_hash": response["contentHash"],
            },
            "metering_source": "provider_reported",
            "measurement_status": "unavailable" if all_unknown else "partial",
            "authority_status": "uncommitted",
        }
        granularity = (
            InvocationGranularity.VERIFICATION
            if route["constraints"]["producer_family"] is not None
            else InvocationGranularity.TASK
        )
        invocation = ModelInvocation(
            schema_version=work.schema_version,
            id=invocation_id,
            created_at=completed_at,
            work_order_ref=work.id,
            profile_ref=profile["profile_version_ref"],
            granularity=granularity,
            capability=route["capability"],
            provider=profile["provider"],
            model=profile["model"],
            model_family=profile["family"],
            input_refs=work.input_refs,
            output_refs=(),
            started_at=started_at,
            completed_at=completed_at,
            usage=invocation_usage,
            side_effects=(),
            runtime_ref=profile["adapter_ref"],
            actor_ref=BROKER_ACTOR_REF,
            parent_ref=route["id"],
            environment_hash=environment_hash,
        )
        common_metadata = {
            "route_decision_ref": route["id"],
            "profile_version_ref": profile["profile_version_ref"],
            "broker_version": response["brokerVersion"],
            "runtime_version": response["runtimeVersion"],
            "broker_response_hash": response["contentHash"],
            "broker_idempotency_status": response["idempotencyStatus"],
            "broker_request_mode": "replay_only" if replay_only else "execute",
            "required_provider_controls": required_controls is not None,
            "provider_control_mode": self._provider_control_mode,
            "provider_control_schema_hash": (
                required_controls["structuredOutput"]["schemaHash"]
                if required_controls is not None
                else None
            ),
        }
        if budget_error is not None:
            outputs = {}
            status = "failed"
            error = {
                "code": "PROVIDER_BUDGET_EXCEEDED",
                "message": str(budget_error),
                "source": "openclaw-model-adapter",
            }
        elif response["ok"]:
            outputs: Mapping[str, Any] = {
                "text": response["text"],
                "content_hash": hashlib.sha256(response["text"].encode("utf-8")).hexdigest(),
            }
            status = "succeeded"
            error = None
        else:
            outputs = {}
            status = "failed"
            error = {
                "code": response["error"]["code"],
                "message": response["error"]["message"],
                "source": "openclaw-model-broker",
            }
        result = ResultEnvelope(
            schema_version=work.schema_version,
            id=result_id,
            created_at=completed_at,
            work_order_ref=work.id,
            invocation_ref=invocation.id,
            status=status,
            outputs=outputs,
            actual_side_effects=(),
            usage_refs=(usage_ref,),
            artifact_refs=(),
            error=error,
            metadata=common_metadata,
        )
        return invocation, result


__all__ = [
    "OpenClawModelAdapter",
    "OpenClawModelAdapterError",
    "ModelAdmissionError",
    "RouteAuthorityError",
    "BrokerProtocolError",
    "BrokerConnectionError",
    "BrokerTimeout",
    "BrokerFrameTooLarge",
    "BrokerBudgetExceeded",
    "BrokerIdempotencyConflict",
    "PROVIDER_CONTROL_MODE_REQUIRED",
    "PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC",
    "owner_only_secret_file_provider",
]
