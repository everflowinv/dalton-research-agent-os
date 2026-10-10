"""Batch B: forward multiples, enterprise value with total debt, and peers.

The stored valuation snapshot (P11c) carries trailing P/E, P/S and FCF yield
with their historical percentiles and stays the authority for those.  It
could not produce an enterprise value because its ``total_debt`` role accepts
only a single combined concept, which none of the covered filers report.  This
module adds, read-only and labelled:

* **Total debt from the XBRL statement lines**, as a declared composite:
  a combined concept when filed, else current debt + non-current debt
  (``DebtCurrent``/``LongTermDebtCurrent``/``ShortTermBorrowings`` plus
  ``LongTermDebtAndCapitalLeaseObligations``/``LongTermDebtNoncurrent``/...),
  each component with its accession.  Operating lease liabilities are not
  debt here and are named as excluded.
* **EV** = market cap + total debt - cash and equivalents; **EV/EBITDA** with
  EBITDA = trailing operating income + trailing D&A (a filer's extension D&A
  concept is admitted and labelled when no us-gaap one is filed); **EV/Sales**.
* **Forward P/E**: price / NTM EPS.  NTM is time-weighted FY1/FY2 when both
  are held (``w = days left in FY1 / 365``); FY1 alone is used, and said, when
  FY2 is not.  Three EPS sources, each its own row: the vendor consensus, the
  median of broker house estimates (street table), and *ours* -- the forecast
  model's EPS row read through the cockpit model payload, or ``unavailable``
  with the reason when the model has no EPS row.
* **Peers**: the same multiples for every company in the mission universe.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from .store import content_hash

SCHEMA_VERSION = "valuation-forward:batch-b:0.1"

COMBINED_DEBT = ("us-gaap:DebtLongtermAndShorttermCombinedAmount",
                 "us-gaap:DebtAndCapitalLeaseObligations",
                 "us-gaap:DebtAndFinanceLeaseObligations")
CURRENT_DEBT = ("us-gaap:DebtCurrent", "us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent",
                "us-gaap:LongTermDebtCurrent", "us-gaap:ShortTermBorrowings",
                "us-gaap:CommercialPaper")
NONCURRENT_DEBT = ("us-gaap:LongTermDebtAndCapitalLeaseObligations",
                   "us-gaap:LongTermDebtNoncurrent",
                   "us-gaap:LongTermDebtAndFinanceLeaseLiabilitiesNoncurrent",
                   "us-gaap:LongTermDebt")
CASH = ("us-gaap:CashAndCashEquivalentsAtCarryingValue",
        "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")
OPERATING_INCOME = (("income", "us-gaap:OperatingIncomeLoss"),)
REVENUE = (("income", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"),
           ("income", "us-gaap:Revenues"))
DA = (("cash", "us-gaap:DepreciationDepletionAndAmortization"),
      ("cash", "us-gaap:DepreciationAmortizationAndAccretionNet"),
      ("cash", "us-gaap:DepreciationAndAmortization"),
      ("income", "us-gaap:DepreciationAndAmortization"))
_EXTENSION_DA = re.compile(r"^[a-z0-9]+:Depreciation\w*Amortization\w*$")


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
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


# ---------------------------------------------------------------------------
# filed lines
# ---------------------------------------------------------------------------


def _lines(connection: sqlite3.Connection, company_ref: str, concept: str, statement: str,
           as_of: str | None) -> list[dict[str, Any]]:
    from .financial_period import _refs, _table_exists, query

    if not _table_exists(connection, "coverage_mission_statement_lines"):
        return []
    refs = _refs(company_ref)
    rows = query(connection,
                 "SELECT l.*, f.accession AS accession, f.filed AS filed, f.report_date AS report_date, "
                 "f.form AS filing_form FROM coverage_mission_statement_lines l JOIN "
                 "coverage_mission_statement_filings f ON f.ingest_id=l.ingest_id "
                 f"WHERE f.company_ref IN ({','.join('?' * len(refs))}) AND l.concept=? AND l.statement=? "
                 "ORDER BY f.filed, f.accession, l.ordinal", (*refs, concept, statement))
    out = []
    for row in rows:
        row["is_breakdown"] = bool(row.get("is_breakdown")) or row.get("dimension_axis") is not None
        if as_of and str(row.get("filed") or "") > str(as_of)[:10]:
            continue
        if row["is_breakdown"] or row.get("value") is None:
            continue
        out.append(row)
    return out


def _concepts(connection: sqlite3.Connection, company_ref: str, statement: str) -> list[str]:
    from .financial_period import _refs, _table_exists, query

    if not _table_exists(connection, "coverage_mission_statement_lines"):
        return []
    refs = _refs(company_ref)
    return [row["concept"] for row in query(
        connection, "SELECT DISTINCT l.concept AS concept FROM coverage_mission_statement_lines l JOIN "
        "coverage_mission_statement_filings f ON f.ingest_id=l.ingest_id "
        f"WHERE f.company_ref IN ({','.join('?' * len(refs))}) AND l.statement=?", (*refs, statement))]


def _instant(connection, company_ref, concepts, as_of, currency):
    """The newest balance among the candidate concepts (earlier candidate on a tie).

    Newest, not first: a filer that stopped using ``DebtCurrent`` two years
    ago would otherwise pair a stale current figure with today's long-term one.
    """

    from .valuation_snapshot_cli import latest_instant

    best = None
    for concept in concepts:
        rows = _lines(connection, company_ref, concept, "balance", as_of)
        if not rows:
            continue
        components, total, why = latest_instant(rows, currency=currency)
        if components and (best is None or components[0]["period_end"] > best["period_end"]):
            best = {"concept": concept, "value": total, "period_end": components[0]["period_end"],
                    "accession": components[0]["accession"]}
    return best


def _ttm(connection, company_ref, candidates, as_of, currency):
    from .valuation_snapshot_cli import trailing_year

    from .terminology import label

    def named(concept: str) -> str:
        # The registry's line name; the bare concept only when it has none.
        return "「" + label("statement_line", concept.split(":")[-1], default=concept) + "」"

    reasons = []
    for statement, concept in candidates:
        rows = _lines(connection, company_ref, concept, statement, as_of)
        if not rows:
            reasons.append(f"{named(concept)}：申报里没有合并口径数据")
            continue
        components, total, why = trailing_year(rows, currency=currency)
        if components:
            return {"concept": concept, "value": total, "through": components[-1]["period_end"],
                    "quarters": [c["period_end"] for c in components]}, None
        reasons.append(f"{named(concept)}：{why}")
    return None, "；".join(dict.fromkeys(reasons)) or "没有可用的申报科目"


def total_debt(connection: sqlite3.Connection, company_ref: str, *, as_of: str | None = None,
               currency: str = "USD") -> dict[str, Any]:
    """Total debt at the newest balance sheet filed by ``as_of``, with its components."""

    combined = _instant(connection, company_ref, COMBINED_DEBT, as_of, currency)
    if combined:
        return {"status": "available", "value": _plain(combined["value"]), "basis": "combined_concept",
                "components": [{**combined, "value": _plain(combined["value"])}],
                "period_end": combined["period_end"],
                "excluded": "operating lease liabilities"}
    current = _instant(connection, company_ref, CURRENT_DEBT, as_of, currency)
    noncurrent = _instant(connection, company_ref, NONCURRENT_DEBT, as_of, currency)
    if noncurrent is None:
        return {"status": "unavailable", "value": None,
                "reason": "申报里缺少「长期债务」，暂无法得出有息债务合计"}
    parts = [part for part in (current, noncurrent) if part]
    if current and current["period_end"] != noncurrent["period_end"]:
        return {"status": "unavailable", "value": None,
                "reason": f"流动与非流动债务不在同一资产负债表日（{current['period_end']} / {noncurrent['period_end']}）"}
    value = sum(part["value"] for part in parts)
    return {"status": "available", "value": _plain(value), "period_end": noncurrent["period_end"],
            "basis": "current_plus_noncurrent" if current else "noncurrent_only_no_current_line",
            "components": [{**part, "value": _plain(part["value"])} for part in parts],
            "excluded": "operating lease liabilities"}


def ebitda_ttm(connection: sqlite3.Connection, company_ref: str, *, as_of: str | None = None,
               currency: str = "USD") -> dict[str, Any]:
    operating, why_op = _ttm(connection, company_ref, OPERATING_INCOME, as_of, currency)
    candidates = list(DA) + [("cash", concept) for concept in _concepts(connection, company_ref, "cash")
                             if _EXTENSION_DA.match(concept) and not concept.startswith("us-gaap:")]
    da, why_da = _ttm(connection, company_ref, candidates, as_of, currency)
    if operating is None or da is None:
        return {"status": "unavailable", "value": None,
                "reason": "；".join(filter(None, [why_op and f"经营利润：{why_op}", why_da and f"折旧摊销：{why_da}"]))}
    note = None
    if not da["concept"].startswith("us-gaap:"):
        note = f"折旧摊销用的是公司自定义科目 {da['concept']}（可能含少量其他非现金项目）"
    return {"status": "available", "value": _plain(operating["value"] + da["value"]),
            "operating_income": _plain(operating["value"]), "depreciation_amortisation": _plain(da["value"]),
            "da_concept": da["concept"], "through": operating["through"], "note": note,
            "formula": "operating_income_ttm + depreciation_amortisation_ttm"}


# ---------------------------------------------------------------------------
# NTM EPS and forward P/E
# ---------------------------------------------------------------------------


def ntm(fy1: tuple[str, Any, str] | None, fy2: tuple[str, Any, str] | None, as_of: str) -> dict[str, Any]:
    """``fy*`` = (label, value, fiscal year end ISO).  Time-weighted NTM."""

    if fy1 is None or _dec(fy1[1]) is None:
        return {"status": "unavailable", "value": None, "reason": "当前财年没有 EPS 预测"}
    end = date.fromisoformat(fy1[2])
    weight = Decimal(max(0, min(365, (end - date.fromisoformat(str(as_of)[:10])).days))) / Decimal(365)
    if fy2 is not None and _dec(fy2[1]) is not None:
        value = weight * _dec(fy1[1]) + (1 - weight) * _dec(fy2[1])
        return {"status": "available", "value": _plain(value, "0.01"), "method": "time_weighted_fy1_fy2",
                "fy1": {"label": fy1[0], "value": fy1[1]}, "fy2": {"label": fy2[0], "value": fy2[1]},
                "fy1_weight": _plain(weight, "0.001")}
    return {"status": "available", "value": _plain(_dec(fy1[1]), "0.01"), "method": "fy1_only",
            "fy1": {"label": fy1[0], "value": fy1[1]}, "fy2": None, "fy1_weight": "1",
            "note": f"没有 {fy1[0]} 之后一年的 EPS，NTM 只用 {fy1[0]}（偏低估前瞻 EPS 的增长）"}


OCF = (("cash", "us-gaap:NetCashProvidedByUsedInOperatingActivities"),
       ("cash", "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"))
CAPEX = (("cash", "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"),
         ("cash", "us-gaap:PaymentsToAcquireProductiveAssets"))


def system_fcf(connection: sqlite3.Connection, company_ref: str, *, as_of: str | None = None,
               currency: str = "USD") -> dict[str, Any] | None:
    """The snapshot's definition: TTM operating cash flow - TTM PP&E purchases."""

    ocf, _ = _ttm(connection, company_ref, OCF, as_of, currency)
    capex, _ = _ttm(connection, company_ref, CAPEX, as_of, currency)
    if ocf is None or capex is None:
        return None
    return {"value": _plain(ocf["value"] - capex["value"]), "through": ocf["through"],
            "formula": "operating_cash_flow_ttm - capital_expenditure_ttm",
            "operating_cash_flow": _plain(ocf["value"]), "capital_expenditure": _plain(capex["value"])}


