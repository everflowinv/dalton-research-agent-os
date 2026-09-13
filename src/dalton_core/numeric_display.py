"""Deterministic Chinese display formatting without changing source values."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

NumericKind = Literal["amount_usd", "percent", "eps", "arpu"]


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("display number must be numeric")
    try:
        return Decimal(str(value).replace(",", ""))
    except Exception as exc:
        raise ValueError("display number must be numeric") from exc


def format_display_number(value: object, *, kind: NumericKind) -> str:
    """Format a source value for Chinese prose or spreadsheet presentation."""

    number = _decimal(value)
    if kind == "amount_usd":
        magnitude = abs(number)
        if magnitude >= Decimal("100000000"):
            shown = (number / Decimal("100000000")).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP)
            return f"{shown} 亿美元"
        if magnitude >= Decimal("10000"):
            shown = (number / Decimal("10000")).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP)
            return f"{shown} 万美元"
        return f"{number.quantize(Decimal('1'), rounding=ROUND_HALF_UP)} 美元"
    if kind == "percent":
        return f"{number.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"
    if kind == "eps":
        return f"{number.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)} 美元/股"
    if kind == "arpu":
        return f"{number.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)} 美元/用户"
    raise ValueError("unsupported display number kind")


__all__ = ["NumericKind", "format_display_number"]
