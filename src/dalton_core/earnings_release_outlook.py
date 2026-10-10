"""Batch B: what an earnings release says about bookings and the outlook.

ADR-0015's intake (``earnings_release_intake``) reads the income-statement
table out of the 8-K's Exhibit 99.1 and stops there.  The parts of a release a
PM reads first are in its prose and its outlook table: new bookings and
book-to-bill, local-currency growth, whether revenue landed inside the guided
range, and next quarter's and next year's guidance.  This module reads those,
deterministically, from the same exhibit bytes -- found through the intake's
public ``earnings_release_reads`` records and re-hashed against the spool --
and never edits or imports the intake's parser.

Every record is typed (``kind``, ``metric``, ``period``, ``low``/``high`` or
``value``, ``unit``, ``scale``) and carries the sentence it was read from as
provenance: the exact characters of the plain-text rendering, their span, and
the SHA-256 of the exhibit body.  Each figure's digits are re-found inside its
quote before the record is admitted.  A release that yields nothing is a
record with no items and a reason, not an error.

Stored append-only in ``earnings_release_outlooks``: one row per (exhibit body,
parser version).  A new parser version reads the same exhibit again and
appends; nothing is updated.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from html import unescape
from pathlib import Path
from typing import Any

from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
PARSER_VERSION = "earnings-release-outlook:batch-b:0.1"
_SCHEMA_PATH = Path(__file__).with_name("earnings_release_outlook_schema.sql")
MAX_QUOTE_CHARS = 600
MAX_ITEMS = 80

KINDS = ("bookings", "book_to_bill", "reported", "growth", "guidance", "guidance_vs_actual")
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_SCALES = {"billion": "billion", "b": "billion", "bn": "billion", "million": "million",
           "m": "million", "mm": "million", "thousand": "thousand", "k": "thousand"}


class EarningsReleaseOutlookError(RuntimeError):
    """The exhibit cannot be read as an earnings release."""


# ---------------------------------------------------------------------------
# plain text with stable offsets
# ---------------------------------------------------------------------------

_BLOCK_TAGS = re.compile(r"(?i)<\s*(?:br|/p|/div|/tr|/li|/h[1-6]|/table)\b[^>]*>")
_CELL_TAGS = re.compile(r"(?i)<\s*/t[dh]\s*>")


def plain_text(raw: bytes | str) -> str:
    """The exhibit as lines of text, deterministic for the same bytes.

    Block ends become newlines and table cells become `` | `` so an outlook
    table row reads ``Revenues | $18.95B – $19.60B``.  Whitespace inside a line
    collapses to one space.
    """

    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    text = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", text)
    text = _BLOCK_TAGS.sub("\n", text)
    text = _CELL_TAGS.sub(" | ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text).replace("\xa0", " ").replace("​", "")
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        line = re.sub(r"(?:\|\s*){2,}", "| ", line).strip(" |")
        if line:
            lines.append(line)
    return "\n".join(lines)


def _sentences(text: str) -> list[tuple[int, int]]:
    """Sentence spans: a line, split further at '. ' followed by a capital."""

    spans: list[tuple[int, int]] = []
    offset = 0
    for line in text.split("\n"):
        start = offset
        for match in re.finditer(r"(?<=[a-z0-9%)])\.\s+(?=[A-Z•“\"])", line):
            spans.append((start, offset + match.start() + 1))
            start = offset + match.end()
        if start < offset + len(line):
            spans.append((start, offset + len(line)))
        offset += len(line) + 1
    return spans


# ---------------------------------------------------------------------------
# periods
# ---------------------------------------------------------------------------

_QUARTER_PHRASE = re.compile(
    r"(?i)\b(?:the\s+)?(first|second|third|fourth)[ -]quarter\s+(?:of\s+)?(?:fiscal(?:\s+year)?\s+)?(\d{4})")
_QUARTER_SHORT = re.compile(r"(?i)\bQ([1-4])\s*(?:FY|fiscal\s*)?'?(\d{2,4})\b")
_YEAR_PHRASE = re.compile(r"(?i)\b(?:full[ -]year\s+)?fiscal(?:\s+year)?\s+(\d{4})\b|\bFY\s?'?(\d{2,4})\b")


def _year(text: str) -> int:
    value = int(text)
    return value + 2000 if value < 100 else value


def _period_in(text: str) -> dict[str, Any] | None:
    found = _QUARTER_PHRASE.search(text)
    if found:
        return {"kind": "quarter", "fiscal_year": int(found.group(2)),
                "fiscal_quarter": _ORDINALS[found.group(1).lower()]}
    found = _QUARTER_SHORT.search(text)
    if found:
        return {"kind": "quarter", "fiscal_year": _year(found.group(2)),
                "fiscal_quarter": int(found.group(1))}
    found = _YEAR_PHRASE.search(text)
    if found:
        return {"kind": "fiscal_year", "fiscal_year": _year(found.group(1) or found.group(2)),
                "fiscal_quarter": None}
    return None


def _label(period: Mapping[str, Any] | None) -> str | None:
    if not period:
        return None
    if period.get("fiscal_quarter"):
        return f"FY{period['fiscal_year']}Q{period['fiscal_quarter']}"
    return f"FY{period['fiscal_year']}"


def _period_end(period: Mapping[str, Any] | None, fye_month: int | None) -> str | None:
    if not period or not fye_month:
        return None
    from .financial_period import month_end, shift_months

    end = month_end(int(period["fiscal_year"]), fye_month)
    if period.get("fiscal_quarter"):
        end = shift_months(end, -3 * (4 - int(period["fiscal_quarter"])))
    return end.isoformat()


# ---------------------------------------------------------------------------
# figures
# ---------------------------------------------------------------------------

_NUM = r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?"
_AMOUNT = re.compile(
    r"(?P<neg>negative\s+|\()?(?P<cur>US\$|\$|€|£)?\s?(?P<num>" + _NUM + r")\s?"
    r"(?P<unit>%|bps|basis points|billion|million|thousand|bn|B|M|K)?(?![\w.])", re.I)
_DASH = r"\s*(?:–|—|-|to)\s*"


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _figure(match: re.Match[str]) -> dict[str, Any]:
    unit = (match.group("unit") or "").lower()
    value = _decimal(match.group("num"))
    if value is not None and match.group("neg"):
        value = -abs(value)
    if unit == "%":
        kind, scale = "percent", "one"
    elif unit in {"bps", "basis points"}:
        kind, scale = "bps", "one"
    else:
        kind = "currency" if match.group("cur") else "number"
        scale = _SCALES.get(unit, "one")
    return {"value": value, "unit": kind, "scale": scale,
            "currency": "USD" if match.group("cur") in {"$", "US$"} else (
                {"€": "EUR", "£": "GBP"}.get(match.group("cur") or "")),
            "digits": match.group("num")}


def _range(text: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """``$18.95B – $19.60B`` / ``3% to 6%`` / ``$14.39 to $14.81``."""

    pattern = re.compile(r"(?P<a>(?:US\$|\$)?\s?(?:" + _NUM + r")\s?(?:%|B|M|K|bn|billion|million|bps)?)"
                         + _DASH + r"(?P<b>(?:US\$|\$)?\s?(?:" + _NUM + r")\s?(?:%|B|M|K|bn|billion|million|bps)?)"
                         r"(?![\w.])")
    found = pattern.search(text)
    if not found:
        return None
    low = _AMOUNT.fullmatch(found.group("a").strip())
    high = _AMOUNT.fullmatch(found.group("b").strip())
    if not low or not high:
        return None
    first, second = _figure(low), _figure(high)
    # "3% to 6%": the unit is written on both ends; "3 to 6%" carries it once.
    if first["unit"] == "number" and second["unit"] != "number":
        first.update(unit=second["unit"], scale=second["scale"], currency=second["currency"])
    if first["unit"] == "currency" and second["unit"] == "currency" and first["scale"] == "one":
        first["scale"] = second["scale"]
    if first["value"] is None or second["value"] is None or first["value"] > second["value"]:
        return None
    return first, second


_METRICS = (
    ("eps", re.compile(r"(?i)\b(?:diluted\s+)?(?:earnings per share|EPS)\b")),
    ("operating_margin", re.compile(r"(?i)\boperating margin\b")),
    ("tax_rate", re.compile(r"(?i)\b(?:effective )?tax rate\b")),
    ("free_cash_flow", re.compile(r"(?i)\bfree cash flow\b")),
    ("operating_cash_flow", re.compile(r"(?i)\boperating cash flow\b")),
    ("capex", re.compile(r"(?i)\b(?:property\s*&\s*equipment additions|capital expenditures?|capex)\b")),
    ("capital_return", re.compile(r"(?i)\b(?:capital return|return (?:at least )?\$?[\d.]+\s*(?:billion|B)? in cash to shareholders)\b")),
    ("fx_impact", re.compile(r"(?i)\bforeign[- ]exchange impact\b")),
    ("revenue_growth_lc", re.compile(r"(?i)\brevenue growth\b[^|\n]{0,30}\blocal currency\b|\blocal currency\b[^|\n]{0,20}\brevenue growth\b")),
    ("revenue", re.compile(r"(?i)\brevenues?\b")),
)


def _metric_of(label: str) -> str | None:
    for metric, pattern in _METRICS:
        if pattern.search(label):
            return metric
    return None


# ---------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------


def _item(text: str, span: tuple[int, int], **fields: Any) -> dict[str, Any] | None:
    start, end = span
    quote = text[start:end]
    if len(quote) > MAX_QUOTE_CHARS:
        quote = quote[:MAX_QUOTE_CHARS]
        end = start + MAX_QUOTE_CHARS
    for key in ("value", "low", "high"):
        figure = fields.get(key)
        if isinstance(figure, Mapping) and figure.get("digits") and figure["digits"] not in quote:
            return None  # digits not in the cited sentence: refuse the item
    out = {"quote": quote, "span": [start, end],
           "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest()}
    for key, value in fields.items():
        if isinstance(value, Mapping) and "digits" in value:
            out[key] = None if value["value"] is None else format(value["value"], "f")
            out.setdefault("unit", value["unit"])
            out.setdefault("scale", value["scale"])
            if value.get("currency"):
                out.setdefault("currency", value["currency"])
        else:
            out[key] = value
    return out


_BOOKINGS = re.compile(
    r"(?i)(?:(?P<segment>[A-Z][A-Za-z&\- ]{2,40}?)\s+)?new bookings\s+(?:for\s+(?P<period>[^,$]{3,60}?)\s+)?"
    r"were\s+(?P<amount>\$\s?[\d.,]+\s?(?:billion|million|B|M))"
    r"(?:,\s+an?\s+(?P<dir>increase|decrease)\s+of\s+(?P<usd>\d+(?:\.\d+)?)%\s+in\s+U\.S\.\s+dollars"
    r"(?:\s+and\s+(?P<lc>\d+(?:\.\d+)?)%\s+in\s+local\s+currency)?)?"
    r"(?:[^.]*?book-to-bill\s+of\s+(?P<btb>\d+(?:\.\d+)?))?")
_REVENUE = re.compile(
    r"(?i)\brevenues\s+for\s+(?P<period>[^,$]{3,60}?)\s+were\s+(?P<amount>\$\s?[\d.,]+\s?(?:billion|million|B|M))"
    r"(?:,\s+an?\s+(?P<dir>increase|decrease)\s+of\s+(?P<usd>\d+(?:\.\d+)?)%\s+in\s+U\.S\.\s+dollars"
    r"(?:\s+and\s+(?P<lc>\d+(?:\.\d+)?)%\s+in\s+local\s+currency)?)?"
    r"(?:,?\s+and\s+(?:were|was)\s+(?P<versus>above|below|within|in line with|at the (?:high|low|top|bottom) end of)"
    r"\s+the\s+company[’']s\s+guided\s+range\s+of\s+(?P<range>.*))?")
_EPS_REPORTED = re.compile(
    r"(?i)(?P<basis>GAAP diluted|adjusted|diluted)\s+EPS\s+(?:for\s+(?P<period>[^,$]{3,60}?)\s+)?"
    r"(?:were|was|of)\s+(?P<amount>\$\s?\d+\.\d+)"
    r"(?:\s+for\s+(?P<after_period>(?:the\s+)?(?:first|second|third|fourth)\s+quarter[^,.;]*|fiscal\s+\d{4}))?"
    r"(?:,\s+(?:a|an increase of)\s+\d+%\s+(?:increase\s+)?over\s+(?:(?P<prior_basis>adjusted|GAAP)\s+EPS\s+of\s+)?"
    r"(?P<prior>\$\s?\d+\.\d+)\s+for\s+(?P<prior_period>[^,.]{3,60}?)(?:[,.]|$))?")
_EPS_OVER_ADJ = re.compile(
    r"(?i)over\s+adjusted\s+EPS\s+of\s+(?P<prior>\$\s?\d+\.\d+)\s+for\s+(?P<prior_period>[^,.]{3,60}?)(?:[,.]|$)")
_GUIDANCE_PROSE = re.compile(
    r"(?i)\b(?:expects?|expected|guid(?:es|ed|ing|ance)|outlook|anticipates?|projects?)\b")
_OUTLOOK_HEADING = re.compile(
    r"(?i)^(?:(?P<q>first|second|third|fourth)\s+quarter|full[ -]year|fiscal(?:\s+year)?)\s*(?:fiscal\s+)?(?:year\s+)?"
    r"(?:fiscal\s+)?(?P<year>\d{4})\s+(?:business\s+)?outlook\s*$")


def _amount(text: str) -> dict[str, Any] | None:
    found = _AMOUNT.search(text)
    return None if found is None else _figure(found)


def _pct(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {"value": _decimal(value), "unit": "percent", "scale": "one", "currency": None,
            "digits": value}


def _period_before(sentence: str, position: int,
                   release_period: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The period a figure at ``position`` belongs to, from the words before it."""

    prefix = sentence[:position]
    found = _period_in(prefix)
    if found:
        return found
    lowered = prefix.lower()
    if release_period and re.search(r"\bfull[ -]year\b|\bfor the year\b", lowered):
        return {"kind": "fiscal_year", "fiscal_year": release_period["fiscal_year"],
                "fiscal_quarter": None}
    if release_period and "quarter" in lowered:
        return dict(release_period)
    return None


