"""Narrow post-transport authority port for the trusted connector runner."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


class ConnectorCompletionReceiptReader:
    """Read immutable Connector/Artifact receipts without write authority."""

    __slots__ = ("_connectors", "_observability")

    def __init__(self, *, connectors: Any, observability: Any):
        from .connector import ConnectorStore
        from .observability import ObservabilityStore

        if type(connectors) is not ConnectorStore:
            raise TypeError("completion reader requires an exact ConnectorStore")
        if type(observability) is not ObservabilityStore:
            raise TypeError("completion reader requires an exact ObservabilityStore")
        if connectors.connection is not observability.connection:
            raise TypeError("completion reader authorities must share one Core store")
        self._connectors = connectors
        self._observability = observability

    def get_reservation(self, reservation_ref: str) -> dict[str, Any]:
        return self._connectors.get_reservation(reservation_ref)

    def get_invocation(self, invocation_ref: str) -> dict[str, Any]:
        return self._connectors.get_invocation(invocation_ref)

    def get_profile(self, profile_ref: str) -> dict[str, Any]:
        return self._connectors.get_profile(profile_ref)

    def get_call_spec(self, call_spec_ref: str) -> dict[str, Any]:
        return self._connectors.get_call_spec(call_spec_ref)

    def _connector_record(
        self, table: str, id_column: str, record_ref: str
    ) -> dict[str, Any] | None:
        row = self._connectors.connection.execute(
            f"SELECT record_json FROM {table} WHERE {id_column}=?", (record_ref,)
        ).fetchone()
        return None if row is None else json.loads(row["record_json"])

    def get_physical_attempt(self, attempt_ref: str) -> dict[str, Any] | None:
        return self._connector_record(
            "connector_physical_attempts", "physical_attempt_id", attempt_ref
        )

    def get_usage_entry(self, usage_ref: str) -> dict[str, Any] | None:
        return self._connector_record(
            "connector_usage_entries", "usage_entry_id", usage_ref
        )

    def get_cost_entry(self, cost_ref: str) -> dict[str, Any] | None:
        return self._connector_record(
            "connector_cost_entries", "cost_entry_id", cost_ref
        )

    def get_quota_settlement(self, settlement_ref: str) -> dict[str, Any] | None:
        return self._connector_record(
            "connector_quota_settlements", "settlement_id", settlement_ref
        )

    def get_source_envelope(self, source_ref: str) -> dict[str, Any] | None:
        return self._connector_record(
            "connector_source_envelopes", "source_envelope_id", source_ref
        )

    def get_artifact_version(self, version_ref: str) -> dict[str, Any]:
        return self._observability.get_artifact_version_v2(version_ref)

    def get_execution(self, execution_ref: str) -> dict[str, Any] | None:
        row = self._connectors.connection.execute(
            "SELECT execution_json,content_hash FROM execution_invocations "
            "WHERE execution_id=?",
            (execution_ref,),
        ).fetchone()
        if row is None:
            return None
        return {
            "execution": json.loads(row["execution_json"]),
            "content_hash": row["content_hash"],
        }


class ConnectorAuthorityPort:
    """Expose only the seven writes needed to finish a physical attempt.

    Core Connector/Observability tables share a DaltonStore transaction owner;
    Scheduler is a separate SQLite authority.  Deterministic idempotency keys
    and journal replay provide convergence across that non-atomic boundary.
    """

    __slots__ = ("_connectors", "_observability", "_scheduler", "_receipts")

    def __init__(
        self,
        *,
        connectors: Any,
        observability: Any,
        scheduler: Any,
        receipt_reader: ConnectorCompletionReceiptReader | None = None,
    ):
        self._connectors = connectors
        self._observability = observability
        self._scheduler = scheduler
        self._receipts = receipt_reader or ConnectorCompletionReceiptReader(
            connectors=connectors, observability=observability
        )

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

    def reconcile_journaled_completion(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._scheduler.reconcile_journaled_completion(*args, **kwargs)

    def get_reservation(self, reservation_ref: str) -> dict[str, Any]:
        return self._receipts.get_reservation(reservation_ref)

    def get_invocation(self, invocation_ref: str) -> dict[str, Any]:
        return self._receipts.get_invocation(invocation_ref)

    def get_profile(self, profile_ref: str) -> dict[str, Any]:
        return self._receipts.get_profile(profile_ref)

    def get_call_spec(self, call_spec_ref: str) -> dict[str, Any]:
        return self._receipts.get_call_spec(call_spec_ref)

    def get_physical_attempt(self, attempt_ref: str) -> dict[str, Any] | None:
        return self._receipts.get_physical_attempt(attempt_ref)

    def get_usage_entry(self, usage_ref: str) -> dict[str, Any] | None:
        return self._receipts.get_usage_entry(usage_ref)

    def get_cost_entry(self, cost_ref: str) -> dict[str, Any] | None:
        return self._receipts.get_cost_entry(cost_ref)

    def get_quota_settlement(self, settlement_ref: str) -> dict[str, Any] | None:
        return self._receipts.get_quota_settlement(settlement_ref)

    def get_source_envelope(self, source_ref: str) -> dict[str, Any] | None:
        return self._receipts.get_source_envelope(source_ref)

    def get_artifact_version(self, version_ref: str) -> dict[str, Any]:
        return self._receipts.get_artifact_version(version_ref)

    def get_execution(self, execution_ref: str) -> dict[str, Any] | None:
        return self._receipts.get_execution(execution_ref)

    def get_scheduler_result(self, result_ref: str) -> dict[str, Any] | None:
        row = self._scheduler.connection.execute(
            "SELECT work_order_id,attempt_number,result_envelope_hash,"
            "result_envelope_json,outcome,content_hash,created_at "
            "FROM scheduler_result_envelopes WHERE result_envelope_id=?",
            (result_ref,),
        ).fetchone()
        return None if row is None else dict(row)

    def get_scheduler_work_order(self, work_order_ref: str) -> dict[str, Any] | None:
        row = self._scheduler.connection.execute(
            "SELECT work_order_json,work_order_hash FROM scheduler_work_orders "
            "WHERE work_order_id=?",
            (work_order_ref,),
        ).fetchone()
        if row is None:
            return None
        return {
            "work_order": json.loads(row["work_order_json"]),
            "content_hash": row["work_order_hash"],
        }


__all__ = ["ConnectorAuthorityPort", "ConnectorCompletionReceiptReader"]
