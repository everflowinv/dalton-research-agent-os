"""Deterministic Chinese display formatting without changing source values."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

NumericKind = Literal["amount_usd", "percent", "eps", "arpu"]


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("display number must be numeric")
    try:
        number = Decimal(str(value).replace(",", ""))
        if not number.is_finite():
            raise ValueError("display number must be finite")
        return number
    except Exception as exc:
        raise ValueError("display number must be numeric") from exc


def format_display_number(value: object, *, kind: NumericKind) -> str:
    """Format a source value for Chinese prose or spreadsheet presentation."""

    number = _decimal(value)
    if kind == "amount_usd":
        magnitude = abs(number)
        if magnitude >= Decimal("100000000"):
            return _scaled_amount(number, Decimal("100000000"), "亿美元")
        if magnitude >= Decimal("10000"):
            return _scaled_amount(number, Decimal("10000"), "万美元")
        shown = number.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return f"{shown} 美元"
    if kind == "percent":
        return f"{number.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"
    if kind == "eps":
        return f"{number.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)} 美元/股"
    if kind == "arpu":
        return f"{number.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)} 美元/用户"
    raise ValueError("unsupported display number kind")


__all__ = ["NumericKind", "format_display_number", "format_typed_value"]


def _scaled_amount(number: Decimal, divisor: Decimal, label: str) -> str:
    value = number / divisor
    # Keep about four significant digits: 1562 万美元, 156.2 万美元,
    # 15.62 万美元, 1.26 亿美元. A small total must not round by 20–30%.
    places = max(0, min(2, 3 - abs(value).adjusted()))
    shown = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return f"{shown} {label}"


def format_typed_value(value: object, *, unit: str = "", scale: str | None = None,
                       currency: str | None = None, metric: str = "") -> str:
    """Display a typed claim; infer no unit from an untyped number."""
    number = _decimal(value)
    scale_name = (scale or "one").lower()
    unit_name = unit.lower().replace("-", "_")
    factors = {"one": Decimal(1), "base": Decimal(1), "unit": Decimal(1),
               "thousand": Decimal(1000), "million": Decimal(1000000),
               "billion": Decimal(1000000000)}
    if scale_name not in factors:
        return " ".join(str(x) for x in (value, currency, unit, scale) if x)
    dollar = (currency or "").upper() == "USD" or unit_name in {"usd", "usd_per_share", "usd_per_user"}
    if dollar and (unit_name in {"per_share", "currency_per_share", "usd_per_share"}
                   or metric.lower() in {"eps", "earnings_per_share", "diluted_eps"}):
        return format_display_number(number, kind="eps")
    if dollar and (unit_name in {"per_user", "currency_per_user", "usd_per_user"}
                   or metric.lower() == "arpu"):
        return format_display_number(number, kind="arpu")
    if unit_name in {"percent", "percentage", "%"}:
        return format_display_number(number, kind="percent")
    if dollar and unit_name in {"usd", "currency", "monetary", "dollars", ""}:
        return format_display_number(number * factors[scale_name], kind="amount_usd")
    return " ".join(str(x) for x in (value, currency, unit, scale if scale_name not in {"one", "base"} else "") if x)
