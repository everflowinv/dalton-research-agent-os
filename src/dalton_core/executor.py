"""Small, deterministic execution boundary for Dalton Core.

This module intentionally has no knowledge of a process runner, a network, or
an LLM provider.  A handler is an ordinary Python callable registered under a
capability.  It is useful for exercising the envelope and provenance
contracts while keeping tests entirely local.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .contracts import (
    InvocationGranularity,
    ModelInvocation,
    ResultEnvelope,
    RuntimeProfile,
    WorkOrder,
)


class ExecutionRejected(ValueError):
    """The request cannot be admitted by the deterministic boundary."""


class CapabilityRejected(ExecutionRejected):
    """A requested capability is not present in the registry/profile."""


class BudgetRejected(ExecutionRejected):
    """The work order or handler usage is outside its declared envelope."""


class SideEffectEscalation(ExecutionRejected):
    """A handler attempted a side effect not declared by its work order."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _utc(value: str) -> str:
    """Normalize a contract timestamp without introducing wall-clock entropy."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionRejected(f"timestamp is not RFC3339-like: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _tuple_strings(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ExecutionRejected(f"{field} must be a sequence of strings")
    result = tuple(value)
    if any(not isinstance(item, str) or not item for item in result):
        raise ExecutionRejected(f"{field} must contain only non-empty strings")
    return result


def _as_contract(value: Any, cls: type[Any]) -> Any:
    if isinstance(value, cls):
        return value
    if isinstance(value, Mapping):
        return cls.from_dict(value)
    raise TypeError(f"expected {cls.__name__} or mapping")


class LocalDeterministicExecutor:
    """Capability-to-handler registry with deterministic provenance.

    Handlers receive ``(work_order, runtime_profile)``.  They may return a
    mapping containing ``outputs`` and optional ``actual_side_effects`` (or
    the shorter ``side_effects``), ``usage``, ``artifact_refs``,
    ``usage_refs`` and ``status``.  ``_invocation`` is a private, local fixture
    hook used by :class:`StubModelWorker` to choose model metadata.
    """

    def __init__(
        self,
        handlers: Mapping[str, Callable[..., Any]] | None = None,
        *,
        capability_handlers: Mapping[str, Callable[..., Any]] | None = None,
    ):
        self.handlers: dict[str, Callable[..., Any]] = {}
        selected = dict(handlers or {})
        selected.update(capability_handlers or {})
        for capability, handler in selected.items():
            self.register(capability, handler)

    def register(self, capability: str, handler: Callable[..., Any]) -> "LocalDeterministicExecutor":
        if not isinstance(capability, str) or not capability:
            raise ValueError("capability must be a non-empty string")
        if not callable(handler):
            raise TypeError("handler must be callable")
        self.handlers[capability] = handler
        return self

    add_handler = register

    @staticmethod
    def _validate_budget(work: WorkOrder) -> None:
        if not isinstance(work.budget, Mapping) or not work.budget:
            raise BudgetRejected("budget envelope must be a non-empty mapping")
        # Names are intentionally aliases, not governance thresholds.  A
        # caller can install whatever versioned policy it needs; this boundary
        # only rejects malformed/negative declared limits.
        for key in ("max_tokens", "max_cost", "max_seconds", "token_limit", "cost_limit", "time_limit"):
            if key not in work.budget:
                continue
            value = work.budget[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
                raise BudgetRejected(f"budget field {key!r} must be a finite non-negative number")

    @staticmethod
    def _validate_profile_limits(profile: RuntimeProfile) -> None:
        if not isinstance(profile.limits, Mapping):
            raise BudgetRejected("runtime profile limits must be a mapping")
        for key, value in profile.limits.items():
            if key.startswith("max_") or key.endswith("_limit"):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
                    raise BudgetRejected(f"runtime limit {key!r} must be a finite non-negative number")

    @staticmethod
    def _check_declared_budget(work: WorkOrder, limits: Mapping[str, Any]) -> None:
        pairs = (
            ("max_tokens", "max_tokens"),
            ("token_limit", "max_tokens"),
            ("max_cost", "max_cost"),
            ("cost_limit", "max_cost"),
            ("max_seconds", "max_seconds"),
            ("time_limit", "max_seconds"),
        )
        for work_key, profile_key in pairs:
            if work_key in work.budget and profile_key in limits and work.budget[work_key] > limits[profile_key]:
                raise BudgetRejected(f"work-order budget {work_key!r} exceeds runtime limit {profile_key!r}")

    @staticmethod
    def _check_usage(usage: Mapping[str, Any], budget: Mapping[str, Any]) -> None:
        aliases = {
            "tokens": ("max_tokens", "token_limit"),
            "input_tokens": ("max_input_tokens",),
            "output_tokens": ("max_output_tokens",),
            "cost": ("max_cost", "cost_limit"),
            "seconds": ("max_seconds", "time_limit"),
        }
        for used_name, limit_names in aliases.items():
            used = usage.get(used_name)
            if used is None:
                continue
            if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(float(used)) or used < 0:
                raise BudgetRejected(f"usage field {used_name!r} must be finite and non-negative")
            for limit_name in limit_names:
                if limit_name in budget and used > budget[limit_name]:
                    raise BudgetRejected(f"usage {used_name!r} exceeds budget {limit_name!r}")

    @staticmethod
    def _call(handler: Callable[..., Any], work: WorkOrder, profile: RuntimeProfile) -> Any:
        # Keep the public handler contract simple but permit one-argument
        # fixtures, which are convenient in tests.
        try:
            signature = inspect.signature(handler)
            positional = [p for p in signature.parameters.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
            if len(positional) < 2 and not any(p.kind == p.VAR_POSITIONAL for p in signature.parameters.values()):
                return handler(work)
        except (TypeError, ValueError):
            pass
        return handler(work, profile)

    def execute(self, work_order: WorkOrder | Mapping[str, Any], runtime_profile: RuntimeProfile | Mapping[str, Any]) -> tuple[ModelInvocation, ResultEnvelope]:
        work = _as_contract(work_order, WorkOrder)
        profile = _as_contract(runtime_profile, RuntimeProfile)
        self._validate_budget(work)
        self._validate_profile_limits(profile)
        self._check_declared_budget(work, profile.limits)
        if work.schema_version not in profile.supported_input_versions:
            raise ExecutionRejected("runtime profile does not support the WorkOrder schema version")
        if work.schema_version not in profile.supported_result_versions:
            raise ExecutionRejected("runtime profile does not support the ResultEnvelope schema version")
        if work.runtime_profile_ref != profile.id:
            raise ExecutionRejected("work order runtime_profile_ref does not match runtime profile id")
        if not work.requested_capabilities:
            raise CapabilityRejected("work order requests no capabilities")
        if len(work.requested_capabilities) != 1:
            raise CapabilityRejected("one invocation must request exactly one capability")
        capability = work.requested_capabilities[0]
        if capability not in profile.capabilities:
            raise CapabilityRejected(f"runtime profile does not support capability {capability!r}")
        handler = self.handlers.get(capability)
        if handler is None:
            raise CapabilityRejected(f"no deterministic handler registered for capability {capability!r}")
        declared = set(work.declared_side_effects)
        profile_effects = set(profile.side_effects)
        if not declared.issubset(profile_effects):
            raise SideEffectEscalation("work order declares side effects outside runtime profile")

        raw = self._call(handler, work, profile)
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ExecutionRejected("handler must return a mapping")
        outputs = raw.get("outputs", raw.get("result", {}))
        if not isinstance(outputs, Mapping):
            raise ExecutionRejected("handler outputs must be a mapping")
        usage = raw.get("usage", {})
        if not isinstance(usage, Mapping):
            raise ExecutionRejected("handler usage must be a mapping")
        self._check_usage(usage, work.budget)
        self._check_usage(usage, profile.limits)
        actual = _tuple_strings(raw.get("actual_side_effects", raw.get("side_effects", ())), "actual_side_effects")
        if not set(actual).issubset(declared):
            raise SideEffectEscalation("handler actual side effects exceed work-order declaration")

        fixture = raw.get("_invocation", {})
        if fixture is None:
            fixture = {}
        if not isinstance(fixture, Mapping):
            raise ExecutionRejected("_invocation fixture must be a mapping")
        started = _utc(work.created_at)
        invocation_id = str(fixture.get("id", fixture.get("invocation_id", _stable_id("inv", {"work": work.id, "capability": capability, "input": work.input_refs}))))
        if not invocation_id:
            raise ExecutionRejected("invocation id cannot be empty")
        invocation = ModelInvocation(
            schema_version=work.schema_version,
            id=invocation_id,
            created_at=started,
            work_order_ref=work.id,
            profile_ref=profile.id,
            granularity=InvocationGranularity(fixture.get("granularity", InvocationGranularity.TASK.value)),
            capability=capability,
            provider=str(fixture.get("provider", "deterministic")),
            model=str(fixture.get("model", "deterministic-model")),
            model_family=str(fixture.get("model_family", "deterministic")),
            input_refs=work.input_refs,
            output_refs=_tuple_strings(raw.get("output_refs", ()), "output_refs"),
            started_at=started,
            completed_at=started,
            usage=dict(usage),
            side_effects=actual,
            runtime_ref=profile.id,
            actor_ref=str(fixture.get("actor_ref", "local-deterministic-executor")),
            parent_ref=fixture.get("parent_ref"),
            environment_hash=profile.environment_hash,
        )
        artifact_refs = _tuple_strings(raw.get("artifact_refs", ()), "artifact_refs")
        usage_refs = _tuple_strings(raw.get("usage_refs", ()), "usage_refs")
        result_id = str(raw.get("id", _stable_id("result", {"invocation": invocation.id, "outputs": outputs, "status": raw.get("status", "completed")})))
        envelope = ResultEnvelope(
            schema_version=work.schema_version,
            id=result_id,
            created_at=started,
            work_order_ref=work.id,
            invocation_ref=invocation.id,
            status=str(raw.get("status", "completed")),
            outputs=dict(outputs),
            artifact_refs=artifact_refs,
            actual_side_effects=actual,
            usage_refs=usage_refs,
            error=dict(raw["error"]) if isinstance(raw.get("error"), Mapping) else None,
            metadata={"executor": "local-deterministic", "capability": capability},
        )
        return invocation, envelope

    run = execute
    dispatch = execute
    invoke = execute


__all__ = [
    "BudgetRejected",
    "CapabilityRejected",
    "ExecutionRejected",
    "LocalDeterministicExecutor",
    "SideEffectEscalation",
]
