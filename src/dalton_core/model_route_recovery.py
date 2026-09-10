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
    route_ref = (envelope.get("metadata") or {}).get("route_decision_ref")
    if not isinstance(route_ref, str):
        return None
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
