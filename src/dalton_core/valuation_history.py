"""Batch B (owner review §P1): long valuation history and honest percentile words.

Two problems with the stored percentiles: they rest on 2-4 usable fundamental
windows and about a year of effective sample, and they were read as "历史低位"
regardless.  This module adds, read-only:

* **EPS history from XBRL company-facts** -- the SEC ``companyfacts`` JSON the
  company-facts lane already spooled (``connector_source_envelopes`` of the SEC
  profile, re-hashed against the spool).  ``us-gaap:EarningsPerShareDiluted``
  per period, latest filing wins; a fourth quarter the filer never states is
  ``FY - 9M`` and labelled derived.  Trailing-twelve-month EPS points are
  dated by the day their last component was *filed*, so history never uses a
  figure before the market had it.
* **P/E history** over every stored bar that has a TTM EPS known that day,
  with the sample size, the years covered and the number of distinct EPS
  windows used.
* **``percentile_reading``** -- the only words a percentile may be described
  with: below 3 years or 2 windows of sample it is 「样本不足」, never
  「历史低位/高位」.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "valuation-history:batch-b:0.1"
MIN_YEARS = Decimal(3)
MIN_WINDOWS = 2
LOW_PERCENTILE = Decimal(20)
HIGH_PERCENTILE = Decimal(80)
_SEC_PROFILE = "connector-profile-template:sec%"
_CIK_RE = re.compile(rb'"cik"\s*:\s*(\d+)')


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


def percentile_reading(percentile: Any, *, sample_size: Any, windows: Any,
                       years: Any = None) -> dict[str, Any]:
    """The words a percentile may be shown with, and whether the sample allows them."""

    pct, sample = _dec(percentile), int(sample_size or 0)
    span = _dec(years) if years is not None else Decimal(sample) / Decimal(252)
    count = int(windows or 0)
    detail = f"{sample} 个样本，约 {_plain(span, '0.1')} 年，{count} 个财务窗口"
    if pct is None:
        return {"label": "无分位", "sufficient": False, "detail": detail}
    if span < MIN_YEARS or count < MIN_WINDOWS:
        return {"label": "样本不足", "sufficient": False, "detail": detail,
                "rule": f"需要至少 {MIN_YEARS} 年、{MIN_WINDOWS} 个财务窗口才描述高低位"}
    label = ("历史低位" if pct <= LOW_PERCENTILE else "历史高位" if pct >= HIGH_PERCENTILE
             else "历史中间区间")
    return {"label": label, "sufficient": True, "detail": detail}


# ---------------------------------------------------------------------------
# company facts
# ---------------------------------------------------------------------------


def _cik(company_ref: str) -> int | None:
    found = re.search(r"sec-cik:0*(\d+)$", str(company_ref))
    return int(found.group(1)) if found else None


def company_facts(connection: sqlite3.Connection, state_dir: Path | str,
                  company_ref: str) -> dict[str, Any] | None:
    """The newest spooled ``companyfacts`` document for this company, or None."""

    from .acquired_sec_filings import _spool_bytes
    from .financial_period import _table_exists, query

    cik = _cik(company_ref)
    if cik is None and _table_exists(connection, "coverage_mission_statement_filings"):
        columns = {row[1] for row in connection.execute("PRAGMA table_info(coverage_mission_statement_filings)")}
        if "cik" in columns:
            row = connection.execute("SELECT cik FROM coverage_mission_statement_filings WHERE company_ref=? "
                                     "ORDER BY filed DESC LIMIT 1", (company_ref,)).fetchone()
            cik = int(row[0]) if row else None
    if cik is None or not _table_exists(connection, "connector_source_envelopes"):
        return None
    if all(_table_exists(connection, table) for table in
           ("connector_invocations", "connector_call_specs")):
        rows = query(connection,
                     "SELECT e.raw_response_hash, e.created_at FROM connector_source_envelopes e "
                     "JOIN connector_invocations i ON i.connector_invocation_id=e.connector_invocation_ref "
                     "JOIN connector_call_specs s ON s.call_spec_id=i.call_spec_ref "
                     "WHERE e.connector_profile_ref LIKE ? AND s.operation='get_company_facts' "
                     "ORDER BY e.created_at DESC", (_SEC_PROFILE,))
    else:
        rows = query(connection,
                     "SELECT raw_response_hash, created_at FROM connector_source_envelopes "
                     "WHERE connector_profile_ref LIKE ? ORDER BY created_at DESC", (_SEC_PROFILE,))
    seen: set[str] = set()
    for row in rows:
        digest = row["raw_response_hash"]
        if not digest or digest in seen:
            continue
        seen.add(digest)
        raw, _reason = _spool_bytes(Path(state_dir), digest)
        if raw is None:
            continue
        if raw[:2] == b"\x1f\x8b":
            import gzip

            try:
                raw = gzip.decompress(raw)
            except OSError:
                continue
        head = _CIK_RE.search(raw[:200])
        if head is None or int(head.group(1)) != cik:
            continue
        try:
            document = json.loads(raw)
        except ValueError:
            continue
        if isinstance(document, dict) and isinstance(document.get("facts"), dict):
            return {"facts": document["facts"], "entity": document.get("entityName"),
                    "raw_hash": digest, "fetched_at": row["created_at"]}
    return None


def _duration(start: str, end: str) -> int | None:
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except (TypeError, ValueError):
        return None


def eps_points(facts: Mapping[str, Any], concept: str = "EarningsPerShareDiluted") -> dict[str, Any]:
    """Exact disclosure vintages. Later restatements retain their later dates."""
    gaap = facts.get("us-gaap") or {}
    def entries(names, unit):
        selected = {}
        for name in names:
            raw = ((gaap.get(name) or {}).get("units") or {}).get(unit) or []
            for item in raw:
                if not (item.get("start") and item.get("end") and item.get("filed")) or _dec(item.get("val")) is None:
                    continue
                key = (item["start"], item["end"], item["filed"], item.get("accn"))
                selected.setdefault(key, {"start": item["start"], "end": item["end"], "value": _dec(item["val"]),
                                         "filed": item["filed"], "accn": item.get("accn"), "concept": name})
        return list(selected.values())
    eps = entries((concept, "EarningsPerShareBasicAndDiluted") if concept == "EarningsPerShareDiluted" else (concept,), "USD/shares")
    common = entries(("NetIncomeLossAvailableToCommonStockholdersDiluted",
                      "NetIncomeLossAvailableToCommonStockholdersBasic"), "USD")
    shares = entries(("WeightedAverageNumberOfDilutedSharesOutstanding",
                      "WeightedAverageNumberOfShareOutstandingBasicAndDiluted"), "shares")
    return {"eps_vintages": eps, "common_income_vintages": common, "share_vintages": shares,
            "quarters": {item["end"]: item for item in eps if 80 <= (_duration(item["start"], item["end"]) or 0) <= 100},
            "years": {item["end"]: item for item in eps if 350 <= (_duration(item["start"], item["end"]) or 0) <= 380}}


def ttm_eps(points: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Filed annual EPS; interim TTM common income/day-weighted diluted shares."""
    eps = points.get("eps_vintages") or list((points.get("years") or {}).values())
    incomes = points.get("common_income_vintages") or []
    shares = points.get("share_vintages") or []
    out = [{"period_end": item["end"], "value": item["value"], "effective": item["filed"],
            "basis": "filed_annual_eps", "source_accessions": [item.get("accn")], "earnings_basis": "gaap"}
           for item in eps if 350 <= (_duration(item["start"], item["end"]) or 0) <= 380]
    for point in out:
        candidates = [row for row in shares if row["end"] == point["period_end"]
                      and 350 <= (_duration(row["start"], row["end"]) or 0) <= 380
                      and row["filed"] <= point["effective"]]
        share = max(candidates, key=lambda row: row["filed"]) if candidates else None
        point["diluted_shares"] = None if share is None else {"value": _plain(share["value"]),
            "basis": "filed_annual_weighted", "source_accessions": [share.get("accn")]}
    dates = sorted({item["filed"] for item in [*incomes, *shares]})
    for available in dates:
        def live(rows):
            selected = {}
            for item in sorted(rows, key=lambda row: (row["filed"], row.get("accn") or "")):
                if item["filed"] <= available:
                    selected[(item["start"], item["end"])] = item
            return selected
        ni, ds = live(incomes), live(shares)
        for key, current in ni.items():
            days = _duration(*key) or 0
            if not 80 <= days <= 290:
                continue
            prior_year = next((item for (start, end), item in ni.items()
                               if 350 <= (_duration(start, end) or 0) <= 380
                               and 0 <= (date.fromisoformat(key[0]) - date.fromisoformat(end)).days <= 8), None)
            prior_ytd = next((item for (start, end), item in ni.items()
                              if abs((date.fromisoformat(start) - date.fromisoformat(key[0])).days - 365) <= 7
                              and abs((date.fromisoformat(end) - date.fromisoformat(key[1])).days - 365) <= 7), None)
            if prior_year is None or prior_ytd is None:
                continue
            components = [prior_year, current, prior_ytd]
            keys = [(row["start"], row["end"]) for row in components]
            if any(k not in ds for k in keys):
                continue
            lengths = [Decimal((_duration(*k) or 0) + 1) for k in keys]
            span = lengths[0] + lengths[1] - lengths[2]
            weighted = (ds[keys[0]]["value"] * lengths[0] + ds[keys[1]]["value"] * lengths[1]
                        - ds[keys[2]]["value"] * lengths[2]) / span
            if weighted <= 0:
                continue
            number = components[0]["value"] + components[1]["value"] - components[2]["value"]
            out.append({"period_end": key[1], "value": number / weighted, "effective": available,
                        "basis": "ttm_common_income_over_day_weighted_shares", "earnings_basis": "gaap",
                        "diluted_shares": {"value": _plain(weighted), "basis": "ttm_day_weighted"},
                        "common_income": {"value": _plain(number)},
                        "source_accessions": [row.get("accn") for row in [*components, *(ds[k] for k in keys)]]})
    return sorted(out, key=lambda row: (row["period_end"], row["effective"]))


