"""Versioned, runtime-independent Dalton Core contracts.

The module deliberately contains only Python standard-library code.  The
dataclasses are the executable contract for the small slice implemented here;
the JSON schemas in ``contracts/`` are the interchange contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, ClassVar, Mapping, TypeVar


SCHEMA_VERSION = "0.1"
_UNSET = object()


class ValidationError(ValueError):
    """Raised when a contract is malformed or contains an unknown field."""


class Verdict(str, Enum):
    PASS = "pass"
    CONDITIONAL_PASS = "conditional_pass"
    REVISE = "revise"
    BLOCKED = "blocked"
    REJECT = "reject"


class ValueKind(str, Enum):
    OBSERVED = "observed"
    ASSUMPTION = "assumption"
    DERIVED_DETERMINISTIC = "derived_deterministic"
    ESTIMATE = "estimate"
    SIMULATION = "simulation"


class InvocationGranularity(str, Enum):
    """The unit whose provenance and usage are recorded by an invocation."""

    WORK_ORDER = "work_order"
    TASK = "task"
    VERIFICATION = "verification"
    ADJUDICATION = "adjudication"
    SYSTEM = "system"


class ExecutionKind(str, Enum):
    MODEL = "model"
    CONNECTOR = "connector"


class PredicateOperator(str, Enum):
    EQ = "eq"
    NE = "ne"
    IN = "in"
    NOT_IN = "not_in"


# A policy may choose any value.  Only the location of an attribute is part of
# the contract, so a policy cannot smuggle in arbitrary object traversal.
ALLOWED_INDEPENDENCE_PATHS = frozenset(
    f"{side}.{attribute}"
    for side in ("producer", "verifier")
    for attribute in (
        "provider",
        "model",
        "profile_ref",
        "capability",
        "runtime_ref",
        "actor_ref",
        "granularity",
        "environment_hash",
        "model_family",
    )
)


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{name} must be a non-empty string")
    return value


def _require_sha256(value: Any, name: str) -> str:
    value = _require_string(value, name)
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValidationError(f"{name} must be 64 lowercase SHA-256 hex characters")
    return value


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{name} must be an object")
    return value


def _strict_kwargs(cls: type[Any], data: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {f.name for f in fields(cls)}
    unknown = set(data) - allowed
    if unknown:
        raise ValidationError(f"{cls.__name__}: unknown field(s): {sorted(unknown)}")
    from dataclasses import MISSING

    missing = [
        f.name
        for f in fields(cls)
        if f.default is MISSING and f.default_factory is MISSING and f.name not in data
    ]
    if missing:
        raise ValidationError(f"{cls.__name__}: missing field(s): {missing}")
    return dict(data)


def _enum_value(value: Any, enum_type: type[Enum], name: str) -> str:
    if isinstance(value, enum_type):
        return value.value  # type: ignore[return-value]
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    try:
        return enum_type(value).value  # type: ignore[return-value]
    except ValueError as exc:
        raise ValidationError(f"{name} has invalid value: {value!r}") from exc


def _check_common(schema_version: str, identifier: str, created_at: str) -> None:
    if schema_version != SCHEMA_VERSION:
        raise ValidationError(
            f"schema_version {schema_version!r} is not supported; expected {SCHEMA_VERSION!r}"
        )
    _require_string(identifier, "id")
    _require_string(created_at, "created_at")


@dataclass(frozen=True, slots=True)
class IndependencePredicate:
    left_path: str
    operator: PredicateOperator
    right_path: str | None = None
    value: Any = _UNSET

    allowed_paths: ClassVar[frozenset[str]] = ALLOWED_INDEPENDENCE_PATHS

    def __post_init__(self) -> None:
        _require_string(self.left_path, "left_path")
        if self.left_path not in self.allowed_paths:
            raise ValidationError(f"independence path is not allowed: {self.left_path!r}")
        op = _enum_value(self.operator, PredicateOperator, "operator")
        if self.right_path is not None:
            _require_string(self.right_path, "right_path")
            if self.right_path not in self.allowed_paths:
                raise ValidationError(f"independence path is not allowed: {self.right_path!r}")
            if op in {"in", "not_in"} or self.value is not _UNSET:
                raise ValidationError("right_path cannot be combined with value or in/not_in")
            return
        if op in {"eq", "ne"} and self.value is _UNSET:
            raise ValidationError("eq/ne predicate requires right_path or value")
        if op in {"in", "not_in"}:
            if not isinstance(self.value, list) or not self.value:
                raise ValidationError(f"{op} predicate value must be a non-empty list")
        elif isinstance(self.value, (dict, list)):
            raise ValidationError(f"{op} predicate value must be a scalar")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "IndependencePredicate":
        obj = _require_mapping(data, "predicate")
        _strict_kwargs(cls, obj)
        try:
            operator = PredicateOperator(obj["operator"])
        except ValueError as exc:
            raise ValidationError(f"operator has invalid value: {obj['operator']!r}") from exc
        return cls(
            left_path=obj["left_path"],
            operator=operator,
            right_path=obj.get("right_path"),
            value=obj.get("value", _UNSET),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {"left_path": self.left_path, "operator": self.operator.value}
        if self.right_path is not None:
            result["right_path"] = self.right_path
        else:
            result["value"] = self.value
        return result


def validate_independence_predicate(data: Mapping[str, Any]) -> IndependencePredicate:
    """Validate and return one closed-shape independence predicate."""

    return IndependencePredicate.from_dict(data)


@dataclass(frozen=True, slots=True)
class WorkOrder:
    schema_version: str
    id: str
    created_at: str
    updated_at: str
    question: str
    requested_capabilities: tuple[str, ...]
    runtime_profile_ref: str
    budget: Mapping[str, Any]
    idempotency_key: str
    declared_side_effects: tuple[str, ...]
    status: str
    input_refs: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_common(self.schema_version, self.id, self.created_at)
        _require_string(self.updated_at, "updated_at")
        _require_string(self.question, "question")
        _require_string(self.runtime_profile_ref, "runtime_profile_ref")
        _require_mapping(self.budget, "budget")
        _require_string(self.idempotency_key, "idempotency_key")
        if not isinstance(self.requested_capabilities, tuple) or not all(
            isinstance(x, str) and x for x in self.requested_capabilities
        ):
            raise ValidationError("requested_capabilities must be a tuple of non-empty strings")
        if not isinstance(self.input_refs, tuple) or not all(isinstance(x, str) and x for x in self.input_refs):
            raise ValidationError("input_refs must be a tuple of non-empty strings")
        if not isinstance(self.declared_side_effects, tuple) or not all(isinstance(x, str) and x for x in self.declared_side_effects):
            raise ValidationError("declared_side_effects must be a tuple of non-empty strings")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkOrder":
        obj = _strict_kwargs(cls, _require_mapping(data, "WorkOrder"))
        obj["requested_capabilities"] = tuple(obj["requested_capabilities"])
        obj["input_refs"] = tuple(obj.get("input_refs", ()))
        obj["declared_side_effects"] = tuple(obj.get("declared_side_effects", ()))
        return cls(**obj)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "question": self.question,
            "requested_capabilities": list(self.requested_capabilities),
            "runtime_profile_ref": self.runtime_profile_ref,
            "budget": dict(self.budget),
            "idempotency_key": self.idempotency_key,
            "declared_side_effects": list(self.declared_side_effects),
            "status": self.status,
            "input_refs": list(self.input_refs),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ResultEnvelope:
    schema_version: str
    id: str
    created_at: str
    work_order_ref: str
    invocation_ref: str
    status: str
    outputs: Mapping[str, Any]
    actual_side_effects: tuple[str, ...]
    usage_refs: tuple[str, ...]
    artifact_refs: tuple[str, ...]
    error: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_common(self.schema_version, self.id, self.created_at)
        for name, value in (("work_order_ref", self.work_order_ref), ("invocation_ref", self.invocation_ref), ("status", self.status)):
            _require_string(value, name)
        _require_mapping(self.outputs, "outputs")
        if not isinstance(self.artifact_refs, tuple) or not all(isinstance(x, str) and x for x in self.artifact_refs):
            raise ValidationError("artifact_refs must be a tuple of non-empty strings")
        for name, value in (("actual_side_effects", self.actual_side_effects), ("usage_refs", self.usage_refs)):
            if not isinstance(value, tuple) or not all(isinstance(x, str) and x for x in value):
                raise ValidationError(f"{name} must be a tuple of non-empty strings")
        if self.error is not None:
            _require_mapping(self.error, "error")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResultEnvelope":
        obj = _strict_kwargs(cls, _require_mapping(data, "ResultEnvelope"))
        obj["artifact_refs"] = tuple(obj.get("artifact_refs", ()))
        obj["actual_side_effects"] = tuple(obj.get("actual_side_effects", ()))
        obj["usage_refs"] = tuple(obj.get("usage_refs", ()))
        return cls(**obj)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "id": self.id,
            "created_at": self.created_at,
            "work_order_ref": self.work_order_ref,
            "invocation_ref": self.invocation_ref,
            "status": self.status,
            "outputs": dict(self.outputs),
            "artifact_refs": list(self.artifact_refs),
            "actual_side_effects": list(self.actual_side_effects),
            "usage_refs": list(self.usage_refs),
            "metadata": dict(self.metadata),
        }
        if self.error is not None:
            result["error"] = dict(self.error)
        return result


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    schema_version: str
    id: str
    created_at: str
    version: str
    capabilities: tuple[str, ...]
    isolation_level: str
    allowed_tools: tuple[str, ...]
    network: str
    filesystem: str
    side_effects: tuple[str, ...]
    limits: Mapping[str, Any]
    supported_input_versions: tuple[str, ...]
    supported_result_versions: tuple[str, ...]
    runtime_version: str
    environment_hash: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_common(self.schema_version, self.id, self.created_at)
        for name, value in (("version", self.version), ("isolation_level", self.isolation_level), ("network", self.network), ("filesystem", self.filesystem), ("runtime_version", self.runtime_version), ("environment_hash", self.environment_hash)):
            _require_string(value, name)
        for name, value in (("capabilities", self.capabilities), ("allowed_tools", self.allowed_tools), ("side_effects", self.side_effects), ("supported_input_versions", self.supported_input_versions), ("supported_result_versions", self.supported_result_versions)):
            if not isinstance(value, tuple) or not all(isinstance(x, str) and x for x in value):
                raise ValidationError(f"{name} must be a tuple of non-empty strings")
        _require_mapping(self.limits, "limits")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RuntimeProfile":
        obj = _strict_kwargs(cls, _require_mapping(data, "RuntimeProfile"))
        for key in ("capabilities", "allowed_tools", "side_effects", "supported_input_versions", "supported_result_versions"):
            obj[key] = tuple(obj[key])
        return cls(**obj)

    def to_dict(self) -> dict[str, Any]:
        result = {f.name: getattr(self, f.name) for f in fields(self)}
        for key in ("capabilities", "allowed_tools", "side_effects", "supported_input_versions", "supported_result_versions"):
            result[key] = list(result[key])
        result["limits"] = dict(self.limits)
        result["metadata"] = dict(self.metadata)
        return result


@dataclass(frozen=True, slots=True)
class ExecutionInvocation:
    """Runtime-neutral execution provenance shared by all invocation subtypes."""

    schema_version: str
    id: str
    created_at: str
    kind: ExecutionKind
    work_order_ref: str
    profile_ref: str
    capability: str
    input_refs: tuple[str, ...]
    output_refs: tuple[str, ...]
    started_at: str
    completed_at: str | None
    side_effects: tuple[str, ...]
    runtime_ref: str
    actor_ref: str
    parent_ref: str | None = None
    environment_hash: str | None = None

    def __post_init__(self) -> None:
        _check_common(self.schema_version, self.id, self.created_at)
        _enum_value(self.kind, ExecutionKind, "kind")
        for name, value in (
            ("work_order_ref", self.work_order_ref),
            ("profile_ref", self.profile_ref),
            ("capability", self.capability),
            ("started_at", self.started_at),
            ("runtime_ref", self.runtime_ref),
            ("actor_ref", self.actor_ref),
        ):
            _require_string(value, name)
        for name, value in (
            ("input_refs", self.input_refs),
            ("output_refs", self.output_refs),
            ("side_effects", self.side_effects),
        ):
            if not isinstance(value, tuple) or not all(
                isinstance(item, str) and item for item in value
            ):
                raise ValidationError(f"{name} must be a tuple of non-empty strings")
        for name, value in (
            ("completed_at", self.completed_at),
            ("parent_ref", self.parent_ref),
            ("environment_hash", self.environment_hash),
        ):
            if value is not None:
                _require_string(value, name)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExecutionInvocation":
        obj = _strict_kwargs(cls, _require_mapping(data, "ExecutionInvocation"))
        for key in ("input_refs", "output_refs", "side_effects"):
            obj[key] = tuple(obj.get(key, ()))
        try:
            obj["kind"] = ExecutionKind(obj["kind"])
        except ValueError as exc:
            raise ValidationError(f"kind has invalid value: {obj['kind']!r}") from exc
        return cls(**obj)

    @classmethod
    def from_model(cls, invocation: "ModelInvocation") -> "ExecutionInvocation":
        return cls(
            schema_version=invocation.schema_version,
            id=invocation.id,
            created_at=invocation.created_at,
            kind=ExecutionKind.MODEL,
            work_order_ref=invocation.work_order_ref,
            profile_ref=invocation.profile_ref,
            capability=invocation.capability,
            input_refs=invocation.input_refs,
            output_refs=invocation.output_refs,
            started_at=invocation.started_at,
            completed_at=invocation.completed_at,
            side_effects=invocation.side_effects,
            runtime_ref=invocation.runtime_ref,
            actor_ref=invocation.actor_ref,
            parent_ref=invocation.parent_ref,
            environment_hash=invocation.environment_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        result = {f.name: getattr(self, f.name) for f in fields(self)}
        result["kind"] = self.kind.value
        for key in ("input_refs", "output_refs", "side_effects"):
            result[key] = list(result[key])
        return result


@dataclass(frozen=True, slots=True)
class ModelInvocation:
    schema_version: str
    id: str
    created_at: str
    work_order_ref: str
    profile_ref: str
    granularity: InvocationGranularity
    capability: str
    provider: str
    model: str
    model_family: str
    input_refs: tuple[str, ...]
    output_refs: tuple[str, ...]
    started_at: str
    completed_at: str | None
    usage: Mapping[str, Any]
    side_effects: tuple[str, ...]
    runtime_ref: str
    actor_ref: str
    parent_ref: str | None = None
    environment_hash: str | None = None

    def __post_init__(self) -> None:
        _check_common(self.schema_version, self.id, self.created_at)
        for name, value in (("work_order_ref", self.work_order_ref), ("profile_ref", self.profile_ref), ("capability", self.capability), ("provider", self.provider), ("model", self.model), ("model_family", self.model_family), ("started_at", self.started_at), ("runtime_ref", self.runtime_ref), ("actor_ref", self.actor_ref)):
            _require_string(value, name)
        if self.completed_at is not None:
            _require_string(self.completed_at, "completed_at")
        if self.environment_hash is not None:
            _require_string(self.environment_hash, "environment_hash")
        _enum_value(self.granularity, InvocationGranularity, "granularity")
        _require_mapping(self.usage, "usage")
        for name, value in (("input_refs", self.input_refs), ("output_refs", self.output_refs), ("side_effects", self.side_effects)):
            if not isinstance(value, tuple) or not all(isinstance(x, str) and x for x in value):
                raise ValidationError(f"{name} must be a tuple of non-empty strings")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModelInvocation":
        obj = _strict_kwargs(cls, _require_mapping(data, "ModelInvocation"))
        for key in ("input_refs", "output_refs", "side_effects"):
            obj[key] = tuple(obj[key])
        try:
            obj["granularity"] = InvocationGranularity(obj["granularity"])
        except ValueError as exc:
            raise ValidationError(f"granularity has invalid value: {obj['granularity']!r}") from exc
        return cls(**obj)

    def to_dict(self) -> dict[str, Any]:
        result = {f.name: getattr(self, f.name) for f in fields(self)}
        result["granularity"] = self.granularity.value
        for key in ("input_refs", "output_refs", "side_effects"):
            result[key] = list(result[key])
        result["usage"] = dict(self.usage)
        return result


@dataclass(frozen=True, slots=True)
class DomainEvent:
    schema_version: str
    id: str
    created_at: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    version_ref: str | None
    content_hash: str
    occurred_at: str
    actor_ref: str
    payload: Mapping[str, Any]
    idempotency_key: str
    correlation_id: str
    causation_id: str | None = None
    change_ref: str | None = None
    verification_ref: str | None = None

    def __post_init__(self) -> None:
        _check_common(self.schema_version, self.id, self.created_at)
        for name, value in (("event_type", self.event_type), ("aggregate_type", self.aggregate_type), ("aggregate_id", self.aggregate_id), ("occurred_at", self.occurred_at), ("actor_ref", self.actor_ref), ("idempotency_key", self.idempotency_key), ("correlation_id", self.correlation_id), ("content_hash", self.content_hash)):
            _require_string(value, name)
        for name, value in (("version_ref", self.version_ref), ("causation_id", self.causation_id), ("change_ref", self.change_ref), ("verification_ref", self.verification_ref)):
            if value is not None:
                _require_string(value, name)
        if isinstance(self.aggregate_version, bool) or not isinstance(self.aggregate_version, int) or self.aggregate_version < 0:
            raise ValidationError("aggregate_version must be a non-negative integer")
        _require_mapping(self.payload, "payload")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DomainEvent":
        return cls(**_strict_kwargs(cls, _require_mapping(data, "DomainEvent")))

    def to_dict(self) -> dict[str, Any]:
        result = {f.name: getattr(self, f.name) for f in fields(self)}
        result["payload"] = dict(self.payload)
        return result


@dataclass(frozen=True, slots=True)
class CapabilityProposal:
    schema_version: str
    id: str
    created_at: str
    updated_at: str
    title: str
    kind: str
    gap: Mapping[str, Any]
    expected_benefit: Mapping[str, Any]
    contract: Mapping[str, Any]
    permissions: Mapping[str, Any]
    fixtures: tuple[str, ...]
    fixture_manifest_hash: str
    participants: Mapping[str, str]
    status: str
    artifact_refs: tuple[str, ...] = ()
    prior_capability_ref: str | None = None

    def __post_init__(self) -> None:
        _check_common(self.schema_version, self.id, self.created_at)
        for name, value in (("updated_at", self.updated_at), ("title", self.title), ("kind", self.kind), ("status", self.status)):
            _require_string(value, name)
        for name, value in (("gap", self.gap), ("expected_benefit", self.expected_benefit), ("contract", self.contract), ("permissions", self.permissions), ("participants", self.participants)):
            _require_mapping(value, name)
        for name, value in (("fixtures", self.fixtures), ("artifact_refs", self.artifact_refs)):
            if not isinstance(value, tuple) or not all(isinstance(x, str) and x for x in value):
                raise ValidationError(f"{name} must be a tuple of non-empty strings")
        _require_sha256(self.fixture_manifest_hash, "fixture_manifest_hash")
        if self.prior_capability_ref is not None:
            _require_string(self.prior_capability_ref, "prior_capability_ref")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityProposal":
        obj = _strict_kwargs(cls, _require_mapping(data, "CapabilityProposal"))
        obj["fixtures"] = tuple(obj["fixtures"])
        obj["artifact_refs"] = tuple(obj.get("artifact_refs", ()))
        return cls(**obj)

    def to_dict(self) -> dict[str, Any]:
        result = {f.name: getattr(self, f.name) for f in fields(self)}
        for key in ("gap", "expected_benefit", "contract", "permissions", "participants"):
            result[key] = dict(result[key])
        result["fixtures"] = list(result["fixtures"])
        result["artifact_refs"] = list(result["artifact_refs"])
        return result


class EvidenceRelationType(str, Enum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    QUALIFIES = "qualifies"


class ClaimKind(str, Enum):
    QUANTITATIVE = "quantitative"
    QUALITATIVE = "qualitative"


class AdjudicatedStatus(str, Enum):
    CORROBORATED = "corroborated"
    CONTESTED = "contested"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


@dataclass(frozen=True, slots=True)
class EvidenceVersion:
    """Immutable, source-backed evidence record.

    ``evidence_ref`` is the stable identity; ``id`` identifies this version.
    Source lineage and independence group are retained as data rather than
    inferred from a URL, because two apparently different documents may be
    syndicated copies of the same source.
    """

    schema_version: str
    id: str
    created_at: str
    evidence_ref: str
    version: int
    source_type: str
    source_ref: str
    retrieved_at: str
    valid_until: str | None
    artifact_refs: tuple[str, ...]
    source_lineage: tuple[str, ...]
    independence_group: str
    actor_ref: str
    prior_version_ref: str | None
    content_hash: str

    def __post_init__(self) -> None:
        _check_common(self.schema_version, self.id, self.created_at)
        for name, value in (("evidence_ref", self.evidence_ref), ("source_type", self.source_type),
                            ("source_ref", self.source_ref), ("retrieved_at", self.retrieved_at),
                            ("independence_group", self.independence_group), ("actor_ref", self.actor_ref),
                            ("content_hash", self.content_hash)):
            _require_string(value, name)
        if self.valid_until is not None:
            _require_string(self.valid_until, "valid_until")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValidationError("version must be a positive integer")
        if not self.source_lineage:
            raise ValidationError("source_lineage must not be empty")
        for name, value in (("artifact_refs", self.artifact_refs), ("source_lineage", self.source_lineage)):
            if not isinstance(value, tuple) or not all(isinstance(x, str) and x for x in value):
                raise ValidationError(f"{name} must be a tuple of non-empty strings")
        if self.prior_version_ref is not None:
            _require_string(self.prior_version_ref, "prior_version_ref")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceVersion":
        obj = _strict_kwargs(cls, _require_mapping(data, "EvidenceVersion"))
        obj["artifact_refs"] = tuple(obj["artifact_refs"])
        obj["source_lineage"] = tuple(obj["source_lineage"])
        return cls(**obj)

    def to_dict(self) -> dict[str, Any]:
        result = {f.name: getattr(self, f.name) for f in fields(self)}
        result["artifact_refs"] = list(result["artifact_refs"])
        result["source_lineage"] = list(result["source_lineage"])
        return result


@dataclass(frozen=True, slots=True)
class ClaimVersion:
    """An immutable assertion.  ``status`` intentionally is not a field."""

    schema_version: str
    id: str
    created_at: str
    claim_ref: str
    version: int
    subject_ref: str
    metric_or_aspect: str
    period: str
    basis: str
    normalized_statement: str
    claim_kind: ClaimKind
    value: float | int | None
    unit: str | None
    producer_invocation_refs: tuple[str, ...]
    actor_ref: str
    prior_version_ref: str | None
    content_hash: str

    def __post_init__(self) -> None:
        _check_common(self.schema_version, self.id, self.created_at)
        for name, value in (("claim_ref", self.claim_ref), ("subject_ref", self.subject_ref),
                            ("metric_or_aspect", self.metric_or_aspect), ("period", self.period),
                            ("basis", self.basis), ("normalized_statement", self.normalized_statement),
                            ("actor_ref", self.actor_ref), ("content_hash", self.content_hash)):
            _require_string(value, name)
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValidationError("version must be a positive integer")
        _enum_value(self.claim_kind, ClaimKind, "claim_kind")
        if self.claim_kind == ClaimKind.QUANTITATIVE or self.claim_kind == ClaimKind.QUANTITATIVE.value:
            if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
                raise ValidationError("quantitative claim value must be a number")
            _require_string(self.unit, "unit")
        elif self.value is not None and (isinstance(self.value, bool) or not isinstance(self.value, (int, float))):
            raise ValidationError("claim value must be numeric when supplied")
        if self.unit is not None:
            _require_string(self.unit, "unit")
        if not isinstance(self.producer_invocation_refs, tuple) or not self.producer_invocation_refs or not all(isinstance(x, str) and x for x in self.producer_invocation_refs):
            raise ValidationError("producer_invocation_refs must be a non-empty tuple of strings")
        if self.prior_version_ref is not None:
            _require_string(self.prior_version_ref, "prior_version_ref")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ClaimVersion":
        obj = _strict_kwargs(cls, _require_mapping(data, "ClaimVersion"))
        try:
            obj["claim_kind"] = ClaimKind(obj["claim_kind"])
        except ValueError as exc:
            raise ValidationError(f"claim_kind has invalid value: {obj['claim_kind']!r}") from exc
        obj["producer_invocation_refs"] = tuple(obj["producer_invocation_refs"])
        return cls(**obj)

    def to_dict(self) -> dict[str, Any]:
        result = {f.name: getattr(self, f.name) for f in fields(self)}
        result["claim_kind"] = self.claim_kind.value
        result["producer_invocation_refs"] = list(result["producer_invocation_refs"])
        return result


@dataclass(frozen=True, slots=True)
class EvidenceRelation:
    schema_version: str
    id: str
    created_at: str
    evidence_ref: str
    evidence_version_ref: str
    claim_ref: str
    claim_version_ref: str
    relation: EvidenceRelationType
    source_lineage: tuple[str, ...]
    independence_group: str
    actor_ref: str
    content_hash: str

    def __post_init__(self) -> None:
        _check_common(self.schema_version, self.id, self.created_at)
        for name, value in (("evidence_ref", self.evidence_ref), ("evidence_version_ref", self.evidence_version_ref),
                            ("claim_ref", self.claim_ref), ("claim_version_ref", self.claim_version_ref),
                            ("independence_group", self.independence_group), ("actor_ref", self.actor_ref),
                            ("content_hash", self.content_hash)):
            _require_string(value, name)
        _enum_value(self.relation, EvidenceRelationType, "relation")
        if not isinstance(self.source_lineage, tuple) or not all(isinstance(x, str) and x for x in self.source_lineage):
            raise ValidationError("source_lineage must be a tuple of non-empty strings")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceRelation":
        obj = _strict_kwargs(cls, _require_mapping(data, "EvidenceRelation"))
        try:
            obj["relation"] = EvidenceRelationType(obj["relation"])
        except ValueError as exc:
            raise ValidationError(f"relation has invalid value: {obj['relation']!r}") from exc
        obj["source_lineage"] = tuple(obj["source_lineage"])
        return cls(**obj)

    def to_dict(self) -> dict[str, Any]:
        result = {f.name: getattr(self, f.name) for f in fields(self)}
        result["relation"] = self.relation.value
        result["source_lineage"] = list(result["source_lineage"])
        return result


@dataclass(frozen=True, slots=True)
class AdjudicationVersion:
    schema_version: str
    id: str
    created_at: str
    claim_ref: str
    claim_version_ref: str
    version: int
    adjudicated_status: AdjudicatedStatus
    rationale: str
    findings: tuple[Mapping[str, Any], ...]
    adjudicator_invocation_ref: str
    subject_invocation_refs: tuple[str, ...]
    independence_policy_ref: str
    prior_version_ref: str | None
    content_hash: str

    def __post_init__(self) -> None:
        _check_common(self.schema_version, self.id, self.created_at)
        for name, value in (("claim_ref", self.claim_ref), ("claim_version_ref", self.claim_version_ref),
                            ("rationale", self.rationale), ("adjudicator_invocation_ref", self.adjudicator_invocation_ref),
                            ("independence_policy_ref", self.independence_policy_ref), ("content_hash", self.content_hash)):
            _require_string(value, name)
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValidationError("version must be a positive integer")
        _enum_value(self.adjudicated_status, AdjudicatedStatus, "adjudicated_status")
        if not isinstance(self.findings, tuple) or not all(isinstance(x, Mapping) for x in self.findings):
            raise ValidationError("findings must be a tuple of objects")
        if not isinstance(self.subject_invocation_refs, tuple) or not self.subject_invocation_refs or not all(isinstance(x, str) and x for x in self.subject_invocation_refs):
            raise ValidationError("subject_invocation_refs must be a non-empty tuple of strings")
        if self.prior_version_ref is not None:
            _require_string(self.prior_version_ref, "prior_version_ref")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdjudicationVersion":
        obj = _strict_kwargs(cls, _require_mapping(data, "AdjudicationVersion"))
        try:
            obj["adjudicated_status"] = AdjudicatedStatus(obj["adjudicated_status"])
        except ValueError as exc:
            raise ValidationError(f"adjudicated_status has invalid value: {obj['adjudicated_status']!r}") from exc
        obj["findings"] = tuple(obj["findings"])
        obj["subject_invocation_refs"] = tuple(obj["subject_invocation_refs"])
        return cls(**obj)

    def to_dict(self) -> dict[str, Any]:
        result = {f.name: getattr(self, f.name) for f in fields(self)}
        result["adjudicated_status"] = self.adjudicated_status.value
        result["findings"] = list(result["findings"])
        result["subject_invocation_refs"] = list(result["subject_invocation_refs"])
        return result


@dataclass(frozen=True, slots=True)
class ThesisVersion:
    schema_version: str
    id: str
    created_at: str
    thesis_ref: str
    version: int
    statement: str
    mechanism: str
    confidence: float | str
    implied_expectation: str
    claim_refs: tuple[str, ...]
    catalyst_refs: tuple[str, ...]
    falsifier_refs: tuple[str, ...]
    change_reason: str
    prior_version_ref: str | None
    verification_ref: str | None
    committed_by_ref: str
    content_hash: str
    authority_kind: str | None = None
    authority_ref: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version not in {"0.1", "0.2"}:
            raise ValidationError("unsupported ThesisVersion schema_version")
        _require_string(self.id, "id")
        _require_string(self.created_at, "created_at")
        for name, value in (("thesis_ref", self.thesis_ref), ("statement", self.statement), ("mechanism", self.mechanism), ("implied_expectation", self.implied_expectation), ("change_reason", self.change_reason), ("committed_by_ref", self.committed_by_ref), ("content_hash", self.content_hash)):
            _require_string(value, name)
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValidationError("version must be a positive integer")
        if self.schema_version == "0.1":
            if (
                isinstance(self.confidence, bool)
                or not isinstance(self.confidence, (int, float))
                or not 0 <= self.confidence <= 1
            ):
                raise ValidationError("legacy confidence must be between 0 and 1")
            _require_string(self.verification_ref, "verification_ref")
            if self.authority_kind is not None or self.authority_ref is not None:
                raise ValidationError("legacy ThesisVersion cannot carry v0.2 authority fields")
        else:
            if self.confidence not in {"low", "medium", "high"}:
                raise ValidationError("confidence must be low, medium, or high")
            if self.verification_ref is not None:
                raise ValidationError("ThesisVersion v0.2 uses authority_ref")
            if self.authority_kind not in {"verification", "human_admission"}:
                raise ValidationError("authority_kind is invalid")
            _require_string(self.authority_ref, "authority_ref")
        for name, value in (("claim_refs", self.claim_refs), ("catalyst_refs", self.catalyst_refs), ("falsifier_refs", self.falsifier_refs)):
            if not isinstance(value, tuple) or not all(isinstance(x, str) and x for x in value):
                raise ValidationError(f"{name} must be a tuple of non-empty strings")
        if self.prior_version_ref is not None:
            _require_string(self.prior_version_ref, "prior_version_ref")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ThesisVersion":
        obj = dict(_require_mapping(data, "ThesisVersion"))
        schema_version = obj.get("schema_version")
        common = {
            "schema_version", "id", "created_at", "thesis_ref", "version",
            "statement", "mechanism", "confidence", "implied_expectation",
            "claim_refs", "catalyst_refs", "falsifier_refs", "change_reason",
            "prior_version_ref", "committed_by_ref", "content_hash",
        }
        if schema_version == "0.1":
            expected = common | {"verification_ref"}
        elif schema_version == "0.2":
            expected = common | {"authority_kind", "authority_ref"}
        else:
            raise ValidationError("unsupported ThesisVersion schema_version")
        if set(obj) != expected:
            unknown = sorted(set(obj) - expected)
            missing = sorted(expected - set(obj))
            raise ValidationError(
                f"ThesisVersion fields are invalid; unknown={unknown}, missing={missing}"
            )
        if schema_version == "0.2":
            obj["verification_ref"] = None
        for key in ("claim_refs", "catalyst_refs", "falsifier_refs"):
            obj[key] = tuple(obj[key])
        return cls(**obj)

    def to_dict(self) -> dict[str, Any]:
        names = (
            "schema_version", "id", "created_at", "thesis_ref", "version",
            "statement", "mechanism", "confidence", "implied_expectation",
            "claim_refs", "catalyst_refs", "falsifier_refs", "change_reason",
            "prior_version_ref", "committed_by_ref", "content_hash",
        )
        result = {name: getattr(self, name) for name in names}
        if self.schema_version == "0.1":
            result["verification_ref"] = self.verification_ref
        else:
            result["authority_kind"] = self.authority_kind
            result["authority_ref"] = self.authority_ref
        for key in ("claim_refs", "catalyst_refs", "falsifier_refs"):
            result[key] = list(result[key])
        return result


@dataclass(frozen=True, slots=True)
class VerificationRecord:
    schema_version: str
    id: str
    created_at: str
    target_ref: str
    verifier_invocation_ref: str
    verdict: Verdict
    findings: tuple[Mapping[str, Any], ...]
    deterministic_checks: tuple[Mapping[str, Any], ...]
    verifier_kind: str
    revise_round: int
    independence_policy_ref: str
    subject_invocation_refs: tuple[str, ...]
    target_content_hash: str

    def __post_init__(self) -> None:
        _check_common(self.schema_version, self.id, self.created_at)
        for name, value in (("target_ref", self.target_ref), ("verifier_invocation_ref", self.verifier_invocation_ref), ("verifier_kind", self.verifier_kind), ("independence_policy_ref", self.independence_policy_ref), ("target_content_hash", self.target_content_hash)):
            _require_string(value, name)
        _enum_value(self.verdict, Verdict, "verdict")
        if isinstance(self.revise_round, bool) or not isinstance(self.revise_round, int) or self.revise_round < 0:
            raise ValidationError("revise_round must be a non-negative integer")
        for name, value in (("findings", self.findings), ("deterministic_checks", self.deterministic_checks)):
            if not isinstance(value, tuple) or not all(isinstance(x, Mapping) for x in value):
                raise ValidationError(f"{name} must be a tuple of objects")
        if not isinstance(self.subject_invocation_refs, tuple) or not all(isinstance(x, str) and x for x in self.subject_invocation_refs):
            raise ValidationError("subject_invocation_refs must be a tuple of non-empty strings")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VerificationRecord":
        obj = _strict_kwargs(cls, _require_mapping(data, "VerificationRecord"))
        try:
            obj["verdict"] = Verdict(obj["verdict"])
        except ValueError as exc:
            raise ValidationError(f"verdict has invalid value: {obj['verdict']!r}") from exc
        obj["findings"] = tuple(obj["findings"])
        obj["deterministic_checks"] = tuple(obj["deterministic_checks"])
        obj["subject_invocation_refs"] = tuple(obj["subject_invocation_refs"])
        return cls(**obj)

    def to_dict(self) -> dict[str, Any]:
        result = {f.name: getattr(self, f.name) for f in fields(self)}
        result["verdict"] = self.verdict.value
        for key in ("findings", "deterministic_checks", "subject_invocation_refs"):
            result[key] = list(result[key])
        return result


@dataclass(frozen=True, slots=True)
class GovernancePolicyVersion:
    schema_version: str
    id: str
    created_at: str
    policy_ref: str
    version: int
    effective_from: str
    effective_until: str | None
    policy: Mapping[str, Any]
    independence_predicates: tuple[IndependencePredicate, ...]
    change_reason: str
    actor_ref: str
    prior_version_ref: str | None
    content_hash: str

    def __post_init__(self) -> None:
        _check_common(self.schema_version, self.id, self.created_at)
        for name, value in (("policy_ref", self.policy_ref), ("effective_from", self.effective_from), ("change_reason", self.change_reason), ("actor_ref", self.actor_ref), ("content_hash", self.content_hash)):
            _require_string(value, name)
        if self.effective_until is not None:
            _require_string(self.effective_until, "effective_until")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValidationError("version must be a positive integer")
        _require_mapping(self.policy, "policy")
        if not isinstance(self.independence_predicates, tuple) or not all(isinstance(x, IndependencePredicate) for x in self.independence_predicates):
            raise ValidationError("independence_predicates must contain IndependencePredicate values")
        if self.prior_version_ref is not None:
            _require_string(self.prior_version_ref, "prior_version_ref")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GovernancePolicyVersion":
        obj = _strict_kwargs(cls, _require_mapping(data, "GovernancePolicyVersion"))
        obj["independence_predicates"] = tuple(IndependencePredicate.from_dict(x) for x in obj["independence_predicates"])
        return cls(**obj)

    def to_dict(self) -> dict[str, Any]:
        result = {f.name: getattr(self, f.name) for f in fields(self)}
        result["policy"] = dict(self.policy)
        result["independence_predicates"] = [x.to_dict() for x in self.independence_predicates]
        return result


ContractT = TypeVar("ContractT")


CONTRACT_TYPES: dict[str, type[Any]] = {
    cls.__name__: cls
    for cls in (
        WorkOrder,
        ResultEnvelope,
        RuntimeProfile,
        ExecutionInvocation,
        ModelInvocation,
        DomainEvent,
        CapabilityProposal,
        EvidenceVersion,
        ClaimVersion,
        EvidenceRelation,
        AdjudicationVersion,
        ThesisVersion,
        VerificationRecord,
        GovernancePolicyVersion,
    )
}


def validate_contract(name: str, data: Mapping[str, Any]) -> Any:
    """Parse and validate a named contract without third-party dependencies."""

    try:
        contract_type = CONTRACT_TYPES[name]
    except KeyError as exc:
        raise ValidationError(f"unknown contract: {name}") from exc
    return contract_type.from_dict(data)


__all__ = [
    "ALLOWED_INDEPENDENCE_PATHS",
    "CapabilityProposal",
    "CONTRACT_TYPES",
    "DomainEvent",
    "ExecutionInvocation",
    "ExecutionKind",
    "GovernancePolicyVersion",
    "IndependencePredicate",
    "InvocationGranularity",
    "ModelInvocation",
    "PredicateOperator",
    "ResultEnvelope",
    "RuntimeProfile",
    "SCHEMA_VERSION",
    "ThesisVersion",
    "ValidationError",
    "ValueKind",
    "Verdict",
    "VerificationRecord",
    "WorkOrder",
    "validate_contract",
    "validate_independence_predicate",
]
