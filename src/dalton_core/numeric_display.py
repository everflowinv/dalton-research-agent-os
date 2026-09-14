"""Deterministic Chinese display formatting without changing source values."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import re
from typing import Literal
from typing import Callable

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


_HAN = re.compile(r"[\u3400-\u9fff]")
_DECIMAL_TOKEN = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_BASE_USD = re.compile(
    rf"(?i)(?P<prefix>\bUSD\s+)(?P<prefix_number>{_DECIMAL_TOKEN})"
    rf"(?![A-Za-z0-9.,])|(?<![A-Za-z0-9.,])"
    rf"(?P<suffix_number>{_DECIMAL_TOKEN})\s*(?P<suffix>USD\b|美元)"
    rf"(?![A-Za-z0-9.,])"
)
_EXPLICIT_SCALE = re.compile(r"(?i)^\s*(?:(?:thousand|million|billion)\b|[万亿])")


def _format_unquoted_usd(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        number = match.group("prefix_number") or match.group("suffix_number")
        if _EXPLICIT_SCALE.match(text[match.end():]):
            return match.group(0)
        value = _decimal(number)
        if abs(value) < Decimal("10000"):
            return match.group(0)
        return format_display_number(value, kind="amount_usd")
    return _BASE_USD.sub(replace, text)


def _is_escaped_quote(text: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def format_prose_usd_amounts(text: str) -> str:
    """Format explicit base-USD amounts in reviewed Chinese presentation prose.

    Markdown quote lines, quoted source text, pure-English lines, small values,
    and already-scaled amounts remain byte-for-byte unchanged.
    """
    if not isinstance(text, str):
        raise ValueError("display prose must be a string")
    rendered = []
    quote_end = None
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content):]
        if content.lstrip().startswith(">"):
            rendered.append(line)
            continue
        if quote_end is None and not _HAN.search(content):
            rendered.append(line)
            continue
        pieces = []
        start = 0
        index = 0
        while index < len(content):
            char = content[index]
            if quote_end is None and char in {'"', '“'}:
                pieces.append(_format_unquoted_usd(content[start:index]))
                quote_end = '"' if char == '"' else '”'
                start = index
            elif (quote_end is not None and char == quote_end
                  and not (char == '"' and _is_escaped_quote(content, index))):
                pieces.append(content[start:index + 1])
                start = index + 1
                quote_end = None
            index += 1
        tail = content[start:]
        pieces.append(tail if quote_end is not None else _format_unquoted_usd(tail))
        rendered.append("".join(pieces) + ending)
    return "".join(rendered)


_ISO_DATE_RANGE = re.compile(
    r"(?<![A-Za-z0-9])(?P<start>\d{4}-\d{2}-\d{2})\.\."
    r"(?P<end>\d{4}-\d{2}-\d{2})(?![A-Za-z0-9])"
)


def _format_unquoted_date_ranges(text: str) -> str:
    from datetime import date

    def replace(match: re.Match[str]) -> str:
        try:
            start = date.fromisoformat(match.group("start"))
            end = date.fromisoformat(match.group("end"))
        except ValueError:
            return match.group(0)
        if start > end:
            return match.group(0)
        return (f"{start.year}年{start.month}月{start.day}日"
                f"至{end.year}年{end.month}月{end.day}日")

    return _ISO_DATE_RANGE.sub(replace, text)


def format_prose_date_ranges(text: str) -> str:
    """Format strict ISO date ranges in reviewed prose, outside quotations."""
    if not isinstance(text, str):
        raise ValueError("display prose must be a string")
    rendered = []
    quote_end = None
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content):]
        if content.lstrip().startswith(">"):
            rendered.append(line)
            continue
        pieces = []
        start = 0
        index = 0
        while index < len(content):
            char = content[index]
            if quote_end is None and char in {'"', '“'}:
                pieces.append(_format_unquoted_date_ranges(content[start:index]))
                quote_end = '"' if char == '"' else '”'
                start = index
            elif (quote_end is not None and char == quote_end
                  and not (char == '"' and _is_escaped_quote(content, index))):
                pieces.append(content[start:index + 1])
                start = index + 1
                quote_end = None
            index += 1
        tail = content[start:]
        pieces.append(tail if quote_end is not None else _format_unquoted_date_ranges(tail))
        rendered.append("".join(pieces) + ending)
    return "".join(rendered)


def transform_unquoted_prose(text: str, transform: Callable[[str], str]) -> str:
    """Apply a display transform outside Markdown and typographic quotations."""
    if not isinstance(text, str):
        raise ValueError("display prose must be a string")
    rendered = []
    quote_end = None
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content):]
        if content.lstrip().startswith(">"):
            rendered.append(line)
            continue
        pieces, start = [], 0
        for index, char in enumerate(content):
            if quote_end is None and char in {'"', '“'}:
                pieces.append(transform(content[start:index]))
                quote_end = '"' if char == '"' else '”'
                start = index
            elif (quote_end is not None and char == quote_end
                  and not (char == '"' and _is_escaped_quote(content, index))):
                pieces.append(content[start:index + 1])
                start = index + 1
                quote_end = None
        tail = content[start:]
        pieces.append(tail if quote_end is not None else transform(tail))
        rendered.append("".join(pieces) + ending)
    return "".join(rendered)


__all__ = ["NumericKind", "format_display_number", "format_prose_date_ranges", "format_prose_usd_amounts",
           "transform_unquoted_prose",
           "format_typed_value"]


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
