"""Closed policy and proof checks for paid provider retry attempts."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any


class ProviderRetryError(ValueError):
    pass


ELIGIBLE_RETURNED_CODES = frozenset({
    "RATE_LIMITED", "PROVIDER_RATE_LIMITED", "PROVIDER_OVERLOADED",
    "PROVIDER_INTERNAL_ERROR", "UPSTREAM_SERVICE_ERROR",
})
_HASH_RE = re.compile(r"[0-9a-f]{64}")


def validate_provider_retry(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != {
        "max_same_profile_retries", "retry_backoff_seconds",
    }:
        raise ProviderRetryError("provider_retry has an invalid shape")
    retries = value["max_same_profile_retries"]
    backoff = value["retry_backoff_seconds"]
    if (isinstance(retries, bool) or not isinstance(retries, int)
            or not 0 <= retries <= 3):
        raise ProviderRetryError("max_same_profile_retries must be between 0 and 3")
    if (isinstance(backoff, bool) or not isinstance(backoff, int)
            or not 0 <= backoff <= 3600):
        raise ProviderRetryError("retry_backoff_seconds must be between 0 and 3600")
    return {"max_same_profile_retries": retries,
            "retry_backoff_seconds": backoff}


def returned_provider_failure_proof(invocation: Any, result: Any) -> dict[str, str] | None:
    """Return a closed retry proof only for a completed broker response.

    Token/cost telemetry may be unavailable; that affects settlement, not the
    proof that the provider returned a retryable failure. Transport exceptions
    and host completion failures never reach this function as eligible proof.
    """
    error = result.error if isinstance(getattr(result, "error", None), Mapping) else {}
    metadata = result.metadata if isinstance(getattr(result, "metadata", None), Mapping) else {}
    code = error.get("code")
    if (getattr(result, "status", None) != "failed"
            or error.get("source") != "openclaw-model-broker"
            or not isinstance(code, str) or code not in ELIGIBLE_RETURNED_CODES
            or not isinstance(metadata.get("broker_response_hash"), str)
            or not _HASH_RE.fullmatch(metadata["broker_response_hash"])
            or metadata.get("broker_request_mode") != "execute"
            or metadata.get("dispatch_proof") != {
                "authority": "openclaw-model-adapter",
                "state": "provider_completed_failure", "version": "0.1",
            }
            or getattr(invocation, "id", None) != getattr(result, "invocation_ref", None)
            or getattr(invocation, "work_order_ref", None) != getattr(result, "work_order_ref", None)
            or getattr(invocation, "parent_ref", None) != metadata.get("route_decision_ref")
            or getattr(invocation, "completed_at", None) is None):
        return None
    return {"code": code, "broker_response_hash": metadata["broker_response_hash"]}


__all__ = ["ELIGIBLE_RETURNED_CODES", "ProviderRetryError",
           "returned_provider_failure_proof", "validate_provider_retry"]