def _fy_end(label: str, fye_month: int) -> str:
    from .financial_period import month_end

    return month_end(int(label[2:6]), fye_month).isoformat()


def our_eps(reader_or_payload: Any, company_ref: str) -> dict[str, Any]:
    """Our EPS forecast: the model payload's EPS row (M owns it), or unavailable.

    Accepts a ``charts_api`` model payload, or a ``ChartReader`` to build one.
    """

    payload = reader_or_payload
    if not isinstance(payload, Mapping):
        try:
            from .cockpit_v2.charts_api import model_payload

            payload = model_payload(reader_or_payload, {"company": company_ref})
        except Exception as exc:  # noqa: BLE001 - a model that cannot be read is not ours
            return {"status": "unavailable", "reason": f"预测模型暂时读不出（{type(exc).__name__}）"}
    if payload.get("status") != "ok":
        return {"status": "unavailable", "reason": payload.get("empty_reason") or "还没有预测模型"}
    rows = payload.get("rows") or []
    def is_eps(row: Mapping[str, Any]) -> bool:
        text = f"{row.get('key')} {row.get('label')} {row.get('role')}"
        if re.search(r"(?i)numerator|分子", text):
            return False
        return (row.get("role") == "diluted_eps"
                or str(row.get("concept") or "").endswith("EarningsPerShareDiluted")
                or bool(re.search(r"(?i)(?:^|[^a-z])(?:diluted_)?eps(?:$|[^a-z])|per[_ ]share|每股收益", text)))

    eps_row = next((row for row in rows if is_eps(row)
                    and row.get("unit_kind") in (None, "per_share", "ratio", "other")), None)
    if eps_row is None:
        return {"status": "unavailable", "reason": "我们的预测模型还没有「稀释每股收益」行（模型只到利润表金额行）"}
    forecast_ends = set((payload.get("period") or {}).get("forecast") or ())
    cells = [cell for cell in eps_row.get("cells") or () if cell.get("end") in forecast_ends
             and _dec(cell.get("value")) is not None]
    if len(cells) < 4:
        return {"status": "unavailable", "reason": f"模型 EPS 行只有 {len(cells)} 个预测季度，凑不满未来四个季度"}
    nxt = sorted(cells, key=lambda c: c["end"])[:4]
    projection = payload.get("annual_projection")
    if isinstance(projection, Mapping):
        # The reader supplies the verified projection of this exact model;
        # a detached payload must retain its self-hash and model binding.
        if (projection.get("company_ref") != company_ref
                or projection.get("model_version_ref") != payload.get("version_ref")
                or projection.get("content_hash") != content_hash(
                    {k:v for k,v in projection.items() if k != "content_hash"})):
            return {"status": "unavailable", "reason": "NTM EPS年度投影与当前模型身份不一致"}
        period = next((p for p in projection.get("periods") or []
                       if p.get("quarter_ends") == [c["end"] for c in nxt]), None)
        if period is not None:
            annual = period.get("forecast_eps") or {}
            numerator, denominator = annual.get("numerator") or {}, annual.get("denominator") or {}
            if (annual.get("status") != "computed" or _dec(annual.get("value")) is None
                    or numerator.get("status") != "computed" or denominator.get("status") != "computed"):
                return {"status": "unavailable", "reason": annual.get("reason") or "NTM整财年窗口的正式年度EPS不可得"}
            return {"status":"available", "value":_plain(_dec(annual["value"]),"0.01"),
                    "method":"ntm_exact_fiscal_year_annual_projection", "quarters":[c["end"] for c in nxt],
                    "common_income":numerator["value"], "diluted_shares":denominator["value"],
                    "unit":annual["unit"], "row":eps_row.get("label"), "projection_ref":projection["projection_ref"],
                    "projection_hash":projection["content_hash"], "projection_label":period["label"],
                    "share_method":denominator.get("method"), "share_as_of":denominator.get("as_of"),
                    "share_growth_rate":denominator.get("growth_rate"), "share_steps":denominator.get("steps"),
                    "basis_label":"未来四季恰为完整公司财年，采用同模型的正式年度普通股利润／年度稀释股份"}
    numerators = [row for row in rows if row.get("role") in {"eps_numerator", "diluted_eps_numerator"} and row.get("unit_kind") == "amount"]
    share_rows = [row for row in rows if row.get("role") in {"diluted_shares", "diluted_weighted_average_shares"} and row.get("unit_kind") == "shares"]
    numerator = numerators[0] if len(numerators) == 1 else None
    shares = share_rows[0] if len(share_rows) == 1 else None
    if numerator is None or shares is None:
        return {"status": "unavailable", "reason": "NTM EPS缺少明确普通股利润分子或稀释加权股份；季度EPS不能相加"}
    ni = {cell["end"]: _dec(cell.get("value")) for cell in numerator.get("cells") or []}
    ds = {cell["end"]: _dec(cell.get("value")) for cell in shares.get("cells") or []}
    periods = {column["end"]: column for column in payload.get("columns") or []}
    days = [];previous = None
    for cell in nxt:
        end = cell["end"];period = periods.get(end) or {}
        if ni.get(end) is None or ds.get(end) is None or ds[end] <= 0 or not period.get("start"):
            return {"status": "unavailable", "reason": "NTM EPS的普通股利润、股份或季度起止日期不完整"}
        start = date.fromisoformat(period["start"]);finish = date.fromisoformat(end)
        if start > finish or (previous is not None and (start-previous).days != 1):
            return {"status": "unavailable", "reason": "NTM EPS四季期间不连续，不能形成完整12个月"}
        days.append(Decimal((finish-start).days+1));previous = finish
    weighted = sum(ds[cell["end"]]*length for cell,length in zip(nxt,days))/sum(days)
    total = sum(ni[cell["end"]] for cell in nxt)
    return {"status": "available", "value": _plain(total/weighted, "0.01"),
            "method": "ntm_common_income_over_day_weighted_diluted_shares", "quarters": [c["end"] for c in nxt],
            "common_income": _plain(total), "diluted_shares": _plain(weighted), "unit": "usd_per_share", "row": eps_row.get("label"),
            "basis_label":"跨财年未来四季普通股利润／按天加权稀释股份；季度EPS未直接相加"}


