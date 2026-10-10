"""Batch B (owner review 2026-10-09 §P1): the company-defined FCF bridge.

The valuation snapshot's free cash flow is the system definition -- trailing
operating cash flow minus purchases of property and equipment.  Companies
define it differently: DXC also deducts capitalised transformation/contract
and software spend and finance-lease payments; Meta deducts finance-lease
principal.  A yield on one definition beside a peer's on another is not a
comparison, and a quarter carrying a litigation receipt is not a run-rate.

``fcf_bridge`` puts three columns side by side, read-only:

* ``system``   -- XBRL TTM CFO - PP&E (through the last filed period);
* ``company``  -- the company's own FCF reconciliation read out of its
  earnings release (each component row with its printed label, value and the
  quoted lines), rolled to a trailing year: a Q4 release's fiscal-year column
  is the trailing year; a Q1 release rolls ``FY + Q1 - prior-year Q1``;
* ``company_ex_one_offs`` -- the company figure less cash items the release
  itself names as one-off (litigation/settlement/insurance receipts and the
  like), each with its sentence.

Each column carries its yield on the same market cap.  Nothing is estimated;
a column that cannot be built says why.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

SCHEMA_VERSION = "valuation-fcf-bridge:batch-b:0.1"
_SCALE = {"billion": Decimal(1_000_000_000), "million": Decimal(1_000_000),
          "thousand": Decimal(1000), "one": Decimal(1)}
_FCF_LABEL = re.compile(r"(?i)^(?:adjusted\s+)?free cash flow\b[^|]*$")
_OCF_LABEL = re.compile(r"(?i)(operating cash flow|net cash (?:provided by|from) operating activities|"
                        r"cash (?:flow )?from operations)")
_AMOUNT = re.compile(r"\$?(?P<neg>\(|-|−)?\s?\$?\s?(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\)?")
_ONE_OFF = re.compile(
    r"(?i)(litigation|legal settlement|settlement proceeds|insurance (?:proceeds|recover)|"
    r"one-time|non-recurring|nonrecurring)")
_CASH_AMOUNT = re.compile(r"(?i)\$\s?(?P<num>\d+(?:\.\d+)?)\s?(?P<scale>billion|million|B|M)\b")


def _dec(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def _plain(value: Decimal | None, quantum: str | None = None) -> str | None:
    if value is None:
        return None
    if quantum:
        value = value.quantize(Decimal(quantum))
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _values(line: str) -> list[Decimal] | None:
    """``$(0.25) | $(0.11)`` -> [-0.25, -0.11]; None when the line is not values."""

    cells = [cell.strip() for cell in line.split("|") if cell.strip()]
    if not cells:
        return None
    out = []
    for cell in cells:
        found = _AMOUNT.fullmatch(cell.replace(" ", ""))
        if not found:
            return None
        value = Decimal(found.group("num").replace(",", ""))
        out.append(-value if found.group("neg") else value)
    return out


def company_fcf_tables(text: str, *, fye_month: int | None,
                       release_period: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every FCF reconciliation table in a release's plain text.

    ``[{period, period_kind, scale, components: [{label, value, prior}], fcf,
    fcf_prior, reconciles, quote}]``.  Values are in currency units.
    """

    from .earnings_release_outlook import _label, _period_end, _period_in

    lines = text.split("\n")
    tables = []
    for index, line in enumerate(lines):
        if not _FCF_LABEL.match(line.strip()) or index + 1 >= len(lines):
            continue
        fcf_values = _values(lines[index + 1])
        if not fcf_values:
            continue
        components: list[dict[str, Any]] = []
        cursor = index - 2
        start = None
        while cursor >= 0 and index - cursor <= 24:
            values = _values(lines[cursor + 1]) if cursor + 1 < index else None
            label = lines[cursor].strip()
            if values is None or _values(label) is not None:
                break
            components.insert(0, {"label": label, "value": values[0],
                                  "prior": values[1] if len(values) > 1 else None})
            if _OCF_LABEL.search(label):
                start = cursor
                break
            cursor -= 2
        if start is None or len(components) < 2:
            continue
        header = " ".join(lines[max(0, start - 6):start])
        scale = ("billion" if re.search(r"(?i)in billions|\$\s?bn|billions", header) else
                 "million" if re.search(r"(?i)in millions|\$\s?m\b|millions", header) else
                 "thousand" if re.search(r"(?i)in thousands", header) else None)
        if scale is None:
            continue
        period = _period_in(header)
        if period is None and release_period and re.search(r"(?i)quarter", header):
            period = dict(release_period)
        factor = _SCALE[scale]
        total = sum(item["value"] for item in components)
        tolerance = Decimal("0.02") if scale == "billion" else Decimal("2")
        fcf = fcf_values[0]
        quote = "\n".join(lines[start:index + 2])
        tables.append({
            "period": _label(period), "period_kind": None if period is None else period["kind"],
            "period_end": _period_end(period, fye_month), "scale": scale,
            "components": [{"label": item["label"], "value": _plain(item["value"] * factor),
                            "prior": None if item["prior"] is None else _plain(item["prior"] * factor)}
                           for item in components],
            "fcf": _plain(fcf * factor),
            "fcf_prior": _plain(fcf_values[1] * factor) if len(fcf_values) > 1 else None,
            "reconciles": abs(total - fcf) <= tolerance,
            "quote": quote[:800],
        })
    return tables


