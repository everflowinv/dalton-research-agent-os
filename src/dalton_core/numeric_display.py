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
            if quote_end is None and char in {'"', '“', '`'}:
                pieces.append(_format_unquoted_usd(content[start:index]))
                quote_end = {'"': '"', '“': '”', '`': '`'}[char]
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
    r"(?<![A-Za-z0-9/=?&#%_])(?P<start>\d{4}-\d{2}-\d{2})\.\."
    r"(?P<end>\d{4}-\d{2}-\d{2})(?![A-Za-z0-9/=?&#%_])"
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
            if quote_end is None and char in {'"', '“', '`'}:
                pieces.append(_format_unquoted_date_ranges(content[start:index]))
                quote_end = {'"': '"', '“': '”', '`': '`'}[char]
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
            if quote_end is None and char in {'"', '“', '`'}:
                pieces.append(transform(content[start:index]))
                quote_end = {'"': '"', '“': '”', '`': '`'}[char]
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


# 2026-10-09 (batch am, coordinator): no machine-precision number in analyst
# prose.  A derived-ratio Claim states 「毛利率为 0.577264（即 57.73%）」 and a
# drafter quotes it verbatim (numbers must match the Claim); an amount arrives
# as 20167113474.15073849 or 24033000000.  For display only, never for the
# stored value: a ratio glossed with its percent shows the percent; a decimal
# with more than four places is rounded (two places from 1 up, four
# significant digits below 1); an integer of seven or more digits (or a
# grouped one of 1 亿 and up) is scaled to 万 / 亿 with about four significant
# digits.  Identifiers are left alone: digits joined to letters, hyphens,
# colons, slashes or dots (accession numbers, refs), a leading zero (CIK) and
# an eight-digit date.  ``cockpit_v2/copy.js`` (humanizeNumbers) is the twin;
# tests/test_terminology.py checks they agree.
_RATIO_GLOSS = re.compile(
    r"(?<![A-Za-z0-9_.,])-?\d+\.\d{4,}\s*[（(]\s*即\s*(?P<pct>-?\d+(?:\.\d+)?)\s*%\s*[）)]")
_MACHINE_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.,:/\-])(?P<num>-?(?:[1-9]\d{0,2}(?:,\d{3}){2,}|[1-9]\d{6,}|\d+\.\d{5,}|\d{5,}\.\d+))"
    r"(?![A-Za-z0-9_.,:/\-])(?P<scale>\s*[万亿千百])?(?P<unit>\s*(?:美元|USD\b))?")


def _date_like(digits: str) -> bool:
    if len(digits) != 8 or digits[:2] not in {"19", "20"}:
        return False
    return 1 <= int(digits[4:6]) <= 12 and 1 <= int(digits[6:8]) <= 31


def _round_places(value: Decimal, places: int) -> str:
    return str(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def humanize_machine_numbers(text: str) -> str:
    """Machine-precision numbers in one display fragment, as a reader says them."""

    if not isinstance(text, str) or not text:
        return text
    out = _RATIO_GLOSS.sub(lambda m: f"{m.group('pct')}%", text)

    def replace(match: re.Match[str]) -> str:
        raw = match.group("num")
        if match.group("scale"):
            return match.group(0)
        digits = raw.lstrip("-").replace(",", "")
        whole = digits.split(".")[0]
        if "." not in digits and _date_like(digits):
            return match.group(0)
        value = Decimal(raw.replace(",", ""))
        unit = "美元" if match.group("unit") else ""
        magnitude = abs(value)
        if magnitude >= Decimal("100000000") and (len(whole) >= 7 or "," in raw):
            return _scaled_amount(value, Decimal("100000000"), "亿" + unit)
        if magnitude >= Decimal("10000") and len(whole) >= 5:
            return _scaled_amount(value, Decimal("10000"), "万" + unit)
        if "." not in digits:
            return match.group(0)
        if magnitude >= 1:
            shown = _round_places(value, 2)
        elif magnitude == 0:
            shown = "0"
        else:
            shown = _round_places(value, max(0, 3 - magnitude.adjusted()))
        return shown + (match.group("unit") or "")

    return _MACHINE_NUMBER.sub(replace, out)


def format_prose_machine_numbers(text: str) -> str:
    """:func:`humanize_machine_numbers` over prose, outside quotations."""

    return transform_unquoted_prose(text, humanize_machine_numbers)


__all__ = ["NumericKind", "format_display_number", "format_prose_date_ranges", "format_prose_usd_amounts",
           "transform_unquoted_prose", "humanize_machine_numbers",
           "format_prose_machine_numbers",
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
