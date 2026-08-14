"""Deterministic parent completion wires for recorded reference shadows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .store import content_hash


def _derived_id(prefix: str, key: str) -> str:
    return f"{prefix}:{content_hash({'kind': prefix, 'idempotency_key': key})}"


def build_recorded_parent_result(
    *,
    request_ref: str,
    work_order_ref: str,
    execution_ref: str,
    execution_output_refs: Sequence[str],
    invocation_ref: str,
    receipts: Sequence[Mapping[str, Any]],
    completion_recorded_at: str,
    source: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Derive the only accepted aggregate ResultEnvelope from authority facts."""

    if not receipts:
        raise ValueError("recorded completion requires at least one page receipt")
    last = receipts[-1]
    key = f"runner-shadow:{invocation_ref}:aggregate"
    attempt_refs = [item["physical_attempt_ref"] for item in receipts]
    settlement_refs = [item["quota_settlement_ref"] for item in receipts]
    usage_refs = [item["usage_entry_ref"] for item in receipts]
    retained_artifacts = any(
        item["raw_artifact_version_ref"] is not None for item in receipts
    )
    if source is not None:
        completeness = source["completeness"]
        if completeness not in {"enumerated", "partial"}:
            raise ValueError("successful source completeness is invalid")
        status = "succeeded"
        artifact_refs = list(execution_output_refs)
        error = None
        metadata = {
            "runner_request_ref": request_ref,
            "physical_attempt_count": len(receipts),
            "terminal_cursor_observed": completeness == "enumerated",
            "completeness": completeness,
        }
        source_ref = source["id"]
    else:
        status = "retryable"
        artifact_refs = list(execution_output_refs) if retained_artifacts else []
        error = last["error"] or {
            "code": last["attempt_outcome"],
            "message": "recorded source page failed",
            "retryable": True,
        }
        metadata = {
            "runner_request_ref": request_ref,
            "physical_attempt_count": len(receipts),
            "completeness": "unknown",
        }
        source_ref = None
    result = {
        "schema_version": "0.1",
        "id": _derived_id("result-envelope", f"{key}:result"),
        "created_at": completion_recorded_at,
        "work_order_ref": work_order_ref,
        "invocation_ref": execution_ref,
        "status": status,
        "outputs": {
            "connector_invocation_ref": invocation_ref,
            "physical_attempt_refs": attempt_refs,
            "result_physical_attempt_ref": last["physical_attempt_ref"],
            "quota_settlement_refs": settlement_refs,
            "source_envelope_ref": source_ref,
        },
        "actual_side_effects": ["read:recorded-fixture"],
        "usage_refs": usage_refs,
        "artifact_refs": artifact_refs,
        "metadata": metadata,
    }
    if error is not None:
        result["error"] = error
    return result


def build_recorded_parent_response(
    *,
    request: Mapping[str, Any],
    invocation: Mapping[str, Any],
    receipts: Sequence[Mapping[str, Any]],
    result: Mapping[str, Any],
    result_hash: str,
    source: Mapping[str, Any] | None,
    retry_at: str | None,
) -> dict[str, Any]:
    """Derive the only parent RunnerResponse persisted in the journal."""

    if not receipts:
        raise ValueError("recorded response requires at least one page receipt")
    last = receipts[-1]
    successful = result["status"] == "succeeded"
    base = {
        "schema_version": "0.2",
        "id": _derived_id(
            "connector-runner-response",
            f"runner-shadow:{invocation['id']}:response",
        ),
        "created_at": result["created_at"],
        "runner_request_ref": request["id"],
        "runner_request_hash": request["content_hash"],
        "idempotency_status": "fresh",
        "connector_invocation_ref": invocation["id"],
        "connector_invocation_hash": invocation["content_hash"],
        "physical_attempt_ref": last["physical_attempt_ref"],
        "physical_attempt_hash": last["physical_attempt_hash"],
        "usage_entry_ref": last["usage_entry_ref"],
        "usage_entry_hash": last["usage_entry_hash"],
        "cost_entry_ref": last["cost_entry_ref"],
        "cost_entry_hash": last["cost_entry_hash"],
        "quota_settlement_ref": last["quota_settlement_ref"],
        "quota_settlement_hash": last["quota_settlement_hash"],
        "raw_artifact_version_ref": (
            last["raw_artifact_version_ref"] if successful else None
        ),
        "raw_artifact_version_hash": (
            last["raw_artifact_version_hash"] if successful else None
        ),
        "source_envelope_ref": source["id"] if successful and source else None,
        "source_envelope_hash": (
            source["content_hash"] if successful and source else None
        ),
        "result_envelope_ref": result["id"],
        "result_envelope_hash": result_hash,
        "outcome": result["status"],
        "retry_at": retry_at,
    }
    base["content_hash"] = content_hash(base)
    return base


__all__ = ["build_recorded_parent_result", "build_recorded_parent_response"]