def one_off_items(text: str) -> list[dict[str, Any]]:
    """Cash items the release itself calls one-off, with their sentences."""

    out = []
    for sentence in re.split(r"(?<=[.;])\s+|\n", text):
        if not _ONE_OFF.search(sentence) or not re.search(r"(?i)cash|received|receipt|proceeds|paid", sentence):
            continue
        amount = _CASH_AMOUNT.search(sentence)
        if amount is None:
            continue
        scale = "billion" if amount.group("scale").lower() in {"billion", "b"} else "million"
        value = Decimal(amount.group("num")) * _SCALE[scale]
        inflow = bool(re.search(r"(?i)received|receipt|proceeds|recover|inflow", sentence))
        out.append({"value": _plain(value), "direction": "inflow" if inflow else "outflow",
                    "quote": sentence.strip()[:400],
                    "kind": _ONE_OFF.search(sentence).group(1).lower()})
    return out


def fcf_bridge(*, system: Mapping[str, Any] | None, releases: Sequence[Mapping[str, Any]],
               market_cap: Any, fye_month: int | None) -> dict[str, Any]:
    """The three columns.

    ``system``: ``{value, through, formula}`` (TTM CFO - PP&E) or None.
    ``releases``: ``[{"text", "release_period": {kind, fiscal_year, fiscal_quarter},
    "accession", "report_date"}]`` newest last.
    """

    cap = _dec(market_cap)

    def yield_of(value: Any) -> str | None:
        amount = _dec(value)
        return None if amount is None or not cap else _plain(amount / cap * 100, "0.01")

    columns: list[dict[str, Any]] = []
    columns.append({"basis": "system", "label": "系统口径（XBRL：经营现金流 − 购置固定资产，TTM）",
                    "value": (system or {}).get("value"), "through": (system or {}).get("through"),
                    "yield_percent": yield_of((system or {}).get("value")),
                    "reason": None if system and system.get("value") else "申报里凑不出四个季度的经营现金流和资本开支"})
    company: dict[str, Any] = {"basis": "company", "label": "公司口径（业绩稿 FCF 对账表，滚动一年）",
                               "value": None, "reason": "没有读到公司业绩稿里的 FCF 对账表"}
    adjustments: list[dict[str, Any]] = []
    if releases:
        latest = releases[-1]
        period = latest.get("release_period") or {}
        tables = company_fcf_tables(latest["text"], fye_month=fye_month, release_period=period)
        by_period = {table["period"]: table for table in tables if table["period"]}
        quarter = f"FY{period.get('fiscal_year')}Q{period.get('fiscal_quarter')}" if period else None
        year = f"FY{period.get('fiscal_year')}" if period else None
        source = {"accession": latest.get("accession"), "report_date": latest.get("report_date")}
        if period.get("fiscal_quarter") == 4 and year in by_period:
            table = by_period[year]
            company.update({"value": table["fcf"], "through": table["period_end"], "method": "fiscal_year_column",
                            "components": table["components"], "reconciles": table["reconciles"],
                            "quote": table["quote"], "source": source, "reason": None})
        elif period.get("fiscal_quarter") == 1 and quarter in by_period:
            prior_year = f"FY{int(period['fiscal_year']) - 1}"
            prior_table = None
            for older in reversed(releases[:-1]):
                older_tables = company_fcf_tables(older["text"], fye_month=fye_month,
                                                  release_period=older.get("release_period"))
                prior_table = next((t for t in older_tables if t["period"] == prior_year), None)
                if prior_table:
                    break
            q1 = by_period[quarter]
            if prior_table and q1.get("fcf_prior") is not None:
                value = _dec(prior_table["fcf"]) + _dec(q1["fcf"]) - _dec(q1["fcf_prior"])
                company.update({"value": _plain(value), "through": q1["period_end"],
                                "method": f"{prior_year} + {quarter} − 上年同季",
                                "components": q1["components"], "reconciles": q1["reconciles"],
                                "quote": q1["quote"], "source": source, "reason": None})
            else:
                company["reason"] = f"滚动一年需要 {prior_year} 全年和上年同季的公司口径 FCF，未都读到"
        elif tables:
            company["reason"] = "最新业绩稿是年中季度，滚动一年需要年初至今累计数，未读到"
        for item in one_off_items(latest["text"]):
            adjustments.append(item)
    company["yield_percent"] = yield_of(company.get("value"))
    columns.append(company)
    adjusted = {"basis": "company_ex_one_offs", "label": "公司口径扣除一次性现金项目",
                "value": None, "adjustments": adjustments}
    if company.get("value") is not None:
        value = _dec(company["value"])
        for item in adjustments:
            amount = _dec(item["value"]) or Decimal(0)
            value = value - amount if item["direction"] == "inflow" else value + amount
        adjusted["value"] = _plain(value)
        adjusted["note"] = (None if adjustments else "业绩稿没有点名一次性现金项目，与公司口径相同")
    else:
        adjusted["reason"] = company.get("reason")
    adjusted["yield_percent"] = yield_of(adjusted.get("value"))
    columns.append(adjusted)
    return {"schema_version": SCHEMA_VERSION, "market_cap": _plain(cap), "columns": columns}


__all__ = ["SCHEMA_VERSION", "company_fcf_tables", "fcf_bridge", "one_off_items"]
