"""Versioned data-driven commit gate."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from .contracts import Verdict, validate_independence_predicate

# The contract owns this allowlist; policy evaluation must not drift from it.
from .contracts import ALLOWED_INDEPENDENCE_PATHS
ALLOWED_OPERATORS = frozenset({"eq", "ne", "in", "not_in"})

DEFAULT_POLICY: dict[str, Any] = {
    "required_verification": True,
    "allowed_verdicts": ["pass"],
    "independence_predicates": [{
        "left_path": "producer.model_family",
        "operator": "ne",
        "right_path": "verifier.model_family",
    }],
}


def _path(path: Any) -> str:
    if not isinstance(path, str) or path not in ALLOWED_INDEPENDENCE_PATHS:
        raise ValueError(f"independence path is not allowed: {path!r}")
    return path


def validate_predicate(predicate: Mapping[str, Any]) -> None:
    """Validate only ``{left_path, operator, right_path|value}``."""
    validate_independence_predicate(predicate)


def canonical_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(policy)
    result.setdefault("required_verification", True)
    result.setdefault("allowed_verdicts", ["pass"])
    result.setdefault("independence_predicates", [])
    if result["required_verification"] is not True:
        raise ValueError("thesis commits in contract 0.1 always require verification")
    if not isinstance(result["allowed_verdicts"], list):
        raise ValueError("allowed_verdicts must be an array")
    valid_verdicts = {v.value for v in Verdict}
    if any(getattr(v, "value", v) not in valid_verdicts for v in result["allowed_verdicts"]):
        raise ValueError("allowed_verdicts contains a value outside Verdict")
    result["allowed_verdicts"] = [getattr(v, "value", v) for v in result["allowed_verdicts"]]
    if not isinstance(result["independence_predicates"], list):
        raise ValueError("independence_predicates must be an array")
    result["independence_predicates"] = [
        validate_independence_predicate(predicate).to_dict()
        for predicate in result["independence_predicates"]
    ]
    return result


def allowed_verdict(policy: Mapping[str, Any], verdict: str) -> bool:
    return verdict in {str(v) for v in canonical_policy(policy)["allowed_verdicts"]}


def _resolve(path: str, producer: Mapping[str, Any], verifier: Mapping[str, Any]) -> Any:
    _path(path)
    side, field = path.split(".", 1)
    return (producer if side == "producer" else verifier).get(field)


def predicate_holds(predicate: Mapping[str, Any], producer: Mapping[str, Any], verifier: Mapping[str, Any]) -> bool:
    validate_predicate(predicate)
    op = getattr(predicate["operator"], "value", predicate["operator"])
    left = _resolve(predicate["left_path"], producer, verifier)
    right = _resolve(predicate["right_path"], producer, verifier) if "right_path" in predicate else predicate["value"]
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op == "in":
        return left in right
    if op == "not_in":
        return left not in right
    raise ValueError(f"unsupported independence predicate operator: {op}")


def check_independence(policy: Mapping[str, Any], producer: Mapping[str, Any], verifier: Mapping[str, Any]) -> tuple[bool, str | None]:
    producer_id = producer.get("invocation_id") or producer.get("id")
    verifier_id = verifier.get("invocation_id") or verifier.get("id")
    if producer_id == verifier_id:
        return False, "producer and verifier must be different invocations"
    for predicate in canonical_policy(policy)["independence_predicates"]:
        try:
            if not predicate_holds(predicate, producer, verifier):
                return False, f"independence predicate failed: {dict(predicate)!r}"
        except (TypeError, ValueError) as exc:
            return False, str(exc)
    return True, None


def evaluate_gate(policy: Mapping[str, Any], verdict: str, producer: Mapping[str, Any], verifier: Mapping[str, Any]) -> tuple[bool, str | None]:
    p = canonical_policy(policy)
    if p.get("required_verification", True) and not verifier:
        return False, "verification is required"
    if not allowed_verdict(p, verdict):
        return False, f"verdict {verdict!r} is not allowed by active policy"
    return check_independence(p, producer, verifier)