def forward_block(connection: sqlite3.Connection | None, company_ref: str, *, as_of: str,
                  consensus: Mapping[str, Any] | None, street: Mapping[str, Any] | None,
                  market: Mapping[str, Any] | None, ours: Mapping[str, Any] | None = None,
                  snapshot: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Valuation for one company: stored snapshot multiples + forward/EV additions."""

    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "company_ref": company_ref, "as_of": as_of}
    price = _dec(((market or {}).get("price") or {}).get("close"))
    cap = _dec(((market or {}).get("market_cap") or {}).get("value"))
    currency = (market or {}).get("currency") or "USD"
    out["price"], out["market_cap"] = _plain(price), _plain(cap)
    calendar = (consensus or {}).get("calendar") or {}
    fye_month = int(str(calendar.get("fiscal_year_end") or "12-31")[:2])
    periods = (consensus or {}).get("periods") or []
    rows = []

    def fy(source: Sequence[Mapping[str, Any]], index: int, getter) -> tuple[str, Any, str] | None:
        if index >= len(source):
            return None
        label = source[index]["label"]
        value = getter(source[index])
        return None if value is None else (label, value, _fy_end(label, fye_month))

    # NTM = the next four fiscal quarters after the last reported one
    # (``financial_period.ntm_aligned``); time-weighting by calendar days is
    # the fallback only when the calendar is unknown.
    from .financial_period import company_calendar, ntm_aligned

    last = calendar.get("last_reported_period_end")
    if not last and connection is not None:
        corrected = company_calendar(connection, company_ref, as_of)
        if corrected:
            last, fye_month = corrected["last_reported_period_end"], int(corrected["fiscal_year_end_month"])
    vendor = ((consensus or {}).get("ntm") or {}).get("eps")
    if vendor is None:
        get_vendor = lambda item: ((item.get("eps") or {}).get("mean"))  # noqa: E731
        vendor = ntm(fy(periods, 0, get_vendor), fy(periods, 1, get_vendor), as_of)
    rows.append({"source": "consensus_vendor", "label": "一致预期（数据商，调整后口径）", **vendor})
    street_rows = [item for item in (street or {}).get("periods") or ()
                   if item.get("metric") == "eps" and item.get("median")]
    if last:
        broker = ntm_aligned(last, fye_month,
                             quarterly={i["period"]: i["median"] for i in street_rows if i.get("period_kind") == "quarter"},
                             annual={i["period"]: i["median"] for i in street_rows if i.get("period_kind") == "fiscal_year"})
        if broker.get("status") == "available":
            counts = {i["period"]: i.get("count") for i in street_rows}
            broker["houses"] = [counts.get(c["part"]) for c in broker["components"]]
    else:
        broker = {"status": "unavailable", "reason": "不知道最近已报告的财期，无法对齐未来四个财季"}
    rows.append({"source": "street_median", "label": "券商预测中位数（研报表格，多为调整后口径）", **broker})
    rows.append({"source": "ours", "label": "我们的模型", **(ours or {"status": "unavailable",
                                                                  "reason": "没有读取我们的模型"})})
    for row in rows:
        eps = _dec(row.get("value"))
        if row.get("status") == "available" and price and eps and eps > 0:
            row["forward_pe"] = _plain(price / eps, "0.1")
        else:
            row["forward_pe"] = None
            if row.get("status") == "available" and eps is not None and eps <= 0:
                row["forward_pe_reason"] = "NTM EPS 不为正，市盈率没有意义"
    out["forward_pe"] = rows
    if connection is not None:
        debt = total_debt(connection, company_ref, as_of=as_of, currency=currency)
        cash = _instant(connection, company_ref, CASH, as_of, currency)
        ebitda = ebitda_ttm(connection, company_ref, as_of=as_of, currency=currency)
        revenue, _ = _ttm(connection, company_ref, REVENUE, as_of, currency)
    else:
        debt = {"status": "unavailable", "reason": "没有可读的申报数据，暂无法得出有息债务合计"}
        cash, ebitda, revenue = None, {"status": "unavailable"}, None
    out["total_debt"] = debt
    out["cash"] = None if cash is None else {"value": _plain(cash["value"]), "concept": cash["concept"],
                                             "period_end": cash["period_end"], "accession": cash["accession"]}
    ev = None
    if cap is not None and debt.get("status") == "available" and cash is not None:
        ev = cap + _dec(debt["value"]) - cash["value"]
        out["enterprise_value"] = {"value": _plain(ev, "1"),
                                   "formula": "market_cap + total_debt - cash_and_equivalents",
                                   "balance_sheet_date": debt.get("period_end")}
    else:
        out["enterprise_value"] = {"value": None, "reason": debt.get("reason") or "缺市值或现金"}
    out["ebitda_ttm"] = ebitda
    if ev is not None and ebitda.get("status") == "available" and _dec(ebitda["value"]) > 0:
        out["ev_to_ebitda"] = _plain(ev / _dec(ebitda["value"]), "0.1")
    else:
        out["ev_to_ebitda"] = None
    out["ev_to_sales"] = (_plain(ev / revenue["value"], "0.01")
                          if ev is not None and revenue and revenue["value"] > 0 else None)
    out["snapshot"] = snapshot
    return out


def _reading(metric: Mapping[str, Any], basis: Mapping[str, Any]) -> dict[str, Any] | None:
    from .valuation_history import percentile_reading

    percentile = metric.get("percentile") or {}
    if percentile.get("value") is None:
        return None
    return percentile_reading(percentile.get("value"), sample_size=percentile.get("sample_size"),
                              windows=basis.get("fundamental_window_count"))


def latest_snapshot_view(connection: sqlite3.Connection | None, company_ref: str) -> dict[str, Any] | None:
    """The stored P11c snapshot's metrics with percentiles (kept as the authority)."""

    from .financial_period import _refs, _table_exists, query

    if connection is None or not _table_exists(connection, "valuation_snapshot_versions"):
        return None
    refs = _refs(company_ref)
    rows = query(connection,
                 f"SELECT record_json FROM valuation_snapshot_versions WHERE company_ref IN "
                 f"({','.join('?' * len(refs))}) ORDER BY version_number DESC LIMIT 1", refs)
    if not rows:
        return None
    record = json.loads(rows[0]["record_json"])
    basis = record.get("basis") or {}
    return {"as_of": record.get("as_of"), "version": record.get("version"),
            "fundamental_windows": basis.get("fundamental_window_count"),
            "shares_basis": basis.get("shares_basis"),
            "share_observations": basis.get("share_observation_count"),
            "enterprise_value": basis.get("enterprise_value"),
            "metrics": [{"metric": m.get("metric"), "status": m.get("status"), "value": m.get("value"),
                         "reading": _reading(m, basis),
                         "percentile": (m.get("percentile") or {}).get("value"),
                         "percentile_basis": (m.get("percentile") or {}).get("basis"),
                         "sample_size": (m.get("percentile") or {}).get("sample_size"),
                         "earnings_basis": m.get("earnings_basis"), "core_method": m.get("core_method"),
                         "tax_basis": m.get("tax_basis"), "limitations": m.get("limitations"),
                         "sample_window": m.get("sample_window"),
                         "reason": m.get("reason")} for m in record.get("metrics") or ()]}


def peers_table(blocks: Mapping[str, Mapping[str, Any]], tickers: Mapping[str, str]) -> dict[str, Any]:
    """One row per covered company from already-built forward blocks, plus medians."""

    rows = []
    for ref, block in blocks.items():
        snap = {m["metric"]: m for m in ((block.get("snapshot") or {}).get("metrics") or ())}
        fwd = next((row for row in block.get("forward_pe") or () if row["source"] == "consensus_vendor"), {})
        street = next((row for row in block.get("forward_pe") or () if row["source"] == "street_median"), {})
        rows.append({
            "company_ref": ref, "ticker": tickers.get(ref), "price": block.get("price"),
            "market_cap": block.get("market_cap"),
            "trailing_pe": (snap.get("trailing_pe") or {}).get("value"),
            "core_pe": (snap.get("core_pe") or {}).get("value"),
            "core_pe_basis": (snap.get("core_pe") or {}).get("earnings_basis"),
            "trailing_pe_percentile": (snap.get("trailing_pe") or {}).get("percentile"),
            "forward_pe_consensus": fwd.get("forward_pe"), "forward_pe_street": street.get("forward_pe"),
            "ev_to_ebitda": block.get("ev_to_ebitda"), "ev_to_sales": block.get("ev_to_sales"),
            "price_to_sales": (snap.get("price_to_sales") or {}).get("value"),
            "fcf_yield": (snap.get("fcf_yield") or {}).get("value"),
        })
    medians = {}
    for key in ("trailing_pe", "core_pe", "forward_pe_consensus", "forward_pe_street", "ev_to_ebitda",
                "ev_to_sales", "price_to_sales", "fcf_yield"):
        values = [_dec(row[key]) for row in rows if _dec(row.get(key)) is not None]
        medians[key] = _plain(Decimal(statistics.median(values)), "0.01") if values else None
    return {"rows": rows, "median": medians,
            "note": "价格 = 最新收盘；GAAP与核心营业税后PE分别标口径，核心口径不含融资成本；预期市盈率 = 价格 / NTM EPS；EV 含申报总债务、扣现金"}


__all__ = ["SCHEMA_VERSION", "system_fcf", "ebitda_ttm", "forward_block", "latest_snapshot_view", "ntm",
           "our_eps", "peers_table", "total_debt"]
