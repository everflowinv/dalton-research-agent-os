"""Closed capability-sandbox attestation contract.

This module verifies evidence emitted by an *external* sandbox runner.  It is
not a sandbox and it never executes capability code.  Authority-bearing data
comes from :class:`TrustedLaunchContext`; the child/worker report is parsed as
an untrusted, closed-shape document and may only supply observations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "0.1"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class AttestationError(ValueError):
    """Base error for malformed or untrusted attestation evidence."""


class AttestationMismatch(AttestationError):
    """The sandbox observation does not match trusted launch state."""


class PermissionViolation(AttestationError):
    """The launch or report contains a forbidden capability permission."""


class ResultStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    LIMIT_EXCEEDED = "limit_exceeded"


class FixtureStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"


def canonical_json(value: Any) -> str:
    """Return the single JSON representation used for attestation hashes."""

    if hasattr(value, "to_dict"):
        value = value.to_dict()
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AttestationError("attestation value must be canonical JSON") from exc


def sha256_digest(value: Any) -> str:
    # Dalton Core's existing proposal, policy, artifact, and content hashes
    # are raw lowercase SHA-256 hex.  Attestations use the same wire format so
    # they can bind Registry rows without a lossy prefix conversion.
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def compute_attestation_id(wire: Mapping[str, Any]) -> str:
    """Recompute the stable id from an attestation wire document."""

    if not isinstance(wire, Mapping):
        raise AttestationError("attestation wire must be an object")
    preimage = {key: value for key, value in wire.items() if key not in {"id", "content_hash"}}
    return "attestation:" + sha256_digest(preimage)


def compute_attestation_hash(wire: Mapping[str, Any]) -> str:
    """Recompute the content hash (which includes the stable id)."""

    if not isinstance(wire, Mapping):
        raise AttestationError("attestation wire must be an object")
    preimage = {key: value for key, value in wire.items() if key != "content_hash"}
    return sha256_digest(preimage)


def _strict(data: Any, required: set[str], name: str) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise AttestationError(f"{name} must be an object")
    unknown = set(data) - required
    missing = required - set(data)
    if unknown:
        raise AttestationError(f"{name}: unknown field(s): {sorted(unknown)}")
    if missing:
        raise AttestationError(f"{name}: missing field(s): {sorted(missing)}")
    return dict(data)


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise AttestationError(f"{name} must be a non-empty string")
    return value


def _hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise AttestationError(f"{name} must be 64 lowercase SHA-256 hex characters")
    return value


def _timestamp(value: Any, name: str) -> str:
    value = _string(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AttestationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise AttestationError(f"{name} must include a timezone")
    return value


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _unique_strings(value: Any, name: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise AttestationError(f"{name} must be an array")
    if not allow_empty and not value:
        raise AttestationError(f"{name} must not be empty")
    items = tuple(_string(item, f"{name} item") for item in value)
    if len(items) != len(set(items)):
        raise AttestationError(f"{name} must not contain duplicates")
    return tuple(sorted(items))


@dataclass(frozen=True, slots=True)
class FixtureExpectation:
    fixture_id: str
    input_hash: str
    output_hash: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FixtureExpectation":
        obj = _strict(data, {"fixture_id", "input_hash", "output_hash"}, "FixtureExpectation")
        return cls(
            fixture_id=_string(obj["fixture_id"], "fixture_id"),
            input_hash=_hash(obj["input_hash"], "input_hash"),
            output_hash=_hash(obj["output_hash"], "output_hash"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"fixture_id": self.fixture_id, "input_hash": self.input_hash, "output_hash": self.output_hash}


@dataclass(frozen=True, slots=True)
class FixtureResult:
    fixture_id: str
    input_hash: str
    output_hash: str
    status: FixtureStatus

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FixtureResult":
        obj = _strict(data, {"fixture_id", "input_hash", "output_hash", "status"}, "FixtureResult")
        try:
            status = FixtureStatus(obj["status"])
        except (TypeError, ValueError) as exc:
            raise AttestationError("FixtureResult.status is invalid") from exc
        return cls(
            fixture_id=_string(obj["fixture_id"], "fixture_id"),
            input_hash=_hash(obj["input_hash"], "input_hash"),
            output_hash=_hash(obj["output_hash"], "output_hash"),
            status=status,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture_id": self.fixture_id,
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "status": self.status.value,
        }


def _fixture_expectations(value: Any) -> tuple[FixtureExpectation, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise AttestationError("fixtures must be a non-empty array")
    fixtures = tuple(FixtureExpectation.from_dict(item) for item in value)
    ids = [item.fixture_id for item in fixtures]
    if len(ids) != len(set(ids)):
        raise AttestationError("fixtures must not contain duplicate fixture_id values")
    return tuple(sorted(fixtures, key=lambda item: item.fixture_id))


def _fixture_results(value: Any) -> tuple[FixtureResult, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise AttestationError("fixtures must be a non-empty array")
    fixtures = tuple(FixtureResult.from_dict(item) for item in value)
    ids = [item.fixture_id for item in fixtures]
    if len(ids) != len(set(ids)):
        raise AttestationError("fixtures must not contain duplicate fixture_id values")
    return tuple(sorted(fixtures, key=lambda item: item.fixture_id))


@dataclass(frozen=True, slots=True)
class RunnerIdentity:
    runner_ref: str
    invocation_ref: str
    actor_ref: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunnerIdentity":
        obj = _strict(data, {"runner_ref", "invocation_ref", "actor_ref"}, "RunnerIdentity")
        return cls(*(_string(obj[name], f"runner_identity.{name}") for name in ("runner_ref", "invocation_ref", "actor_ref")))

    def to_dict(self) -> dict[str, Any]:
        return {"runner_ref": self.runner_ref, "invocation_ref": self.invocation_ref, "actor_ref": self.actor_ref}


@dataclass(frozen=True, slots=True)
class PermissionGrants:
    network: bool
    filesystem_read: tuple[str, ...]
    filesystem_write: tuple[str, ...]
    credential_refs: tuple[str, ...]
    core_db: bool

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PermissionGrants":
        obj = _strict(
            data,
            {"network", "filesystem_read", "filesystem_write", "credential_refs", "core_db"},
            "PermissionGrants",
        )
        if not isinstance(obj["network"], bool) or not isinstance(obj["core_db"], bool):
            raise AttestationError("network and core_db grants must be booleans")
        return cls(
            network=obj["network"],
            filesystem_read=_unique_strings(obj["filesystem_read"], "filesystem_read"),
            filesystem_write=_unique_strings(obj["filesystem_write"], "filesystem_write"),
            credential_refs=_unique_strings(obj["credential_refs"], "credential_refs"),
            core_db=obj["core_db"],
        )

    def assert_safe(self) -> None:
        if self.network:
            raise PermissionViolation("capability sandbox attestation forbids network grants")
        if self.credential_refs:
            raise PermissionViolation("capability sandbox attestation forbids credential grants")
        if self.core_db:
            raise PermissionViolation("capability sandbox attestation forbids Core DB grants")

    def to_dict(self) -> dict[str, Any]:
        return {
            "network": self.network,
            "filesystem_read": list(self.filesystem_read),
            "filesystem_write": list(self.filesystem_write),
            "credential_refs": list(self.credential_refs),
            "core_db": self.core_db,
        }


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    max_seconds: float
    max_memory_bytes: int
    max_stdout_bytes: int
    max_stderr_bytes: int

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SandboxLimits":
        obj = _strict(data, {"max_seconds", "max_memory_bytes", "max_stdout_bytes", "max_stderr_bytes"}, "SandboxLimits")
        seconds = obj["max_seconds"]
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(float(seconds)) or seconds <= 0:
            raise AttestationError("max_seconds must be a positive finite number")
        integer_values: dict[str, int] = {}
        for name in ("max_memory_bytes", "max_stdout_bytes", "max_stderr_bytes"):
            value = obj[name]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise AttestationError(f"{name} must be a positive integer")
            integer_values[name] = value
        return cls(max_seconds=float(seconds), **integer_values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_seconds": self.max_seconds,
            "max_memory_bytes": self.max_memory_bytes,
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
        }


@dataclass(frozen=True, slots=True)
class ObservedEffects:
    network_used: bool
    filesystem_writes: tuple[str, ...]
    credential_refs_used: tuple[str, ...]
    core_db_accessed: bool

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ObservedEffects":
        obj = _strict(
            data,
            {"network_used", "filesystem_writes", "credential_refs_used", "core_db_accessed"},
            "ObservedEffects",
        )
        if not isinstance(obj["network_used"], bool) or not isinstance(obj["core_db_accessed"], bool):
            raise AttestationError("observed network/core DB fields must be booleans")
        return cls(
            network_used=obj["network_used"],
            filesystem_writes=_unique_strings(obj["filesystem_writes"], "filesystem_writes"),
            credential_refs_used=_unique_strings(obj["credential_refs_used"], "credential_refs_used"),
            core_db_accessed=obj["core_db_accessed"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "network_used": self.network_used,
            "filesystem_writes": list(self.filesystem_writes),
            "credential_refs_used": list(self.credential_refs_used),
            "core_db_accessed": self.core_db_accessed,
        }


@dataclass(frozen=True, slots=True)
class ObservedUsage:
    duration_seconds: float
    peak_memory_bytes: int
    stdout_bytes: int
    stderr_bytes: int

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ObservedUsage":
        obj = _strict(
            data,
            {"duration_seconds", "peak_memory_bytes", "stdout_bytes", "stderr_bytes"},
            "ObservedUsage",
        )
        duration = obj["duration_seconds"]
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or duration < 0
        ):
            raise AttestationError("duration_seconds must be a non-negative finite number")
        counts: dict[str, int] = {}
        for name in ("peak_memory_bytes", "stdout_bytes", "stderr_bytes"):
            value = obj[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AttestationError(f"{name} must be a non-negative integer")
            counts[name] = value
        return cls(duration_seconds=float(duration), **counts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_seconds": self.duration_seconds,
            "peak_memory_bytes": self.peak_memory_bytes,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
        }


@dataclass(frozen=True, slots=True)
class TrustedLaunchContext:
    capability_ref: str
    proposal_ref: str
    proposal_hash: str
    artifact_hash: str
    dependency_lock_hash: str
    environment_hash: str
    image_hash: str
    fixtures: tuple[FixtureExpectation, ...]
    fixture_manifest_hash: str
    builder_invocation_ref: str
    evaluator_invocation_ref: str
    runner_identity: RunnerIdentity
    policy_ref: str
    policy_hash: str
    grants: PermissionGrants
    limits: SandboxLimits
    started_at: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TrustedLaunchContext":
        required = {
            "capability_ref", "proposal_ref", "proposal_hash", "artifact_hash", "dependency_lock_hash",
            "environment_hash", "image_hash", "fixtures", "builder_invocation_ref",
            "fixture_manifest_hash",
            "evaluator_invocation_ref", "runner_identity", "policy_ref", "policy_hash", "grants",
            "limits", "started_at",
        }
        obj = _strict(data, required, "TrustedLaunchContext")
        result = cls(
            capability_ref=_string(obj["capability_ref"], "capability_ref"),
            proposal_ref=_string(obj["proposal_ref"], "proposal_ref"),
            proposal_hash=_hash(obj["proposal_hash"], "proposal_hash"),
            artifact_hash=_hash(obj["artifact_hash"], "artifact_hash"),
            dependency_lock_hash=_hash(obj["dependency_lock_hash"], "dependency_lock_hash"),
            environment_hash=_hash(obj["environment_hash"], "environment_hash"),
            image_hash=_hash(obj["image_hash"], "image_hash"),
            fixtures=_fixture_expectations(obj["fixtures"]),
            fixture_manifest_hash=_hash(obj["fixture_manifest_hash"], "fixture_manifest_hash"),
            builder_invocation_ref=_string(obj["builder_invocation_ref"], "builder_invocation_ref"),
            evaluator_invocation_ref=_string(obj["evaluator_invocation_ref"], "evaluator_invocation_ref"),
            runner_identity=RunnerIdentity.from_dict(obj["runner_identity"]),
            policy_ref=_string(obj["policy_ref"], "policy_ref"),
            policy_hash=_hash(obj["policy_hash"], "policy_hash"),
            grants=PermissionGrants.from_dict(obj["grants"]),
            limits=SandboxLimits.from_dict(obj["limits"]),
            started_at=_timestamp(obj["started_at"], "started_at"),
        )
        result.grants.assert_safe()
        manifest_hash = sha256_digest([item.to_dict() for item in result.fixtures])
        if result.fixture_manifest_hash != manifest_hash:
            raise AttestationMismatch(
                "fixture_manifest_hash does not match the immutable fixture manifest"
            )
        if result.builder_invocation_ref in {
            result.evaluator_invocation_ref,
            result.runner_identity.invocation_ref,
        }:
            raise AttestationMismatch("builder invocation must be independent from evaluator and runner")
        return result


@dataclass(frozen=True, slots=True)
class UntrustedSandboxReport:
    observed_proposal_hash: str
    observed_artifact_hash: str
    observed_dependency_lock_hash: str
    observed_environment_hash: str
    observed_image_hash: str
    observed_policy_hash: str
    fixtures: tuple[FixtureResult, ...]
    observed_effects: ObservedEffects
    observed_usage: ObservedUsage
    completed_at: str
    exit_code: int
    stdout_hash: str
    stderr_hash: str
    result_status: ResultStatus

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "UntrustedSandboxReport":
        required = {
            "observed_proposal_hash", "observed_artifact_hash", "observed_dependency_lock_hash",
            "observed_environment_hash", "observed_image_hash", "observed_policy_hash", "fixtures",
            "observed_effects", "completed_at", "exit_code", "stdout_hash", "stderr_hash", "result_status",
            "observed_usage",
        }
        obj = _strict(data, required, "UntrustedSandboxReport")
        if isinstance(obj["exit_code"], bool) or not isinstance(obj["exit_code"], int):
            raise AttestationError("exit_code must be an integer")
        try:
            status = ResultStatus(obj["result_status"])
        except (TypeError, ValueError) as exc:
            raise AttestationError("result_status is invalid") from exc
        return cls(
            observed_proposal_hash=_hash(obj["observed_proposal_hash"], "observed_proposal_hash"),
            observed_artifact_hash=_hash(obj["observed_artifact_hash"], "observed_artifact_hash"),
            observed_dependency_lock_hash=_hash(obj["observed_dependency_lock_hash"], "observed_dependency_lock_hash"),
            observed_environment_hash=_hash(obj["observed_environment_hash"], "observed_environment_hash"),
            observed_image_hash=_hash(obj["observed_image_hash"], "observed_image_hash"),
            observed_policy_hash=_hash(obj["observed_policy_hash"], "observed_policy_hash"),
            fixtures=_fixture_results(obj["fixtures"]),
            observed_effects=ObservedEffects.from_dict(obj["observed_effects"]),
            observed_usage=ObservedUsage.from_dict(obj["observed_usage"]),
            completed_at=_timestamp(obj["completed_at"], "completed_at"),
            exit_code=obj["exit_code"],
            stdout_hash=_hash(obj["stdout_hash"], "stdout_hash"),
            stderr_hash=_hash(obj["stderr_hash"], "stderr_hash"),
            result_status=status,
        )


@dataclass(frozen=True, slots=True)
class CapabilityAttestation:
    schema_version: str
    id: str
    created_at: str
    capability_ref: str
    proposal_ref: str
    proposal_hash: str
    artifact_hash: str
    dependency_lock_hash: str
    environment_hash: str
    image_hash: str
    fixtures: tuple[FixtureResult, ...]
    fixture_manifest_hash: str
    builder_invocation_ref: str
    evaluator_invocation_ref: str
    runner_identity: RunnerIdentity
    policy_ref: str
    policy_hash: str
    grants: PermissionGrants
    observed_effects: ObservedEffects
    observed_usage: ObservedUsage
    started_at: str
    completed_at: str
    exit_code: int
    limits: SandboxLimits
    stdout_hash: str
    stderr_hash: str
    result_status: ResultStatus
    content_hash: str

    def _base_wire(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "created_at": self.created_at,
            "capability_ref": self.capability_ref,
            "proposal_ref": self.proposal_ref,
            "proposal_hash": self.proposal_hash,
            "artifact_hash": self.artifact_hash,
            "dependency_lock_hash": self.dependency_lock_hash,
            "environment_hash": self.environment_hash,
            "image_hash": self.image_hash,
            "fixtures": [item.to_dict() for item in self.fixtures],
            "fixture_manifest_hash": self.fixture_manifest_hash,
            "builder_invocation_ref": self.builder_invocation_ref,
            "evaluator_invocation_ref": self.evaluator_invocation_ref,
            "runner_identity": self.runner_identity.to_dict(),
            "policy_ref": self.policy_ref,
            "policy_hash": self.policy_hash,
            "grants": self.grants.to_dict(),
            "observed_effects": self.observed_effects.to_dict(),
            "observed_usage": self.observed_usage.to_dict(),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "exit_code": self.exit_code,
            "limits": self.limits.to_dict(),
            "stdout_hash": self.stdout_hash,
            "stderr_hash": self.stderr_hash,
            "result_status": self.result_status.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityAttestation":
        required = {
            "schema_version", "id", "created_at", "capability_ref", "proposal_ref", "proposal_hash",
            "artifact_hash", "dependency_lock_hash", "environment_hash", "image_hash", "fixtures",
            "fixture_manifest_hash",
            "builder_invocation_ref", "evaluator_invocation_ref", "runner_identity", "policy_ref",
            "policy_hash", "grants", "observed_effects", "observed_usage", "started_at", "completed_at", "exit_code", "limits",
            "stdout_hash", "stderr_hash", "result_status", "content_hash",
        }
        obj = _strict(data, required, "CapabilityAttestation")
        if obj["schema_version"] != SCHEMA_VERSION:
            raise AttestationError(f"schema_version must be {SCHEMA_VERSION!r}")
        if isinstance(obj["exit_code"], bool) or not isinstance(obj["exit_code"], int):
            raise AttestationError("exit_code must be an integer")
        try:
            status = ResultStatus(obj["result_status"])
        except (TypeError, ValueError) as exc:
            raise AttestationError("result_status is invalid") from exc
        result = cls(
            schema_version=obj["schema_version"],
            id=_string(obj["id"], "id"),
            created_at=_timestamp(obj["created_at"], "created_at"),
            capability_ref=_string(obj["capability_ref"], "capability_ref"),
            proposal_ref=_string(obj["proposal_ref"], "proposal_ref"),
            proposal_hash=_hash(obj["proposal_hash"], "proposal_hash"),
            artifact_hash=_hash(obj["artifact_hash"], "artifact_hash"),
            dependency_lock_hash=_hash(obj["dependency_lock_hash"], "dependency_lock_hash"),
            environment_hash=_hash(obj["environment_hash"], "environment_hash"),
            image_hash=_hash(obj["image_hash"], "image_hash"),
            fixtures=_fixture_results(obj["fixtures"]),
            fixture_manifest_hash=_hash(obj["fixture_manifest_hash"], "fixture_manifest_hash"),
            builder_invocation_ref=_string(obj["builder_invocation_ref"], "builder_invocation_ref"),
            evaluator_invocation_ref=_string(obj["evaluator_invocation_ref"], "evaluator_invocation_ref"),
            runner_identity=RunnerIdentity.from_dict(obj["runner_identity"]),
            policy_ref=_string(obj["policy_ref"], "policy_ref"),
            policy_hash=_hash(obj["policy_hash"], "policy_hash"),
            grants=PermissionGrants.from_dict(obj["grants"]),
            observed_effects=ObservedEffects.from_dict(obj["observed_effects"]),
            observed_usage=ObservedUsage.from_dict(obj["observed_usage"]),
            started_at=_timestamp(obj["started_at"], "started_at"),
            completed_at=_timestamp(obj["completed_at"], "completed_at"),
            exit_code=obj["exit_code"],
            limits=SandboxLimits.from_dict(obj["limits"]),
            stdout_hash=_hash(obj["stdout_hash"], "stdout_hash"),
            stderr_hash=_hash(obj["stderr_hash"], "stderr_hash"),
            result_status=status,
            content_hash=_hash(obj["content_hash"], "content_hash"),
        )
        result.grants.assert_safe()
        if (
            result.observed_effects.network_used
            or result.observed_effects.credential_refs_used
            or result.observed_effects.core_db_accessed
        ):
            raise PermissionViolation(
                "attestation records forbidden network, credential, or Core DB access"
            )
        undeclared_writes = set(result.observed_effects.filesystem_writes) - set(
            result.grants.filesystem_write
        )
        if undeclared_writes:
            raise PermissionViolation(
                f"attestation records undeclared filesystem writes: {sorted(undeclared_writes)}"
            )
        persisted_manifest_hash = sha256_digest(
            [
                {
                    "fixture_id": item.fixture_id,
                    "input_hash": item.input_hash,
                    "output_hash": item.output_hash,
                }
                for item in result.fixtures
            ]
        )
        if result.fixture_manifest_hash != persisted_manifest_hash:
            raise AttestationMismatch(
                "attestation fixture_manifest_hash does not match persisted fixtures"
            )
        if result.builder_invocation_ref in {
            result.evaluator_invocation_ref,
            result.runner_identity.invocation_ref,
        }:
            raise AttestationMismatch("builder invocation must be independent from evaluator and runner")
        if _instant(result.completed_at) < _instant(result.started_at):
            raise AttestationMismatch("completed_at precedes started_at")
        if result.created_at != result.completed_at:
            raise AttestationMismatch("created_at must equal completed_at")
        elapsed = (_instant(result.completed_at) - _instant(result.started_at)).total_seconds()
        exceeded = (
            elapsed > result.limits.max_seconds
            or result.observed_usage.duration_seconds > result.limits.max_seconds
            or result.observed_usage.peak_memory_bytes > result.limits.max_memory_bytes
            or result.observed_usage.stdout_bytes > result.limits.max_stdout_bytes
            or result.observed_usage.stderr_bytes > result.limits.max_stderr_bytes
        )
        if result.result_status is ResultStatus.PASSED and exceeded:
            raise AttestationMismatch("passed attestation exceeds sandbox resource limits")
        fixtures_passed = all(item.status is FixtureStatus.PASSED for item in result.fixtures)
        if result.result_status is ResultStatus.PASSED and (result.exit_code != 0 or not fixtures_passed):
            raise AttestationMismatch("passed result requires exit_code 0 and all fixtures passed")
        if result.result_status is not ResultStatus.PASSED and result.exit_code == 0 and fixtures_passed:
            raise AttestationMismatch("non-passed result is inconsistent with exit_code and fixture results")
        result.verify_integrity()
        return result

    def verify_integrity(self) -> None:
        base = self._base_wire()
        expected_id = compute_attestation_id(base)
        if self.id != expected_id:
            raise AttestationMismatch("attestation id does not match canonical content")
        if self.content_hash != compute_attestation_hash(base):
            raise AttestationMismatch("attestation content_hash does not match canonical wire")

    def to_dict(self) -> dict[str, Any]:
        return dict(self._base_wire(), content_hash=self.content_hash)


def validate_sandbox_report(
    expected: TrustedLaunchContext | Mapping[str, Any],
    report: UntrustedSandboxReport | Mapping[str, Any],
) -> CapabilityAttestation:
    """Validate sandbox observations against trusted launch state.

    The untrusted report cannot choose capability/policy/runner identity,
    grants, limits, or timestamps of launch.  Those values are copied only
    from the trusted context after all cross-checks pass.
    """

    context = expected if isinstance(expected, TrustedLaunchContext) else TrustedLaunchContext.from_dict(expected)
    observation = report if isinstance(report, UntrustedSandboxReport) else UntrustedSandboxReport.from_dict(report)

    comparisons = {
        "proposal_hash": (context.proposal_hash, observation.observed_proposal_hash),
        "artifact_hash": (context.artifact_hash, observation.observed_artifact_hash),
        "dependency_lock_hash": (context.dependency_lock_hash, observation.observed_dependency_lock_hash),
        "environment_hash": (context.environment_hash, observation.observed_environment_hash),
        "image_hash": (context.image_hash, observation.observed_image_hash),
        "policy_hash": (context.policy_hash, observation.observed_policy_hash),
    }
    for name, (trusted, observed) in comparisons.items():
        if trusted != observed:
            raise AttestationMismatch(f"observed {name} does not match trusted launch context")

    expected_fixtures = {item.fixture_id: item for item in context.fixtures}
    observed_fixtures = {item.fixture_id: item for item in observation.fixtures}
    if set(expected_fixtures) != set(observed_fixtures):
        raise AttestationMismatch("reported fixtures must exactly match proposal evaluation fixtures")
    for fixture_id, trusted in expected_fixtures.items():
        observed = observed_fixtures[fixture_id]
        if observed.input_hash != trusted.input_hash or observed.output_hash != trusted.output_hash:
            raise AttestationMismatch(f"fixture {fixture_id!r} input/output hash mismatch")

    effects = observation.observed_effects
    if effects.network_used or effects.credential_refs_used or effects.core_db_accessed:
        raise PermissionViolation("report contains forbidden network, credential, or Core DB access")
    undeclared_writes = set(effects.filesystem_writes) - set(context.grants.filesystem_write)
    if undeclared_writes:
        raise PermissionViolation(f"report contains undeclared filesystem writes: {sorted(undeclared_writes)}")
    if _instant(observation.completed_at) < _instant(context.started_at):
        raise AttestationMismatch("completed_at precedes trusted started_at")

    fixture_passed = all(item.status is FixtureStatus.PASSED for item in observation.fixtures)
    if observation.result_status is ResultStatus.PASSED and (observation.exit_code != 0 or not fixture_passed):
        raise AttestationMismatch("passed result requires exit_code 0 and all fixtures passed")
    if observation.result_status is not ResultStatus.PASSED and observation.exit_code == 0 and fixture_passed:
        raise AttestationMismatch("non-passed result is inconsistent with exit_code and fixture results")

    base = {
        "schema_version": SCHEMA_VERSION,
        "created_at": observation.completed_at,
        "capability_ref": context.capability_ref,
        "proposal_ref": context.proposal_ref,
        "proposal_hash": context.proposal_hash,
        "artifact_hash": context.artifact_hash,
        "dependency_lock_hash": context.dependency_lock_hash,
        "environment_hash": context.environment_hash,
        "image_hash": context.image_hash,
        "fixtures": [item.to_dict() for item in observation.fixtures],
        "fixture_manifest_hash": context.fixture_manifest_hash,
        "builder_invocation_ref": context.builder_invocation_ref,
        "evaluator_invocation_ref": context.evaluator_invocation_ref,
        "runner_identity": context.runner_identity.to_dict(),
        "policy_ref": context.policy_ref,
        "policy_hash": context.policy_hash,
        "grants": context.grants.to_dict(),
        "observed_effects": observation.observed_effects.to_dict(),
        "observed_usage": observation.observed_usage.to_dict(),
        "started_at": context.started_at,
        "completed_at": observation.completed_at,
        "exit_code": observation.exit_code,
        "limits": context.limits.to_dict(),
        "stdout_hash": observation.stdout_hash,
        "stderr_hash": observation.stderr_hash,
        "result_status": observation.result_status.value,
    }
    identifier = compute_attestation_id(base)
    wire = dict(base, id=identifier)
    # Context/report parsers already sort fixtures and grant lists, so the
    # dictionary is the same canonical wire that ``to_dict`` will later emit.
    digest = compute_attestation_hash(wire)
    return CapabilityAttestation.from_dict(dict(wire, content_hash=digest))


__all__ = [
    "AttestationError", "AttestationMismatch", "PermissionViolation",
    "FixtureExpectation", "FixtureResult", "FixtureStatus", "RunnerIdentity",
    "PermissionGrants", "SandboxLimits", "ObservedEffects", "ObservedUsage", "TrustedLaunchContext",
    "UntrustedSandboxReport", "CapabilityAttestation", "ResultStatus",
    "canonical_json", "sha256_digest", "compute_attestation_id", "compute_attestation_hash",
    "validate_sandbox_report",
]
