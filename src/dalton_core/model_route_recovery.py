"""Recover an immutable pre-call route refusal after admission inputs change."""

from __future__ import annotations

import re
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .model_router import ModelRouter, ModelRouterError
from .store import content_hash


def configuration_fingerprint(path: Path | None) -> str | None:
    """Name the consumed configuration, never its credential file contents."""
    if path is None:
        return None
    from .cockpit_model import validate_model_config
    from .lane_child_launcher import LaneChildRejected
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        validated = validate_model_config(value)
    except (OSError, ValueError, TypeError) as exc:
        raise LaneChildRejected("model configuration is missing or invalid") from exc
    return content_hash(validated)


def configured_business_key(key: str, launcher: Any) -> str:
    fingerprint = getattr(launcher, "configuration_fingerprint", None)
    value = fingerprint() if callable(fingerprint) else None
    return key if value is None else f"{key}|model_config:{value}"


def route_recovery_request(
    formal: Mapping[str, Any] | None, *, work_order_ref: str,
    request_id: str, config: Mapping[str, Any],
) -> str | None:
    """One deterministic successor per changed policy/credential scope.

    Only an authoritative rejected route qualifies. Successful work and
    ambiguous provider failures retain their original replay identity.
    Credential *references* are used; secret contents never enter identity.
    """
    if not isinstance(formal, Mapping) or formal.get("terminal_state") != "failed":
        return None
    envelope = formal.get("result_envelope") or {}
    if (envelope.get("error") or {}).get("code") != "MODEL_ROUTE_REJECTED":
        return None
    metadata = envelope.get("metadata") or {}
    route_ref = metadata.get("route_decision_ref")
    if not isinstance(route_ref, str):
        return _refusal_recovery_request(
            formal, metadata, request_id=request_id, config=config)
    try:
        with ModelRouter(config["model_router_db"], read_only=True) as router:
            route = router.get_decision(route_ref)
            policy = router.get_policy(config["routing_policy_ref"])
    except ModelRouterError:
        return None
    if (route.get("work_order_ref") != work_order_ref
            or route.get("outcome") != "rejected"):
        return None
    previous = {
        "policy_hash": route["policy_hash"],
        "credential_slot_refs": sorted(route["constraints"]["credential_slot_refs"]),
    }
    current = {
        "policy_hash": policy["content_hash"],
        "credential_slot_refs": sorted(config["credential_slot_refs"]),
    }
    if previous == current:
        return None
    suffix = ":route-admission:" + content_hash(current)
    if request_id.endswith(suffix):
        return None
    base = re.sub(r":route-admission:[0-9a-f]{64}$", "", request_id)
    return base + suffix


def _current_scope(config: Mapping[str, Any]) -> tuple[dict[str, Any], str | None] | None:
    try:
        with ModelRouter(config["model_router_db"], read_only=True) as router:
            policy = router.get_policy(config["routing_policy_ref"])
    except ModelRouterError:
        return None
    return ({"policy_hash": policy["content_hash"],
             "credential_slot_refs": sorted(config["credential_slot_refs"])},
            policy.get("created_at"))


def _successor(request_id: str, current: Mapping[str, Any]) -> str | None:
    suffix = ":route-admission:" + content_hash(dict(current))
    if request_id.endswith(suffix):
        return None
    base = re.sub(r":route-admission:[0-9a-f]{64}$", "", request_id)
    return base + suffix


def _refusal_recovery_request(
    formal: Mapping[str, Any], metadata: Mapping[str, Any], *,
    request_id: str, config: Mapping[str, Any],
) -> str | None:
    """Batch w: a chain refused before any route decision was made.

    Live 10-06..10-08, 235 localization-verifier requests were refused this
    way (the verifier chain held only the producer's family) and, carrying no
    route decision, could never be asked again: every later call replayed
    "the model call did not succeed (MODEL_ROUTE_REJECTED)" even after the
    owner changed the selection.  A refusal that recorded its policy
    (``chain_refusal``) is asked again once that policy or the credential
    scope differs; an older one that recorded nothing is asked again once the
    pinned policy is newer than the refusal.  Either way one successor per
    scope, as for a rejected route.
    """

    if metadata.get("chain_failures") or not metadata.get("control_plane_failure"):
        return None
    scope = _current_scope(config)
    if scope is None:
        return None
    current, created_at = scope
    refusal = metadata.get("chain_refusal")
    if isinstance(refusal, Mapping):
        previous = {
            "policy_hash": refusal.get("policy_hash"),
            "credential_slot_refs": sorted(refusal.get("credential_slot_refs") or ()),
        }
        if previous == current:
            return None
        return _successor(request_id, current)
    failed_at = str(formal.get("created_at") or "")
    if not failed_at or not isinstance(created_at, str) or created_at <= failed_at:
        return None
    return _successor(request_id, current)