def pe_history(bars: Sequence[Mapping[str, Any]], ttm: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """P/E on each bar with a positive TTM EPS known that day; percentile of the last."""

    known = sorted(ttm, key=lambda item: (item["effective"], item["period_end"]))
    values: list[tuple[str, Decimal, str]] = []
    for bar in bars:
        day = str(bar["date"])
        close = _dec(bar.get("close"))
        live = [item for item in known if item["effective"] <= day]
        if not live or close is None:
            continue
        point = max(live, key=lambda item: (item["period_end"], item["effective"]))
        if point["value"] <= 0:
            continue
        values.append((day, close / point["value"], point["period_end"]))
    if not values:
        return {"status": "unavailable", "reason": "过去 12 个月「稀释每股收益」与价格序列没有重叠的日期"}
    current = values[-1][1]
    below = sum(1 for _, value, _ in values if value <= current)
    years = Decimal((date.fromisoformat(values[-1][0]) - date.fromisoformat(values[0][0])).days) / Decimal("365.25")
    windows = len({end for _, _, end in values})
    percentile = Decimal(below) / Decimal(len(values)) * 100
    return {
        "status": "available", "current": _plain(current, "0.01"),
        "percentile": _plain(percentile, "0.1"), "sample_size": len(values),
        "years": _plain(years, "0.1"), "windows": windows,
        "first_date": values[0][0], "last_date": values[-1][0],
        "median": _plain(sorted(v for _, v, _ in values)[len(values) // 2], "0.01"),
        "earnings_basis": "gaap",
        "basis": "close / filed annual EPS or TTM common income/day-weighted diluted shares; exact disclosure vintage",
        "reading": percentile_reading(percentile, sample_size=len(values), windows=windows, years=years),
    }


def long_pe(connection: sqlite3.Connection, state_dir: Path | str, company_ref: str, *,
            bars: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    facts = company_facts(connection, state_dir, company_ref)
    if facts is None:
        return {"status": "unavailable", "reason": "还没有获取这家公司的申报财务数据（XBRL）"}
    points = eps_points(facts["facts"])
    series = ttm_eps(points)
    out = pe_history(bars, series)
    out["eps_source"] = {"raw_hash": facts["raw_hash"], "entity": facts["entity"],
                         "fetched_at": facts["fetched_at"], "ttm_points": len(series),
                         "first_period": series[0]["period_end"] if series else None}
    return out


__all__ = ["SCHEMA_VERSION", "company_facts", "eps_points", "long_pe", "pe_history",
           "percentile_reading", "ttm_eps"]