def _guidance_year(release_period: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """"full-year" in a release's outlook: the next fiscal year after a Q4 release."""

    if not release_period:
        return None
    year = int(release_period["fiscal_year"]) + (1 if release_period.get("fiscal_quarter") == 4 else 0)
    return {"kind": "fiscal_year", "fiscal_year": year, "fiscal_quarter": None}


def parse_release(text: str, *, fye_month: int | None = None,
                  release_period: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Typed outlook/bookings/headline records from one release's plain text.

    ``release_period`` (``{"kind","fiscal_year","fiscal_quarter"}``) names the
    quarter the release reports, used when a sentence says "the quarter".
    """

    items: list[dict[str, Any]] = []

    def add(span: tuple[int, int], period: Mapping[str, Any] | None, **fields: Any) -> None:
        if len(items) >= MAX_ITEMS:
            return
        record = _item(text, span, period=_label(period),
                       period_kind=None if not period else period["kind"],
                       period_end=_period_end(period, fye_month), **fields)
        if record is not None:
            items.append(record)

    bookings_period: dict[str, Any] | None = None
    for start, end in _sentences(text):
        sentence = text[start:end]
        lowered = sentence.lower()
        context_period = _period_in(sentence) or (release_period if "quarter" in lowered else None)
        if re.search(r"(?i)\bnew bookings\b|\bfinancial review\b|\brevenues\b", sentence) and not \
                _BOOKINGS.search(sentence):
            bookings_period = None
        # -- bookings and book-to-bill
        for found in _BOOKINGS.finditer(sentence):
            segment = (found.group("segment") or "").strip()
            if segment.lower() in {"the", "total", "q4", "q3", "q2", "q1"} or segment.lower().endswith(" of"):
                segment = ""
            period = _period_in(found.group("period") or "")
            if period is None and segment:
                period = bookings_period
            period = period or context_period
            if not segment:
                bookings_period = period
            amount = _amount(found.group("amount"))
            metric = "bookings" + (f":{segment.lower().replace(' ', '_')}" if segment else "")
            add((start, end), period, kind="bookings", metric=metric, value=amount, basis="reported")
            if found.group("usd"):
                sign = -1 if (found.group("dir") or "").lower() == "decrease" else 1
                usd = _pct(found.group("usd"))
                usd["value"] = sign * usd["value"]
                add((start, end), period, kind="growth", metric=f"{metric}_growth_usd", value=usd,
                    basis="reported")
                if found.group("lc"):
                    lc = _pct(found.group("lc"))
                    lc["value"] = sign * lc["value"]
                    add((start, end), period, kind="growth", metric=f"{metric}_growth_lc", value=lc,
                        basis="reported")
            if found.group("btb"):
                add((start, end), period, kind="book_to_bill",
                    metric="book_to_bill" + (f":{segment.lower().replace(' ', '_')}" if segment else ""),
                    value={"value": _decimal(found.group("btb")), "unit": "ratio", "scale": "one",
                           "currency": None, "digits": found.group("btb")}, basis="reported")
        # -- revenue, growth, versus guidance
        for found in _REVENUE.finditer(sentence):
            period = _period_in(found.group("period")) or context_period
            add((start, end), period, kind="reported", metric="revenue",
                value=_amount(found.group("amount")), basis="reported")
            sign = -1 if (found.group("dir") or "").lower() == "decrease" else 1
            for key, metric in (("usd", "revenue_growth_usd"), ("lc", "revenue_growth_lc")):
                if found.group(key):
                    figure = _pct(found.group(key))
                    figure["value"] = sign * figure["value"]
                    add((start, end), period, kind="growth", metric=metric, value=figure,
                        basis="reported")
            if found.group("versus") and found.group("range"):
                bounds = _range(found.group("range"))
                if bounds and bounds[0]["unit"] != "percent":
                    add((start, end), period, kind="guidance_vs_actual", metric="revenue_prior_guidance",
                        low=bounds[0], high=bounds[1], position=found.group("versus").lower(),
                        basis="prior_guidance")
                lc_range = re.search(r"(\d+(?:\.\d+)?)%\s*to\s*(\d+(?:\.\d+)?)%\s+growth\s+in\s+local\s+currency",
                                     found.group("range"))
                if lc_range:
                    add((start, end), period, kind="guidance_vs_actual",
                        metric="revenue_growth_lc_prior_guidance",
                        low=_pct(lc_range.group(1)), high=_pct(lc_range.group(2)),
                        position=found.group("versus").lower(), basis="prior_guidance")
        # -- reported EPS, GAAP and adjusted, with the prior year it is compared to
        for found in _EPS_REPORTED.finditer(sentence):
            if "expect" in lowered or re.search(r"(?i)\bover\s+$", sentence[:found.start()]):
                continue
            period = (_period_in(found.group("period") or "")
                      or _period_in(found.group("after_period") or "")
                      or _period_before(sentence, found.start("amount"), release_period))
            basis = "adjusted" if found.group("basis").lower() == "adjusted" else "gaap"
            add((start, end), period, kind="reported", metric=f"eps_{basis}",
                value=_amount(found.group("amount")), basis="reported")
            if found.group("prior") and found.group("prior_period"):
                prior_basis = (found.group("prior_basis") or found.group("basis")).lower()
                add((start, end), _period_in(found.group("prior_period")), kind="reported",
                    metric="eps_adjusted" if prior_basis == "adjusted" else "eps_gaap",
                    value=_amount(found.group("prior")), basis="reported_prior_year")
        if "eps" in lowered and "increased" in lowered:
            for found in _EPS_OVER_ADJ.finditer(sentence):
                add((start, end), _period_in(found.group("prior_period")), kind="reported",
                    metric="eps_adjusted", value=_amount(found.group("prior")),
                    basis="reported_prior_year")
        # -- guidance in prose: "expects full-year revenue growth of 3% to 6% in local currency"
        if _GUIDANCE_PROSE.search(sentence) and not sentence.lower().startswith(("revenues for", "new bookings")):
            period = _period_in(sentence)
            if period is None and re.search(r"(?i)full[ -]year|fiscal year", sentence):
                period = _guidance_year(release_period)
            elif period is None and re.search(r"(?i)next quarter|first quarter|current quarter", sentence):
                period = None
            metric = None
            for clause in re.split(r"(?i);|\band expects\b|, and\b", sentence):
                metric = _metric_of(clause) or metric
                bounds = _range(clause)
                if metric and bounds:
                    if metric == "revenue" and "local currency" in clause.lower():
                        metric = "revenue_growth_lc"
                    elif metric == "revenue" and bounds[0]["unit"] == "percent":
                        metric = "revenue_growth"
                    elif metric == "eps" and bounds[0]["unit"] == "percent":
                        metric = ("eps_growth_vs_adjusted" if "adjusted" in clause.lower()
                                  else "eps_growth")
                    add((start, end), period, kind="guidance", metric=metric, low=bounds[0],
                        high=bounds[1], basis="company_guidance", source="prose")
    items.extend(_outlook_table(text, fye_month, budget=MAX_ITEMS - len(items)))
    return _dedupe(items)


def _outlook_table(text: str, fye_month: int | None, *, budget: int) -> list[dict[str, Any]]:
    """The ``Business Outlook`` block: heading, then label/value line pairs."""

    out: list[dict[str, Any]] = []
    lines = text.split("\n")
    offsets, position = [], 0
    for line in lines:
        offsets.append(position)
        position += len(line) + 1
    period: dict[str, Any] | None = None
    pending: tuple[int, str] | None = None
    heading_line = ""
    for index, line in enumerate(lines):
        heading = _OUTLOOK_HEADING.match(line)
        if heading:
            heading_line = line
            period = {"kind": "quarter" if heading.group("q") else "fiscal_year",
                      "fiscal_year": int(heading.group("year")),
                      "fiscal_quarter": _ORDINALS[heading.group("q").lower()] if heading.group("q") else None}
            pending = None
            continue
        if period is None:
            continue
        if re.match(r"(?i)^(conference call|about |forward-looking|non-gaap|\d+$)", line):
            if not re.match(r"^\d+$", line):
                period = None
            continue
        metric = _metric_of(line) if not re.search(r"\d", line) or "(" in line else None
        if metric and not _range(line) and not _AMOUNT.search(line.replace("Fiscal", "")):
            pending = (index, line)
            continue
        if pending is None:
            continue
        label_index, label = pending
        metric = _metric_of(label)
        if metric == "revenue" and "local currency" in label.lower():
            metric = "revenue_growth_lc"
        start, end = offsets[label_index], offsets[index] + len(line)
        bounds = _range(line)
        record: dict[str, Any] | None
        common = dict(period=_label(period), period_kind=period["kind"],
                      period_end=_period_end(period, fye_month), kind="guidance", metric=metric,
                      basis="company_guidance", source="outlook_table", label_as_printed=label,
                      heading_as_printed=heading_line)
        if bounds:
            record = _item(text, (start, end), low=bounds[0], high=bounds[1], **common)
        elif re.search(r"(?i)\bflat\b", line):
            record = _item(text, (start, end), value={"value": Decimal(0), "unit": "percent",
                                                      "scale": "one", "currency": None, "digits": ""},
                           qualifier="flat", **common)
        else:
            found = _AMOUNT.search(line)
            if found is None:
                pending = None
                continue
            qualifier = ("at_least" if re.search(r"(?i)at least", line) else
                         "approximately" if re.search(r"(?i)approx", line) else None)
            record = _item(text, (start, end), value=_figure(found), qualifier=qualifier, **common)
        if record is not None and len(out) < budget:
            out.append(record)
        pending = None
    return out


def _dedupe(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for item in items:
        key = canonical_json({k: item.get(k) for k in
                              ("kind", "metric", "period", "value", "low", "high", "basis")})
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(item))
    return out


def actuals_for_calendar(items: Sequence[Mapping[str, Any]], ref: str) -> list[dict[str, Any]]:
    """Reported headline figures in ``financial_period.held_actuals`` ``extra`` form.

    Adjusted and GAAP EPS from the release narrative are what a vendor's
    year-ago EPS is usually built on; revenue is scaled to currency units.
    """

    scale = {"one": Decimal(1), "thousand": Decimal(1000), "million": Decimal(1_000_000),
             "billion": Decimal(1_000_000_000)}
    out = []
    for item in items:
        if item.get("kind") != "reported" or not item.get("period_end") or item.get("value") is None:
            continue
        metric = str(item.get("metric"))
        if metric.startswith("eps_"):
            out.append({"metric": "eps", "kind": item.get("period_kind"), "period_end": item["period_end"],
                        "value": item["value"], "basis": f"press_release_{metric}", "ref": ref})
        elif metric == "revenue":
            value = Decimal(str(item["value"])) * scale.get(str(item.get("scale")), Decimal(1))
            out.append({"metric": "revenue", "kind": item.get("period_kind"),
                        "period_end": item["period_end"], "value": value,
                        "basis": "press_release_rounded", "ref": ref})
    return out


# ---------------------------------------------------------------------------
# reading the intake's records and storing outlooks
# ---------------------------------------------------------------------------


def _query(connection: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    from .financial_period import query

    return query(connection, sql, params)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    from .financial_period import _table_exists as exists

    return exists(connection, name)


def intake_reads(connection: sqlite3.Connection, company_ref: str) -> list[dict[str, Any]]:
    """The intake's parsed reads for one company, newest first (public records only)."""

    if not _table_exists(connection, "earnings_release_reads"):
        return []
    from .financial_period import _refs

    refs = _refs(company_ref)
    rows = _query(connection,
                  f"SELECT record_json FROM earnings_release_reads WHERE company_ref IN ({','.join('?' * len(refs))}) "
                  "AND status='parsed' AND body_sha256 IS NOT NULL ORDER BY created_at DESC", refs)
    out, seen = [], set()
    for row in rows:
        record = json.loads(row["record_json"])
        if record.get("body_sha256") in seen:
            continue
        seen.add(record.get("body_sha256"))
        out.append(record)
    return out


def exhibit_bytes(state_dir: Path | str, body_sha256: str) -> tuple[bytes | None, str | None]:
    """The exhibit body from the owner-only spool, re-hashed; gzip unwrapped."""

    from .acquired_sec_filings import _spool_bytes

    raw, reason = _spool_bytes(Path(state_dir), body_sha256)
    if raw is not None and raw.startswith(b"\x1f\x8b"):
        import gzip

        try:
            raw = gzip.decompress(raw)
        except OSError:
            return None, "附件内容压缩损坏"
    return raw, reason


def _release_period(read: Mapping[str, Any]) -> dict[str, Any] | None:
    found = re.fullmatch(r"FY(\d{4})-Q([1-4])", str(read.get("period") or ""))
    if not found:
        return None
    return {"kind": "quarter", "fiscal_year": int(found.group(1)), "fiscal_quarter": int(found.group(2))}


def outlook_record(read: Mapping[str, Any], raw: bytes, *, fye_month: int | None) -> dict[str, Any]:
    """One release's outlook record (unhashed body) from its exhibit bytes."""

    digest = hashlib.sha256(raw).hexdigest()
    if read.get("body_sha256") and digest != read["body_sha256"]:
        raise EarningsReleaseOutlookError("exhibit bytes do not match the intake's recorded SHA-256")
    text = plain_text(raw)
    items = parse_release(text, fye_month=fye_month, release_period=_release_period(read))
    return {
        "schema_version": SCHEMA_VERSION, "parser_version": PARSER_VERSION,
        "company_ref": read.get("company_ref"), "accession": read.get("accession"),
        "document_ref": read.get("document_ref") or f"sec:filing:{read.get('accession')}",
        "exhibit_url": read.get("exhibit_url"), "body_sha256": digest,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text_chars": len(text),
        "release_period": read.get("period"), "report_date": read.get("report_date"),
        "intake_read_ref": read.get("id"), "items": items,
        "item_count": len(items),
        "reason": None if items else "附件里没有读到 bookings、增速或指引的句子",
        "basis": "company-press-release-preliminary",
    }


class EarningsReleaseOutlookStore:
    """``earnings_release_outlooks``: append-only, one row per (body, parser version)."""

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("EarningsReleaseOutlookStore requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def record(self, body: Mapping[str, Any], *, now: str | None = None) -> dict[str, Any]:
        existing = self.connection.execute(
            "SELECT record_json FROM earnings_release_outlooks WHERE body_sha256=? AND parser_version=?",
            (body["body_sha256"], body["parser_version"])).fetchone()
        if existing is not None:
            return {"status": "duplicate", "record": json.loads(existing[0])}
        wire = dict(body)
        wire["created_at"] = now or datetime.now(timezone.utc).isoformat(timespec="microseconds")
        wire["id"] = "earnings-release-outlook:" + content_hash(
            {"body": wire["body_sha256"], "parser": wire["parser_version"]})[:32]
        wire["content_hash"] = content_hash(wire)
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO earnings_release_outlooks (outlook_id, company_ref, accession, body_sha256, "
                "parser_version, release_period, item_count, record_json, content_hash, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (wire["id"], wire["company_ref"], wire["accession"], wire["body_sha256"],
                 wire["parser_version"], wire.get("release_period"), wire["item_count"],
                 canonical_json(wire), wire["content_hash"], wire["created_at"]))
        return {"status": "recorded", "record": wire}

    def ingest_company(self, state_dir: Path | str, company_ref: str) -> list[dict[str, Any]]:
        """Read every parsed intake exhibit for one company not yet read by this parser."""

        from .financial_period import fiscal_year_end

        fye = fiscal_year_end(self.connection, company_ref)
        results = []
        for read in intake_reads(self.connection, company_ref):
            raw, reason = exhibit_bytes(state_dir, str(read["body_sha256"]))
            if raw is None:
                results.append({"status": "exhibit_unavailable", "accession": read.get("accession"),
                                "reason": reason})
                continue
            body = outlook_record(read, raw, fye_month=None if fye is None else fye["month"])
            results.append(self.record(body))
        return results


def latest_outlook(connection: sqlite3.Connection, company_ref: str, *,
                   state_dir: Path | str | None = None) -> dict[str, Any] | None:
    """The newest release's outlook: stored if recorded, else parsed read-only on the fly.

    The on-the-fly path writes nothing; it exists so a read-only reader (the
    cockpit, the numbers pack on a copy of the live state) sees the same record
    a writer would store.
    """

    from .financial_period import _refs, fiscal_year_end

    if _table_exists(connection, "earnings_release_outlooks"):
        refs = _refs(company_ref)
        rows = _query(connection,
                      f"SELECT record_json FROM earnings_release_outlooks WHERE company_ref IN "
                      f"({','.join('?' * len(refs))}) AND parser_version=? ORDER BY created_at DESC",
                      (*refs, PARSER_VERSION))
        if rows:
            records = [json.loads(row["record_json"]) for row in rows]
            return max(records, key=lambda r: (str(r.get("report_date") or ""), r["created_at"]))
    if state_dir is None:
        return None
    reads = intake_reads(connection, company_ref)
    if not reads:
        return None
    read = max(reads, key=lambda r: str(r.get("report_date") or ""))
    raw, reason = exhibit_bytes(state_dir, str(read["body_sha256"]))
    if raw is None:
        return {"status": "exhibit_unavailable", "accession": read.get("accession"), "reason": reason,
                "items": []}
    fye = fiscal_year_end(connection, company_ref)
    body = outlook_record(read, raw, fye_month=None if fye is None else fye["month"])
    body["stored"] = False
    return body


# ---------------------------------------------------------------------------
# one-off items (2026-10-09, batch B follow-up)
# ---------------------------------------------------------------------------
#
# What a release itself names as not recurring: legal and litigation charges,
# severance and restructuring ("business optimization"), impairments, gains on
# a sale.  Read from the same exhibit text with the same provenance as the
# outlook items (quote, span, quote SHA-256, exhibit body SHA-256), from
# sentences ("... include legal-related charges of $2.40 billion ...") and from
# table rows ("Restructuring charges | 1,180 | —" under "(in millions)").  Only
# currency amounts with a scale are admitted -- "$0.78 for business
# optimization costs" is a per-share effect, not an amount -- and a table row
# is read from its first (current-period) column only, so Accenture's Q4 FY26
# release, whose business-optimization column for the quarter is "—", yields
# nothing.  ``forecast_assumption_sources`` reads ``one_off_items`` by name.

ONE_OFF_PARSER_VERSION = "earnings-release-one-offs:batch-b:0.1"
ONE_OFF_KINDS = ("legal", "restructuring", "impairment", "gain_on_sale")
ONE_OFF_KIND_LABELS = {"legal": "法律/诉讼费用", "restructuring": "裁员/重组费用",
                       "impairment": "资产减值", "gain_on_sale": "出售资产收益"}
_ONE_OFF_PATTERNS = (
    ("legal", re.compile(r"(?i)\b(?:legal(?:[- ]related)?|litigation|settlements?|regulatory\s+(?:fines?|penalt(?:y|ies)))\b"
                         r"[^.|]{0,40}?\b(?:charges?|accruals?|expenses?|costs?|settlements?|matters?)\b")),
    ("restructuring", re.compile(r"(?i)\b(?:severance|restructuring|business optimization|workforce reductions?|"
                                 r"layoffs?|employee termination)\b")),
    ("impairment", re.compile(r"(?i)\bimpairments?\b")),
    ("gain_on_sale", re.compile(r"(?i)\bgains?\s+on\s+(?:the\s+)?(?:sale|disposal|divestiture)s?\b")),
)
# Where the release says the charge sits; otherwise the line the kind
# normally lands in (a legal accrual in G&A).  ``concept_basis`` says which.
_LINE_MENTIONS = (
    (re.compile(r"(?i)\bgeneral and administrative\b"), "us-gaap:GeneralAndAdministrativeExpense"),
    (re.compile(r"(?i)\bresearch and development\b"), "us-gaap:ResearchAndDevelopmentExpense"),
    (re.compile(r"(?i)\b(?:marketing and sales|sales and marketing|selling and marketing)\b"),
     "us-gaap:SellingAndMarketingExpense"),
    (re.compile(r"(?i)\bcost of (?:revenues?|sales|services)\b"), "us-gaap:CostOfRevenue"),
)
_DEFAULT_CONCEPT = {"legal": "us-gaap:GeneralAndAdministrativeExpense",
                    "restructuring": "us-gaap:RestructuringCharges",
                    "impairment": "us-gaap:AssetImpairmentCharges",
                    "gain_on_sale": "us-gaap:GainLossOnDispositionOfAssets"}
_SCALED_CURRENCY = re.compile(
    r"(?P<cur>US\$|\$)\s?(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s?"
    r"(?P<unit>billion|million|bn|B|M)(?![\w.])", re.I)
_FORWARD = re.compile(r"(?i)\b(?:expects?|expected|anticipates?|will|outlook|guidance|forward-looking)\b")
_TABLE_SCALE = re.compile(r"(?i)\bin\s+(billions|millions|thousands)\b")
_EMPTY_CELL = {"—", "–", "-", "— %", "$ —", "$—", "$ -"}


def _one_off_kind(text: str) -> tuple[str, re.Match[str]] | None:
    for kind, pattern in _ONE_OFF_PATTERNS:
        found = pattern.search(text)
        if found:
            return kind, found
    return None


def _one_off_concept(kind: str, text: str) -> tuple[str, str]:
    for pattern, concept in _LINE_MENTIONS:
        if pattern.search(text):
            return concept, "stated_line"
    return _DEFAULT_CONCEPT[kind], "default_for_kind"


def _one_off_item(text: str, span: tuple[int, int], *, kind: str, label: str, figure: Mapping[str, Any],
                  period: Mapping[str, Any] | None, fye_month: int | None, source: str) -> dict[str, Any] | None:
    concept, basis = _one_off_concept(kind, text[span[0]:span[1]])
    item = _item(text, span, kind=kind, label=label, amount=figure, period=_label(period),
                 period_kind=None if period is None else period["kind"],
                 period_end=_period_end(period, fye_month), concept=concept, concept_basis=basis,
                 source=source)
    if item is None:
        return None
    item["sentence"] = item["quote"]
    item["kind_label"] = ONE_OFF_KIND_LABELS[kind]
    return item


def parse_one_offs(text: str, *, fye_month: int | None = None,
                   release_period: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """One-off items named in one release's plain text (see the note above)."""

    items: list[dict[str, Any]] = []
    lines = text.split("\n")
    # sentences
    for start, end in _sentences(text):
        sentence = text[start:end]
        if "|" in sentence:
            continue  # table rows are read below
        hit = _one_off_kind(sentence)
        if hit is None or _FORWARD.search(sentence):
            continue
        kind, keyword = hit
        amounts = list(_SCALED_CURRENCY.finditer(sentence))
        if not amounts:
            continue
        after = [m for m in amounts if m.start() >= keyword.start()]
        chosen = after[0] if after else amounts[-1]
        matched = _AMOUNT.match(chosen.group(0))
        if matched is None:
            continue
        figure = _figure(matched)
        if figure.get("value") is None or figure["unit"] != "currency":
            continue
        period = _period_before(sentence, chosen.start(), release_period) or (
            dict(release_period) if release_period else None)
        label = keyword.group(0).strip()
        item = _one_off_item(text, (start, end), kind=kind, label=label, figure=figure, period=period,
                             fye_month=fye_month, source="sentence")
        if item is not None:
            items.append(item)
    # table rows: label | current-period value | ...
    offset = 0
    for index, line in enumerate(lines):
        line_start = offset
        offset += len(line) + 1
        if "|" not in line:
            continue
        cells = [cell.strip() for cell in line.split("|")]
        hit = _one_off_kind(cells[0])
        if hit is None or not cells[0] or _SCALED_CURRENCY.search(cells[0]):
            continue
        values = [cell for cell in cells[1:] if cell and cell not in {"$", "%"}]
        if not values or values[0] in _EMPTY_CELL:
            continue
        number = re.fullmatch(r"\$?\s?\(?(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\)?", values[0])
        if number is None:
            continue
        header = "\n".join(lines[max(0, index - 40):index])
        scales = _TABLE_SCALE.findall(header)
        if not scales:
            continue
        scale = {"billions": "billion", "millions": "million", "thousands": "thousand"}[scales[-1].lower()]
        value = _decimal(number.group(1))
        if value is None or value == 0:
            continue
        figure = {"value": value, "unit": "currency", "scale": scale, "currency": "USD",
                  "digits": number.group(1)}
        item = _one_off_item(text, (line_start, line_start + len(line)), kind=hit[0], label=cells[0],
                             figure=figure, period=dict(release_period) if release_period else None,
                             fye_month=fye_month, source="table_current_column")
        if item is not None:
            items.append(item)
    out, seen = [], set()
    for item in items:
        key = (item["kind"], item.get("period"), _scaled_amount(item))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[:MAX_ITEMS]


def _scaled_amount(item: Mapping[str, Any]) -> str | None:
    scale = {"one": Decimal(1), "thousand": Decimal(1000), "million": Decimal(1_000_000),
             "billion": Decimal(1_000_000_000)}
    value = item.get("amount")
    if value is None:
        return None
    amount = Decimal(str(value)) * scale.get(str(item.get("scale") or "one"), Decimal(1))
    return format(amount.normalize(), "f")


def one_off_record(read: Mapping[str, Any], raw: bytes, *, fye_month: int | None) -> dict[str, Any]:
    """One release's one-off record (unhashed body) from its exhibit bytes."""

    digest = hashlib.sha256(raw).hexdigest()
    if read.get("body_sha256") and digest != read["body_sha256"]:
        raise EarningsReleaseOutlookError("exhibit bytes do not match the intake's recorded SHA-256")
    text = plain_text(raw)
    items = parse_one_offs(text, fye_month=fye_month, release_period=_release_period(read))
    return {
        "schema_version": SCHEMA_VERSION, "parser_version": ONE_OFF_PARSER_VERSION,
        "company_ref": read.get("company_ref"), "accession": read.get("accession"),
        "document_ref": read.get("document_ref") or f"sec:filing:{read.get('accession')}",
        "exhibit_url": read.get("exhibit_url"), "body_sha256": digest,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text_chars": len(text),
        "release_period": read.get("period"), "report_date": read.get("report_date"),
        "intake_read_ref": read.get("id"), "items": items, "item_count": len(items),
        "reason": None if items else "业绩稿没有点名本期的一次性项目（法律/诉讼、裁员重组、减值、出售收益）",
        "basis": "company-press-release-preliminary",
    }


class EarningsReleaseOneOffStore:
    """``earnings_release_one_offs``: append-only, one row per (body, parser version)."""

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("EarningsReleaseOneOffStore requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def record(self, body: Mapping[str, Any], *, now: str | None = None) -> dict[str, Any]:
        existing = self.connection.execute(
            "SELECT record_json FROM earnings_release_one_offs WHERE body_sha256=? AND parser_version=?",
            (body["body_sha256"], body["parser_version"])).fetchone()
        if existing is not None:
            return {"status": "duplicate", "record": json.loads(existing[0])}
        wire = dict(body)
        wire["created_at"] = now or datetime.now(timezone.utc).isoformat(timespec="microseconds")
        wire["id"] = "earnings-release-one-offs:" + content_hash(
            {"body": wire["body_sha256"], "parser": wire["parser_version"]})[:32]
        wire["content_hash"] = content_hash(wire)
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO earnings_release_one_offs (one_off_id, company_ref, accession, body_sha256, "
                "parser_version, release_period, item_count, record_json, content_hash, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (wire["id"], wire["company_ref"], wire["accession"], wire["body_sha256"],
                 wire["parser_version"], wire.get("release_period"), wire["item_count"],
                 canonical_json(wire), wire["content_hash"], wire["created_at"]))
        return {"status": "recorded", "record": wire}

    def ingest_company(self, state_dir: Path | str, company_ref: str) -> list[dict[str, Any]]:
        from .financial_period import fiscal_year_end

        fye = fiscal_year_end(self.connection, company_ref)
        results = []
        for read in intake_reads(self.connection, company_ref):
            raw, reason = exhibit_bytes(state_dir, str(read["body_sha256"]))
            if raw is None:
                results.append({"status": "exhibit_unavailable", "accession": read.get("accession"),
                                "reason": reason})
                continue
            results.append(self.record(one_off_record(read, raw, fye_month=None if fye is None else fye["month"])))
        return results


def _period_matches(item: Mapping[str, Any], record: Mapping[str, Any], period: Any) -> bool:
    if period is None:
        return True
    wanted = str(period)
    compact = wanted.replace("-", "")
    return wanted in {str(item.get("period_end")), str(item.get("period")), str(record.get("release_period"))} \
        or compact == str(item.get("period") or "").replace("-", "")


def one_off_items(connection: sqlite3.Connection, company_ref: str, period: Any = None, *,
                  state_dir: Path | str | None = None) -> list[dict[str, Any]]:
    """One-off items across a company's held releases, newest period last.

    ``[{label, kind, amount, scale, unit, currency, period, period_kind,
    period_end, concept, concept_basis, sentence, quote, span, quote_sha256,
    sha256 (the exhibit body), accession, ref}]``.  Stored records are used
    when present (this parser version); any other held exhibit is parsed
    read-only on the fly from the spool beside the Core.  ``period`` filters
    by a ``FY2025Q4`` label, a period end, or the release period.
    """

    from .financial_period import _refs, fiscal_year_end

    records: dict[str, dict[str, Any]] = {}
    if _table_exists(connection, "earnings_release_one_offs"):
        refs = _refs(company_ref)
        for row in _query(connection,
                          f"SELECT record_json FROM earnings_release_one_offs WHERE company_ref IN "
                          f"({','.join('?' * len(refs))}) AND parser_version=? ORDER BY created_at",
                          (*refs, ONE_OFF_PARSER_VERSION)):
            record = json.loads(row["record_json"])
            records[str(record["body_sha256"])] = record
    if state_dir is None:
        try:
            from .earnings_release_intake import core_state_dir

            state_dir = core_state_dir(connection)
        except Exception:  # noqa: BLE001 - no spool: stored records only
            state_dir = None
    reads = intake_reads(connection, company_ref)
    if state_dir is not None and any(str(read.get("body_sha256")) not in records for read in reads):
        fye = fiscal_year_end(connection, company_ref)
        for read in reads:
            if str(read.get("body_sha256")) in records:
                continue
            raw, _reason = exhibit_bytes(state_dir, str(read["body_sha256"]))
            if raw is None:
                continue
            try:
                body = one_off_record(read, raw, fye_month=None if fye is None else fye["month"])
            except EarningsReleaseOutlookError:
                continue
            body["stored"] = False
            records[body["body_sha256"]] = body
    out = []
    for record in records.values():
        for item in record.get("items") or ():
            if not _period_matches(item, record, period):
                continue
            out.append({**item, "sha256": record.get("body_sha256"), "accession": record.get("accession"),
                        "release_period": record.get("release_period"),
                        "report_date": record.get("report_date"),
                        "ref": f"sec:filing:{record.get('accession')}",
                        "because": (f"{item.get('kind_label')} {item.get('amount')} {item.get('scale')}"
                                    f"（{item.get('period') or record.get('release_period')}，业绩稿原句）")})
    out.sort(key=lambda item: (str(item.get("period_end") or ""), str(item.get("kind")),
                               str(item.get("quote_sha256"))))
    return out


__all__ = [
    "EarningsReleaseOneOffStore", "EarningsReleaseOutlookError", "EarningsReleaseOutlookStore", "KINDS",
    "ONE_OFF_KINDS", "ONE_OFF_PARSER_VERSION", "PARSER_VERSION", "one_off_items", "one_off_record",
    "parse_one_offs",
    "actuals_for_calendar", "exhibit_bytes", "intake_reads", "latest_outlook", "outlook_record",
    "parse_release", "plain_text",
]
