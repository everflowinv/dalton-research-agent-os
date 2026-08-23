"""Shared durable usage and cost accounting for broker model workers."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from typing import Any

from .contracts import ModelInvocation
from .store import content_hash


class ModelAccountingError(RuntimeError):
    pass


def _route_estimate_micros(
    route: Mapping[str, Any], profile: Mapping[str, Any]
) -> int:
    selected = [
        item
        for item in route.get("candidate_snapshot", [])
        if isinstance(item, Mapping)
        and item.get("profile_version_ref") == profile["profile_version_ref"]
        and item.get("eligible") is True
    ]
    if len(selected) != 1:
        raise ModelAccountingError("selected route has no exact cost estimate")
    try:
        value = Decimal(str(selected[0]["estimated_cost_usd"]))
    except (KeyError, ValueError, ArithmeticError) as exc:
        raise ModelAccountingError("selected route cost estimate is invalid") from exc
    if not value.is_finite() or value < 0:
        raise ModelAccountingError("selected route cost estimate is invalid")
    return int(
        (value * Decimal(1_000_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def record_model_accounting(
    observability: Any,
    invocation: ModelInvocation,
    route: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    actor_ref: str,
    namespace: str,
) -> dict[str, Any]:
    """Record one immutable usage entry and its actual or estimated USD cost."""

    usage = dict(invocation.usage)
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    if (
        all(
            isinstance(item, int) and not isinstance(item, bool)
            for item in (input_tokens, output_tokens, total_tokens)
        )
        and total_tokens != input_tokens + output_tokens
    ):
        total_tokens = None
    normalized = {
        "input_tokens": input_tokens if isinstance(input_tokens, int) else None,
        "output_tokens": output_tokens if isinstance(output_tokens, int) else None,
        "reasoning_tokens": None,
        "cache_read_tokens": (
            usage.get("cache_read_tokens")
            if isinstance(usage.get("cache_read_tokens"), int)
            else None
        ),
        "cache_write_tokens": (
            usage.get("cache_write_tokens")
            if isinstance(usage.get("cache_write_tokens"), int)
            else None
        ),
        "total_tokens": total_tokens if isinstance(total_tokens, int) else None,
        "requests": 1,
        "duration_ms": None,
        "input_bytes": None,
        "output_bytes": None,
    }
    usage_entry_id = "usage-entry:" + hashlib.sha256(
        invocation.id.encode("utf-8")
    ).hexdigest()[:32]
    usage_entry = observability.record_usage(
        invocation.id,
        entry_id=usage_entry_id,
        occurred_at=invocation.completed_at or invocation.created_at,
        metering_source=(
            "provider_reported"
            if normalized["input_tokens"] is not None
            or normalized["output_tokens"] is not None
            else "launcher_measured"
        ),
        measurement_status="partial",
        raw_usage=usage,
        workflow_ref=None,
        provider_usage_ref=invocation.parent_ref,
        correction_of_ref=None,
        actor_ref=actor_ref,
        idempotency_key=f"usage:{invocation.id}",
        **normalized,
    )

    raw_cost = usage.get("raw_provider_telemetry", {}).get("cost", {})
    reported = (
        isinstance(raw_cost, Mapping)
        and raw_cost.get("available") is True
        and isinstance(raw_cost.get("usd"), (int, float))
        and not isinstance(raw_cost.get("usd"), bool)
        and math.isfinite(float(raw_cost["usd"]))
        and float(raw_cost["usd"]) >= 0
    )
    rates: list[dict[str, Any]] = []
    if reported:
        amount_micros = int(
            (Decimal(str(raw_cost["usd"])) * Decimal(1_000_000)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        charge_specs = [("request", 1, amount_micros)]
        cost_status = "actual"
        calculation_ref = "calculator:broker-reported-request-cost:0.1"
    elif normalized["input_tokens"] is not None and normalized["output_tokens"] is not None:
        charge_specs = []
        amount = Fraction(0, 1)
        for charge, metric, price_field in (
            ("input_tokens", normalized["input_tokens"], "input_per_million_usd"),
            ("output_tokens", normalized["output_tokens"], "output_per_million_usd"),
        ):
            unit_price = int(
                (
                    Decimal(str(profile["cost"][price_field]))
                    * Decimal(1_000_000)
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
            charge_specs.append((charge, 1_000_000, unit_price))
            amount += Fraction(metric * unit_price, 1_000_000)
        amount_micros = (
            2 * amount.numerator + amount.denominator
        ) // (2 * amount.denominator)
        cost_status = "estimated"
        calculation_ref = "calculator:profile-token-rates:0.1"
    else:
        amount_micros = _route_estimate_micros(route, profile)
        charge_specs = [("request", 1, amount_micros)]
        cost_status = "estimated"
        calculation_ref = "calculator:model-route-estimate:0.1"

    for charge, quantity, unit_price in charge_specs:
        identity = {
            "invocation_ref": invocation.id,
            "profile_version_ref": profile["profile_version_ref"],
            "charge_type": charge,
            "unit_quantity": quantity,
            "unit_price_micros": unit_price,
        }
        digest = content_hash(identity)[:32]
        rates.append(observability.create_price_rate_version(
            f"price-rate:{namespace}:{digest}",
            provider=profile["provider"],
            model=profile["model"],
            charge_type=charge,
            unit_quantity=quantity,
            unit_price_micros=unit_price,
            currency="USD",
            effective_from=invocation.created_at,
            effective_until=None,
            source_ref=(
                invocation.parent_ref
                if charge == "request"
                else profile["profile_version_ref"]
            ),
            actor_ref=actor_ref,
            prior_version_ref=None,
            version_id=f"price-rate-version:{digest}",
            idempotency_key=f"price-rate:{namespace}:{digest}",
        ))
    cost_entry_id = "cost-entry:" + hashlib.sha256(
        usage_entry_id.encode("utf-8")
    ).hexdigest()[:32]
    cost = observability.record_cost(
        usage_entry_id,
        price_rate_refs=[item["id"] for item in rates],
        amount_micros=amount_micros,
        currency="USD",
        cost_status=cost_status,
        calculation_ref=calculation_ref,
        correction_of_ref=None,
        cost_entry_id=cost_entry_id,
        actor_ref=actor_ref,
        idempotency_key=f"cost:{usage_entry_id}",
    )
    return {"usage": usage_entry, "cost": cost}


__all__ = ["ModelAccountingError", "record_model_accounting"]
