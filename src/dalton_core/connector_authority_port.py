"""Narrow post-transport authority port for the trusted connector runner."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ConnectorAuthorityPort:
    """Expose only the seven writes needed to finish a physical attempt.

    Core Connector/Observability tables share a DaltonStore transaction owner;
    Scheduler is a separate SQLite authority.  Deterministic idempotency keys
    and journal replay provide convergence across that non-atomic boundary.
    """

    __slots__ = ("_connectors", "_observability", "_scheduler")

    def __init__(self, *, connectors: Any, observability: Any, scheduler: Any):
        self._connectors = connectors
        self._observability = observability
        self._scheduler = scheduler

    def record_physical_attempt(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._connectors.record_physical_attempt(*args, **kwargs)

    def record_usage(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._connectors.record_usage(*args, **kwargs)

    def record_cost(
        self,
        usage_entry_ref: str,
        *,
        cost_status: str,
        calculation_ref: str,
        actor_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._connectors.record_cost_for_runner(
            usage_entry_ref,
            cost_status=cost_status,
            calculation_ref=calculation_ref,
            actor_ref=actor_ref,
            idempotency_key=idempotency_key,
        )

    def settle_quota(
        self,
        reservation_ref: str,
        state: str,
        *,
        usage_entry_ref: str | None,
        cost_entry_ref: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._connectors.settle_quota_for_runner(
            reservation_ref,
            state,
            usage_entry_ref=usage_entry_ref,
            cost_entry_ref=cost_entry_ref,
            idempotency_key=idempotency_key,
        )

    def register_artifact_version_v2(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._observability.register_artifact_version_v2(*args, **kwargs)

    def record_source_envelope(
        self, spec: Mapping[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        return self._connectors.record_source_envelope(
            spec, idempotency_key=idempotency_key
        )

    def complete(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._scheduler.complete(*args, **kwargs)


__all__ = ["ConnectorAuthorityPort"]
