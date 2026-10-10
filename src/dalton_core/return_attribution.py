"""ADR-0014: holding-period return attribution and buy-side expectation sources.

Vision-gap 1.1 #2 (2026-09-30 report).  Four derived_deterministic,
append-only objects, computed by the same code in every environment with no
model call:

``IncomeBridgeVersion``
    One company, one fiscal period *as filed*: operating income -> pre-tax
    income -> net income -> net income to common.  Every line is a statement
    line ref (``statement-line:<line_id>``, with concept, accession, period and
    the filed value) carrying a sign and a closed classification (interest,
    other non-operating, equity method, tax, discontinued operations,
    minority, preferred).  Each step reconciles to the filed anchor within a
    tolerance that is written into the record, or records the residual.  A
    missing anchor is named, never inferred: IBM and DXC file no operating
    income line, and their bridges say so.

``PerShareSeriesVersion``
    Basic / diluted EPS and weighted share counts per filed period, each with
    explicit period semantics (``quarterly`` / ``year_to_date`` /
    ``annual``).  A fiscal fourth quarter is *not stated* by any filer this
    system covers, and it is never derived by subtracting cumulative values:
    the series lists it under ``quarters_not_stated``.  Trailing-twelve-month
    points use one stated rule, recorded with its components on every point
    (``annual`` or ``annual_plus_ytd_minus_prior_ytd``).  Missing rows (the
    DXC diluted share count) are flagged per period.

``HoldingPeriodReturnBridge``
    For one company: the realised trailing-year total return and the
    expected forward-year return, each decomposed into earnings growth,
    share count (the per-share effect of buybacks), multiple change and
    dividends, with refs to every input (per-share series, valuation snapshot,
    price series, driver model, consensus).  Expected legs carry the
    :data:`EXPECTATION_SOURCE_KINDS` of the expectation they rest on.

``ExpectationSourceTag``
    The typed source of an expectation-bearing input: a consensus version's
    fields, one debate's market position, a conviction call's market view.
    Closed vocabulary, deterministic classifier, written once per subject.

Who writes: the automation principal of the mission, under the ``valuation``
scope (the same grant as the valuation snapshot these objects extend).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from typing import Any

from .store import canonical_json, content_hash

SCHEMA_VERSION = "return-attribution-0.2"
BRIDGE_FORMULA_REF = "income-bridge:0.1"
PER_SHARE_FORMULA_REF = "per-share-series:0.2"
RETURN_FORMULA_REF = "holding-period-return:0.2"
CLASSIFIER_REF = "expectation-source-classifier:0.1"
WRITE_SCOPE = "valuation"
KIND = "derived_deterministic"
_SCHEMA_PATH = Path(__file__).with_name("return_attribution_schema.sql")
TABLES = ("income_bridge_versions", "per_share_series_versions",
          "holding_period_return_bridges", "expectation_source_tags")

PERIOD_SEMANTICS = ("quarterly", "year_to_date", "annual")
BRIDGE_STATUSES = ("complete", "residual", "missing_rows")
STEP_STATUSES = ("complete", "residual", "missing_anchor")

#: The buy side's expectation sources.  Closed: an input whose source is not
#: one of these is listed as ``unclassified``, never forced into one.
EXPECTATION_SOURCE_KINDS = (
    "consensus", "management_guidance", "sell_side_note", "owner_view", "market_implied",
)
EXPECTATION_SOURCE_LABELS = {
    "consensus": "市场一致预期", "management_guidance": "管理层指引",
    "sell_side_note": "卖方研报", "owner_view": "你的判断", "market_implied": "市场价格隐含",
}

# ---------------------------------------------------------------------------
# the closed concept tables
# ---------------------------------------------------------------------------

LINE_CLASSES = (
    "revenue", "operating_income", "interest_expense", "interest_income",
    "other_non_operating", "equity_method", "pretax_income", "tax",
    "discontinued_operations", "minority_interest", "net_income", "preferred",
    "net_income_to_common",
)
LINE_CLASS_LABELS = {
    "revenue": "营业收入", "operating_income": "营业利润", "interest_expense": "利息费用",
    "interest_income": "利息及投资收益", "other_non_operating": "其他营业外损益",
    "equity_method": "权益法投资损益", "pretax_income": "税前利润", "tax": "所得税",
    "discontinued_operations": "终止经营损益", "minority_interest": "少数股东损益",
    "net_income": "净利润（归属母公司）", "preferred": "优先股股息及调整",
    "net_income_to_common": "归属普通股股东净利润",
}

ANCHOR_CONCEPTS: Mapping[str, tuple[str, ...]] = {
    "revenue": ("us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                "us-gaap:SalesRevenueNet"),
    "operating_income": ("us-gaap:OperatingIncomeLoss",),
    "pretax_income": (
        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ),
    "net_income": ("us-gaap:NetIncomeLoss",),
    "net_income_to_common": ("us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic",
                             "us-gaap:NetIncomeLossAvailableToCommonStockholdersDiluted"),
}
#: concept -> (classification, sign applied to the filed value).
ITEM_CONCEPTS: Mapping[str, tuple[str, int]] = {
    "us-gaap:InterestExpense": ("interest_expense", -1),
    "us-gaap:InterestExpenseNonoperating": ("interest_expense", -1),
    "us-gaap:InterestExpenseDebt": ("interest_expense", -1),
    "us-gaap:InterestAndDebtExpense": ("interest_expense", -1),
    "us-gaap:InvestmentIncomeInterest": ("interest_income", 1),
    "us-gaap:InvestmentIncomeInterestAndDividend": ("interest_income", 1),
    "us-gaap:InterestIncomeOther": ("interest_income", 1),
    "us-gaap:InvestmentIncomeNonoperating": ("interest_income", 1),
    "us-gaap:InterestAndOtherIncome": ("interest_income", 1),
    "us-gaap:OtherNonoperatingIncomeExpense": ("other_non_operating", 1),
    "us-gaap:NonoperatingIncomeExpense": ("other_non_operating", 1),
    "us-gaap:NonoperatingGainsLosses": ("other_non_operating", 1),
    "us-gaap:OtherNonoperatingIncome": ("other_non_operating", 1),
    "us-gaap:OtherNonoperatingExpense": ("other_non_operating", -1),
    "us-gaap:ForeignCurrencyTransactionGainLossBeforeTax": ("other_non_operating", 1),
    "us-gaap:GainLossOnSaleOfPropertyPlantEquipment": ("other_non_operating", 1),
    "us-gaap:GainLossOnSaleOfBusiness": ("other_non_operating", 1),
    "us-gaap:GainLossOnInvestments": ("other_non_operating", 1),
    "us-gaap:IncomeLossFromEquityMethodInvestments": ("equity_method", 1),
    "us-gaap:IncomeTaxExpenseBenefit": ("tax", -1),
    "us-gaap:IncomeLossFromDiscontinuedOperationsNetOfTax": ("discontinued_operations", 1),
    "us-gaap:IncomeLossFromDiscontinuedOperationsNetOfTaxAttributableToReportingEntity":
        ("discontinued_operations", 1),
    "us-gaap:NetIncomeLossAttributableToNoncontrollingInterest": ("minority_interest", -1),
    "us-gaap:MinorityInterestInNetIncomeLossOfConsolidatedEntities": ("minority_interest", -1),
    "us-gaap:NetIncomeLossAttributableToRedeemableNoncontrollingInterest": ("minority_interest", -1),
    "us-gaap:NetIncomeLossAttributableToNonredeemableNoncontrollingInterest": ("minority_interest", -1),
    "us-gaap:NoncontrollingInterestInNetIncomeLossOtherNoncontrollingInterestsNonredeemable":
        ("minority_interest", -1),
    "us-gaap:NoncontrollingInterestInNetIncomeLossOtherNoncontrollingInterestsRedeemable":
        ("minority_interest", -1),
    "us-gaap:PreferredStockDividendsIncomeStatementImpact": ("preferred", -1),
    "us-gaap:PreferredStockDividendsAndOtherAdjustments": ("preferred", -1),
}
NON_OPERATING_CLASSES = frozenset({"interest_expense", "interest_income", "other_non_operating",
                                   "equity_method"})
#: Concepts that are operating totals, never a non-operating item, even when
#: a filer hangs them directly under pre-tax income.
OPERATING_TOTALS = frozenset({
    "us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:GrossProfit", "us-gaap:CostsAndExpenses", "us-gaap:OperatingExpenses",
    "us-gaap:CostOfRevenue", "us-gaap:OperatingIncomeLoss",
})

PER_SHARE_CONCEPTS: Mapping[str, tuple[str, ...]] = {
    "basic_eps": ("us-gaap:EarningsPerShareBasic", "us-gaap:EarningsPerShareBasicAndDiluted"),
    "diluted_eps": ("us-gaap:EarningsPerShareDiluted", "us-gaap:EarningsPerShareBasicAndDiluted"),
    "basic_shares": ("us-gaap:WeightedAverageNumberOfSharesOutstandingBasic",
                     "us-gaap:WeightedAverageNumberOfShareOutstandingBasicAndDiluted"),
    "diluted_shares": ("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
                       "us-gaap:WeightedAverageNumberOfShareOutstandingBasicAndDiluted"),
    "dps": ("us-gaap:CommonStockDividendsPerShareCashPaid",
            "us-gaap:CommonStockDividendsPerShareDeclared"),
}
REQUIRED_PER_SHARE = ("basic_eps", "diluted_eps", "basic_shares", "diluted_shares")
PER_SHARE_LABELS = {"basic_eps": "基本每股收益", "diluted_eps": "稀释每股收益",
                    "basic_shares": "基本加权股数", "diluted_shares": "稀释加权股数",
                    "dps": "每股股息"}
DIVIDEND_CASH_CONCEPTS = ("us-gaap:PaymentsOfDividendsCommonStock", "us-gaap:PaymentsOfDividends",
                          "us-gaap:PaymentsOfOrdinaryDividends")
REPURCHASE_CASH_CONCEPTS = ("us-gaap:PaymentsForRepurchaseOfCommonStock",)

TTM_RULE_ANNUAL = "annual"
TTM_RULE_YTD = "annual_plus_ytd_minus_prior_ytd"
TTM_RULE_TEXT = {
    TTM_RULE_ANNUAL: "该期即完整财年，直接取年报数",
    TTM_RULE_YTD: "上一完整财年 + 本年累计 − 上年同期累计（三个都是申报数，不推算单季）",
}

#: Reconciliation tolerance: the larger of this share of the filed anchor and
#: half a rounding quantum per term (a filer that reports in millions cannot
#: reconcile closer than its own rounding).
RELATIVE_TOLERANCE = Decimal("0.0005")
#: A decomposition component moving this many percentage points is material
#: for the weekly brief.
MATERIAL_CHANGE_PP = Decimal("5")
REALISED_WINDOW_DAYS = 365
FORWARD_QUARTERS = 4

LEG_KINDS = ("realised_trailing_year", "expected_driver_model", "expected_consensus",
             "expected_sell_side")
LEG_LABELS = {
    "realised_trailing_year": "已实现（过去 12 个月）",
    "expected_driver_model": "预期：我们的驱动模型（未来 4 个季度）",
    # 2026-10-09 (batch B finding): by construction these two legs are the
    # target price over today's price plus the dividend yield; EPS growth only
    # splits that number, the multiple change is its residual.  Named for what
    # they are, on one 12-month window.
    "expected_consensus": "预期：一致预期目标价隐含回报（12 个月）",
    "expected_sell_side": "预期：卖方目标价隐含回报（12 个月）",
}
TARGET_WINDOW_DAYS = 365
COMPONENTS = ("earnings_growth", "share_count", "multiple_change", "dividends")
COMPONENT_LABELS = {"earnings_growth": "盈利增长", "share_count": "股数变化（回购的每股效应）",
                    "multiple_change": "估值倍数变化", "dividends": "股息"}
SEMANTICS_LABELS = {"quarterly": "单季", "year_to_date": "年初至今累计", "annual": "完整财年"}
STEP_LABELS = {"operating_to_pretax": "营业利润 → 税前利润",
               "pretax_to_net_income": "税前利润 → 归母净利润",
               "net_income_to_common": "归母净利润 → 普通股股东净利润"}
STATUS_LABELS = {"complete": "完整勾稽", "residual": "有未解释差额", "missing_rows": "缺报表行"}


class ReturnAttributionError(RuntimeError):
    """Base error."""


class ReturnAttributionConflict(ReturnAttributionError):
    """A record no longer verifies, or a write is not permitted."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _row_factory(connection: sqlite3.Connection) -> None:
    if connection.row_factory is None:
        connection.row_factory = sqlite3.Row


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _text(value: Decimal | None, quantum: str | None = None) -> str | None:
    if value is None:
        return None
    if quantum is not None:
        value = value.quantize(Decimal(quantum), rounding=ROUND_HALF_EVEN)
    text = format(value, "f")
    if "." in text and quantum is None:
        text = text.rstrip("0").rstrip(".") or "0"
    return text


def _pct(value: Decimal | None) -> str | None:
    return None if value is None else _text(value * 100, "0.01")


def _date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def _days(start: str | None, end: str) -> int | None:
    if not start:
        return None
    return (_date(end) - _date(start)).days + 1


def _month_end(year: int, month: int) -> date:
    following = date(year + (month // 12), month % 12 + 1, 1)
    return following - timedelta(days=1)


def _record(body: Mapping[str, Any]) -> dict[str, Any]:
    record = json.loads(canonical_json(dict(body)))
    record.pop("content_hash", None)
    record["content_hash"] = content_hash(record)
    return record


def _decode(row: Any, name: str) -> dict[str, Any]:
    if row is None:
        raise ReturnAttributionConflict(f"{name} is missing")
    wire = json.loads(row["record_json"])
    body = dict(wire)
    asserted = body.pop("content_hash", None)
    if asserted != content_hash(body) or asserted != row["content_hash"]:
        raise ReturnAttributionConflict(f"{name} content hash drifted")
    return wire


def statement_line_ref(line_id: str) -> str:
    return f"statement-line:{line_id}"


def _precision(value: Decimal) -> int:
    """Significant precision: more is better when a filer states a value twice."""

    text = format(value.normalize(), "f") if value != 0 else "0"
    digits = text.replace("-", "").replace(".", "").strip("0")
    return len(digits)


# ---------------------------------------------------------------------------
# statement lines: periods, fiscal calendar
# ---------------------------------------------------------------------------


def _filings(connection: sqlite3.Connection, company_ref: str) -> list[dict[str, Any]]:
    _row_factory(connection)
    if not table_exists(connection, "coverage_mission_statement_filings"):
        return []
    return [dict(row) for row in connection.execute(
        "SELECT ingest_id, accession, form, filed, report_date FROM "
        "coverage_mission_statement_filings WHERE company_ref=? ORDER BY filed, accession",
        (company_ref,))]


def _filing_lines(connection: sqlite3.Connection, ingest_id: str) -> list[dict[str, Any]]:
    from .statement_context import statement_rows

    return [dict(row) for row in statement_rows(connection, ingest_id)
            if row.get("statement") in {"income", "cash"}
            and row.get("dimension_axis") is None
            and not row.get("dimensions")
            and not row.get("dimension_count")]



class FiscalCalendar:
    """Fiscal year end read from the annual periods the filer stated."""

    def __init__(self, annual_ends: Sequence[str]) -> None:
        ends = sorted({_date(item) for item in annual_ends})
        self.month = ends[-1].month if ends else None
        self.month_end = bool(ends) and all(
            item == _month_end(item.year, item.month) for item in ends)
        self.day = ends[-1].day if ends else None

    @property
    def known(self) -> bool:
        return self.month is not None

    def year_end(self, year: int) -> date:
        assert self.month is not None and self.day is not None
        if self.month_end:
            return _month_end(year, self.month)
        return date(year, self.month, min(self.day, 28 if self.month == 2 else self.day))

    def _is_year_end(self, day: date) -> bool:
        return any(abs((day - self.year_end(year)).days) <= 7
                   for year in (day.year - 1, day.year, day.year + 1))

    def fy_start(self, period_end: str) -> date | None:
        """The first day of the fiscal year that contains ``period_end``."""

        if not self.known:
            return None
        end = _date(period_end)
        prior = [self.year_end(year) for year in (end.year - 2, end.year - 1, end.year)]
        prior = [item for item in prior if item < end]
        return max(prior) + timedelta(days=1)

    def fiscal_year(self, period_end: str) -> int | None:
        start = self.fy_start(period_end)
        if start is None:
            return None
        return self.year_end(start.year if self.year_end(start.year) >= start else start.year + 1).year

    def semantics(self, start: str | None, end: str) -> str | None:
        days = _days(start, end)
        if days is None:
            return None
        if 350 <= days <= 380:
            # Twelve months ending anywhere but the fiscal year end is a
            # trailing figure the filer chose to state, not a fiscal year.
            if self.known and not self._is_year_end(_date(end)):
                return None
            return "annual"
        if 80 <= days <= 100:
            return "quarterly"
        fy_start = self.fy_start(end)
        if fy_start is not None and abs((_date(start) - fy_start).days) <= 7 \
                and (160 <= days <= 200 or 250 <= days <= 290):
            return "year_to_date"
        return None

    def label(self, start: str | None, end: str, semantics: str) -> str:
        year = self.fiscal_year(end)
        prefix = f"FY{year}" if year else end[:4]
        if semantics == "annual":
            return prefix
        fy_start = self.fy_start(end)
        months = round(((_date(end) - fy_start).days + 1) / 30.44) if fy_start else None
        if semantics == "quarterly":
            return f"{prefix} Q{max(1, min(4, round(months / 3)))}" if months else f"{prefix} 季度"
        return f"{prefix} 前{months}个月" if months else f"{prefix} 累计"


def _pick(lines: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """One line per concept: the most precise value, then the first ordinal."""

    usable = [line for line in lines if _dec(line.get("value")) is not None]
    if not usable:
        return None
    return sorted(usable, key=lambda line: (-_precision(_dec(line["value"])), line["ordinal"]))[0]


def collect_periods(connection: sqlite3.Connection, company_ref: str, *,
                    as_of: str | None = None) -> dict[str, Any]:
    """Every (start, end) period the filer stated, with the filing it is read from.

    The *original* filing (the one whose report date is the period end) is
    preferred; a period only held as a later filing's comparative is read from
    the newest filing that states it, and says so.
    """

    filings = [filing for filing in _filings(connection, company_ref)
               if as_of is None or filing["filed"] <= as_of]
    by_period: dict[tuple[str, str], list[tuple[dict[str, Any], list[dict[str, Any]]]]] = {}
    annual_ends: list[str] = []
    for filing in filings:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for line in _filing_lines(connection, filing["ingest_id"]):
            if not line["period_start"]:
                continue
            grouped.setdefault((line["period_start"], line["period_end"]), []).append(line)
        for key, lines in grouped.items():
            by_period.setdefault(key, []).append((filing, lines))
            days = _days(*key)
            # Only a 10-K's twelve months define the fiscal year: a 10-Q may
            # state a trailing twelve months too (Amazon's cash flow does).
            if filing["form"] == "10-K" and days is not None and 350 <= days <= 380:
                annual_ends.append(key[1])
    calendar = FiscalCalendar(annual_ends)
    periods: dict[tuple[str, str], dict[str, Any]] = {}
    skipped: list[str] = []
    for key, sources in sorted(by_period.items()):
        semantics = calendar.semantics(*key)
        if semantics is None:
            skipped.append(f"{key[0]}..{key[1]}")
            continue
        # The fullest statement of the period wins (a later filing may repeat
        # only its net income); then the original over a comparative; then the
        # newest filing.
        def rank(item: tuple[dict[str, Any], list[dict[str, Any]]]) -> tuple[int, int, str]:
            filing, lines = item
            return (sum(1 for line in lines if line["statement"] == "income"),
                    int(filing["report_date"] == key[1]), filing["filed"])

        filing, lines = max(sources, key=rank)
        originals = [filing] if filing["report_date"] == key[1] else []
        concepts: dict[str, list[dict[str, Any]]] = {}
        for line in lines:
            concepts.setdefault((line["statement"], line["concept"]), []).append(line)
        chosen = {key2: _pick(value) for key2, value in concepts.items()}
        periods[key] = {
            "start": key[0], "end": key[1], "semantics": semantics,
            "fiscal_label": calendar.label(key[0], key[1], semantics),
            "filing": {"ingest_id": filing["ingest_id"], "accession": filing["accession"],
                       "form": filing["form"], "filed": filing["filed"],
                       "report_date": filing["report_date"],
                       "role": "original" if originals else "comparative"},
            "lines": {k: v for k, v in chosen.items() if v is not None},
        }
    return {"calendar": calendar, "periods": periods, "skipped": skipped,
            "filing_count": len(filings)}


def _line_view(line: Mapping[str, Any], period: Mapping[str, Any], *,
               classification: str, sign: int, sign_basis: str, step: str | None) -> dict[str, Any]:
    value = _dec(line["value"])
    return {
        "ref": line.get("authority_ref") or statement_line_ref(line["line_id"]), "concept": line["concept"],
        "label": line["label"], "statement": line["statement"],
        "accession": period["filing"]["accession"], "ordinal": line["ordinal"],
        "classification": classification, "step": step, "sign": sign, "sign_basis": sign_basis,
        "value": _text(value), "signed_value": _text(value * sign),
        **({"source_evidence": line["source_evidence"]} if line.get("source_evidence") else {}),
    }


def _find(period: Mapping[str, Any], concepts: Sequence[str],
          statements: Sequence[str] = ("income",)) -> Mapping[str, Any] | None:
    for statement in statements:
        for concept in concepts:
            line = period["lines"].get((statement, concept))
            if line is not None:
                return line
    return None


def _quantum(values: Sequence[Decimal]) -> Decimal:
    quantum = Decimal(1)
    for exponent in range(6, 0, -1):
        candidate = Decimal(10) ** exponent
        if all(value == value.to_integral_value() and value % candidate == 0
               for value in values if value != 0) and any(values):
            quantum = candidate
            break
    if any(value != value.to_integral_value() for value in values):
        quantum = Decimal("0.01")
    return quantum


def _reconcile(step: str, *, start: Decimal | None, start_ref: str | None,
               items: Sequence[Mapping[str, Any]], filed: Decimal | None,
               filed_ref: str | None, missing: Sequence[str]) -> dict[str, Any]:
    if missing:
        return {"step": step, "status": "missing_anchor", "missing": list(missing),
                "start": _text(start), "start_ref": start_ref, "filed": _text(filed),
                "filed_ref": filed_ref, "items_sum": None, "expected": None,
                "residual": None, "tolerance": None}
    assert start is not None and filed is not None
    signed = [Decimal(item["signed_value"]) for item in items]
    expected = start + sum(signed, Decimal(0))
    residual = filed - expected
    quantum = _quantum([start, filed, *[Decimal(item["value"]) for item in items]])
    tolerance = max(abs(filed) * RELATIVE_TOLERANCE, quantum * (len(items) + 2) / 2)
    status = "complete" if abs(residual) <= tolerance else "residual"
    return {"step": step, "status": status, "missing": [], "start": _text(start),
            "start_ref": start_ref, "filed": _text(filed), "filed_ref": filed_ref,
            "items_sum": _text(sum(signed, Decimal(0))), "expected": _text(expected),
            "residual": _text(residual), "tolerance": _text(tolerance)}


def build_income_bridge(company_ref: str, period: Mapping[str, Any]) -> dict[str, Any]:
    """The typed bridge for one filed period.  Pure: same period, same bytes."""

    lines_out: list[dict[str, Any]] = []
    anchors: dict[str, Any] = {}
    for role, concepts in ANCHOR_CONCEPTS.items():
        statements = ("income", "cash") if role == "net_income" else ("income",)
        line = _find(period, concepts, statements)
        anchors[role] = None if line is None else _line_view(
            line, period, classification=role, sign=1, sign_basis="anchor", step=None)
    oi, pretax, ni = (anchors["operating_income"], anchors["pretax_income"], anchors["net_income"])
    income = {concept: line for (statement, concept), line in period["lines"].items()
              if statement == "income"}
    used: set[str] = set()
    # Step 1: operating income -> pre-tax income.  The items are the lines the
    # filer hangs under pre-tax income other than operating income (expanded
    # to their own children where a subtotal has them), else the classified
    # non-operating concepts.
    step1_items: list[dict[str, Any]] = []
    subtotals: list[dict[str, Any]] = []
    if oi is not None and pretax is not None:
        children = [line for concept, line in income.items()
                    if line.get("parent_concept") == pretax["concept"]
                    and concept not in OPERATING_TOTALS
                    and concept not in ANCHOR_CONCEPTS["pretax_income"]
                    and ITEM_CONCEPTS.get(concept, ("", 0))[0] not in {"tax", "minority_interest",
                                                                       "discontinued_operations",
                                                                       "preferred"}]
        if not children:
            children = [line for concept, line in income.items()
                        if ITEM_CONCEPTS.get(concept, ("", 0))[0] in NON_OPERATING_CLASSES
                        and concept != "us-gaap:NonoperatingIncomeExpense"]
        expanded: list[Mapping[str, Any]] = []
        for line in sorted(children, key=lambda item: item["ordinal"]):
            grandchildren = [item for item in income.values()
                             if item.get("parent_concept") == line["concept"]]
            if grandchildren:
                total = sum((ITEM_CONCEPTS.get(item["concept"], ("", 1))[1] * _dec(item["value"])
                             for item in grandchildren), Decimal(0))
                subtotals.append({"concept": line["concept"], "ref": line.get("authority_ref") or statement_line_ref(line["line_id"]),
                                  "filed": _text(_dec(line["value"])),
                                  "children_sum": _text(total),
                                  "gap": _text(_dec(line["value"]) - total)})
                expanded.extend(sorted(grandchildren, key=lambda item: item["ordinal"]))
            else:
                expanded.append(line)
        for line in expanded:
            classification, sign = ITEM_CONCEPTS.get(line["concept"], (None, 0))
            basis = "concept_table"
            if classification is None:
                classification = "other_non_operating"
                balance = (line.get("balance") or "").lower()
                sign, basis = ((-1, "balance_debit") if balance == "debit"
                               else (1, "balance_credit") if balance == "credit"
                               else (1, "as_reported_unknown_balance"))
            used.add(line["concept"])
            step1_items.append(_line_view(line, period, classification=classification, sign=sign,
                                          sign_basis=basis, step="operating_to_pretax"))
    missing1 = [role for role, value in (("operating_income", oi), ("pretax_income", pretax))
                if value is None]
    step1 = _reconcile("operating_to_pretax",
                       start=None if oi is None else Decimal(oi["value"]),
                       start_ref=None if oi is None else oi["ref"], items=step1_items,
                       filed=None if pretax is None else Decimal(pretax["value"]),
                       filed_ref=None if pretax is None else pretax["ref"], missing=missing1)
    # Step 2: pre-tax -> net income (to the parent): tax, after-tax equity
    # method, discontinued operations, minority interest.
    step2_items: list[dict[str, Any]] = []
    tax_line = income.get("us-gaap:IncomeTaxExpenseBenefit")
    for concept, line in sorted(income.items(), key=lambda item: item[1]["ordinal"]):
        classification, sign = ITEM_CONCEPTS.get(concept, (None, 0))
        if classification not in {"tax", "equity_method", "discontinued_operations",
                                  "minority_interest"} or concept in used:
            continue
        if classification == "equity_method" and pretax is not None \
                and line.get("parent_concept") == pretax["concept"]:
            continue
        step2_items.append(_line_view(line, period, classification=classification, sign=sign,
                                      sign_basis="concept_table", step="pretax_to_net_income"))
    missing2 = [role for role, value in (("pretax_income", pretax), ("tax", tax_line),
                                         ("net_income", ni)) if value is None]
    step2 = _reconcile("pretax_to_net_income",
                       start=None if pretax is None else Decimal(pretax["value"]),
                       start_ref=None if pretax is None else pretax["ref"], items=step2_items,
                       filed=None if ni is None else Decimal(ni["value"]),
                       filed_ref=None if ni is None else ni["ref"],
                       missing=missing2)
    # Step 3: net income -> net income to common (preferred dividends).
    step3_items = [_line_view(line, period, classification="preferred", sign=-1,
                              sign_basis="concept_table", step="net_income_to_common")
                   for concept, line in sorted(income.items(), key=lambda item: item[1]["ordinal"])
                   if ITEM_CONCEPTS.get(concept, ("", 0))[0] == "preferred"]
    common = anchors["net_income_to_common"]
    if ni is None:
        step3 = _reconcile("net_income_to_common", start=None, start_ref=None, items=step3_items,
                           filed=None, filed_ref=None, missing=["net_income"])
        to_common = {"value": None, "basis": "unavailable", "ref": None}
    elif common is not None:
        step3 = _reconcile("net_income_to_common", start=Decimal(ni["value"]), start_ref=ni["ref"],
                           items=step3_items, filed=Decimal(common["value"]),
                           filed_ref=common["ref"], missing=[])
        to_common = {"value": common["value"], "basis": "filed", "ref": common["ref"]}
    else:
        computed = Decimal(ni["value"]) + sum((Decimal(item["signed_value"]) for item in step3_items),
                                              Decimal(0))
        step3 = {"step": "net_income_to_common", "status": "complete", "missing": [],
                 "start": ni["value"], "start_ref": ni["ref"], "filed": None, "filed_ref": None,
                 "items_sum": _text(computed - Decimal(ni["value"])), "expected": _text(computed),
                 "residual": None, "tolerance": None,
                 "note": ("没有申报“归属普通股净利润”行，也没有优先股行：等于归母净利润"
                          if not step3_items else "没有申报“归属普通股净利润”行：归母净利润减优先股行")}
        to_common = {"value": _text(computed),
                     "basis": "no_preferred_line" if not step3_items else "net_income_less_preferred",
                     "ref": ni["ref"]}
    steps = [step1, step2, step3]
    missing_rows = sorted({item for step in steps for item in step["missing"]})
    residuals = [{"step": step["step"], "residual": step["residual"], "tolerance": step["tolerance"]}
                 for step in steps if step["status"] == "residual"]
    status = "missing_rows" if missing_rows else ("residual" if residuals else "complete")
    lines_out = [anchor for anchor in (anchors["revenue"], oi) if anchor is not None]
    lines_out += step1_items + ([pretax] if pretax else []) + step2_items + ([ni] if ni else [])
    lines_out += step3_items + ([common] if common else [])
    return {
        "schema_version": SCHEMA_VERSION, "kind": KIND, "object": "IncomeBridgeVersion",
        "formula_ref": BRIDGE_FORMULA_REF, "company_ref": company_ref,
        "bridge_ref": income_bridge_ref(company_ref, period["start"], period["end"]),
        "period": {"start": period["start"], "end": period["end"],
                   "semantics": period["semantics"], "fiscal_label": period["fiscal_label"]},
        "source": dict(period["filing"]),
        "anchors": anchors, "lines": lines_out, "steps": steps, "subtotals": subtotals,
        "net_income_to_common": to_common, "status": status, "missing_rows": missing_rows,
        "residuals": residuals,
        "tolerance_rule": "max(|申报数| × 0.05%, 每项半个舍入单位)",
    }


def income_bridge_ref(company_ref: str, start: str, end: str) -> str:
    return "income-bridge:" + content_hash({"company": company_ref, "start": start,
                                            "end": end})[:32]


def build_income_bridges(collected: Mapping[str, Any], company_ref: str) -> list[dict[str, Any]]:
    return [build_income_bridge(company_ref, period)
            for _key, period in sorted(collected["periods"].items())]


# ---------------------------------------------------------------------------
# the per-share series
# ---------------------------------------------------------------------------


def per_share_series_ref(company_ref: str) -> str:
    return "per-share-series:" + content_hash({"company": company_ref})[:32]


def _value_cell(line: Mapping[str, Any] | None, period: Mapping[str, Any]) -> dict[str, Any] | None:
    if line is None:
        return None
    return {"value": _text(_dec(line["value"])), "ref": line.get("authority_ref") or statement_line_ref(line["line_id"]),
            "concept": line["concept"], "accession": period["filing"]["accession"],
            "basis": "filed", "available_on": period["filing"]["filed"],
            **({"source_evidence": line["source_evidence"]} if line.get("source_evidence") else {})}


def build_per_share_series(company_ref: str, collected: Mapping[str, Any],
                           bridges: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    calendar: FiscalCalendar = collected["calendar"]
    periods = collected["periods"]
    by_key = {(item["period"]["start"], item["period"]["end"]): item for item in bridges}
    pays_dividends = any(
        _find(period, PER_SHARE_CONCEPTS["dps"]) is not None
        or _find(period, DIVIDEND_CASH_CONCEPTS, ("cash",)) is not None
        for period in periods.values())
    rows: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for key, period in sorted(periods.items(), key=lambda item: (item[0][1], item[0][0])):
        row: dict[str, Any] = {"start": period["start"], "end": period["end"],
                               "semantics": period["semantics"],
                               "fiscal_label": period["fiscal_label"],
                               "accession": period["filing"]["accession"],
                               "filing_role": period["filing"]["role"],
                               "available_on": period["filing"]["filed"]}
        for field, concepts in PER_SHARE_CONCEPTS.items():
            row[field] = _value_cell(_find(period, concepts), period)
        if row["dps"] is None and pays_dividends:
            cash = _find(period, DIVIDEND_CASH_CONCEPTS, ("cash",))
            basic = row["basic_shares"]
            if cash is not None and basic is not None and Decimal(basic["value"]) > 0:
                value = abs(_dec(cash["value"])) / Decimal(basic["value"])
                row["dps"] = {"value": _text(value, "0.0001"),
                              "ref": statement_line_ref(cash["line_id"]),
                              "concept": cash["concept"], "accession": period["filing"]["accession"],
                              "basis": "derived_cash_dividends_over_basic_shares",
                              "inputs": [statement_line_ref(cash["line_id"]), basic["ref"]]}
        row["dividend_status"] = "filed" if row["dps"] is not None else "not_collected"
        repurchase = _find(period, REPURCHASE_CASH_CONCEPTS, ("cash",))
        row["repurchase_cash"] = _value_cell(repurchase, period)
        bridge = by_key.get(key)
        row["net_income_to_common"] = None if bridge is None else {
            "value": bridge["net_income_to_common"]["value"],
            "basis": bridge["net_income_to_common"]["basis"], "bridge_ref": bridge["bridge_ref"]}
        revenue = None if bridge is None else bridge["anchors"].get("revenue")
        row["revenue"] = None if revenue is None else {"value": revenue["value"], "ref": revenue["ref"]}
        # Is diluted EPS what net income to common over diluted shares says?
        check = "not_checkable"
        if (row["diluted_eps"] and row["diluted_shares"] and row["net_income_to_common"]
                and row["net_income_to_common"]["value"] is not None
                and Decimal(row["diluted_shares"]["value"]) > 0):
            implied = (Decimal(row["net_income_to_common"]["value"])
                       / Decimal(row["diluted_shares"]["value"]))
            filed = Decimal(row["diluted_eps"]["value"])
            check = ("consistent" if abs(implied - filed) <= Decimal("0.015") + abs(filed) * Decimal("0.01")
                     else "inconsistent")
            row["implied_diluted_eps"] = _text(implied, "0.0001")
        row["eps_check"] = check
        row["missing"] = [field for field in REQUIRED_PER_SHARE if row[field] is None]
        for field in row["missing"]:
            missing.append({"start": period["start"], "end": period["end"],
                            "fiscal_label": period["fiscal_label"], "field": field,
                            "label": PER_SHARE_LABELS[field]})
        rows.append(row)
    # Fiscal fourth quarters: stated by nobody we cover, never derived here.
    quarter_not_stated = []
    for (start, end), period in sorted(periods.items()):
        if period["semantics"] != "annual":
            continue
        q4_start = None
        held = [item for (s, e), item in periods.items()
                if item["semantics"] == "quarterly" and e == end]
        if not held:
            q4_start = (_date(end) - timedelta(days=91)).isoformat()
            quarter_not_stated.append({"fiscal_label": period["fiscal_label"].split()[0] + " Q4",
                                       "end": end, "approximate_start": q4_start,
                                       "reason": "申报只给全年数；不以全年减前三季推算单季"})
    ttm = _ttm_points(rows, calendar)
    body = {
        "schema_version": SCHEMA_VERSION, "kind": KIND, "object": "PerShareSeriesVersion",
        "formula_ref": PER_SHARE_FORMULA_REF, "company_ref": company_ref,
        "series_ref": per_share_series_ref(company_ref),
        "fiscal_year_end": (None if not calendar.known
                            else f"{calendar.month:02d}-{calendar.year_end(2025).day:02d}"),
        "periods": rows, "missing": missing, "quarters_not_stated": quarter_not_stated,
        "skipped_periods": list(collected["skipped"]), "ttm": ttm,
        "pays_dividends": True if pays_dividends else None,
        "dividend_collection_status": "filed_period_data" if pays_dividends else "not_collected",
        "ttm_rules": dict(TTM_RULE_TEXT),
        "period_rule": "单季 80–100 天；完整财年 350–380 天；年初至今 = 自财年首日起 6 或 9 个月；其余期间不收录",
    }
    return body


def _ttm_points(rows: Sequence[Mapping[str, Any]], calendar: FiscalCalendar) -> list[dict[str, Any]]:
    by_key = {(row["start"], row["end"]): row for row in rows}

    def find(start: date, end: date, semantics: set[str]) -> Mapping[str, Any] | None:
        for (s, e), row in by_key.items():
            if row["semantics"] in semantics and abs((_date(s) - start).days) <= 7 \
                    and abs((_date(e) - end).days) <= 7:
                return row
        return None

    def value(row: Mapping[str, Any] | None, field: str) -> Decimal | None:
        if row is None or row.get(field) is None:
            return None
        if field == "net_income_to_common" and row[field].get("basis") == "no_preferred_line":
            return None
        return _dec(row[field].get("value"))

    points = []
    ends = sorted({row["end"] for row in rows if row["semantics"] in {"quarterly", "annual"}})
    for end in ends:
        end_date = _date(end)
        annual = next((row for row in rows if row["end"] == end and row["semantics"] == "annual"), None)
        fy_start = calendar.fy_start(end)
        ytd = prior_fy = prior_ytd = None
        if annual is None and fy_start is not None:
            ytd = find(fy_start, end_date, {"quarterly", "year_to_date"})
            prior_fy = find(fy_start - timedelta(days=365), fy_start - timedelta(days=1), {"annual"})
            prior_ytd = find(fy_start - timedelta(days=365), end_date - timedelta(days=365),
                             {"quarterly", "year_to_date"})
        point: dict[str, Any] = {"end": end, "fiscal_label": None, "rule": None, "components": []}
        if annual is not None:
            point.update(rule=TTM_RULE_ANNUAL, fiscal_label=annual["fiscal_label"],
                         components=[{"role": "annual", "start": annual["start"], "end": annual["end"]}])
        elif ytd is not None and prior_fy is not None and prior_ytd is not None:
            point.update(rule=TTM_RULE_YTD, fiscal_label=f"截至 {end} 的过去四个季度",
                         components=[{"role": "prior_fiscal_year", "start": prior_fy["start"], "end": prior_fy["end"]},
                                     {"role": "year_to_date", "start": ytd["start"], "end": ytd["end"]},
                                     {"role": "prior_year_to_date", "start": prior_ytd["start"],
                                      "end": prior_ytd["end"]}])
        else:
            point.update(reason="缺上一完整财年或上年同期累计，按规则不能组成过去四个季度")
            points.append(point)
            continue
        for field in ("net_income_to_common", "dps", "revenue",
                      "repurchase_cash"):
            if annual is not None:
                number = value(annual, field)
                refs = [annual[field].get("ref") or annual[field].get("bridge_ref")] if annual.get(field) else []
            else:
                parts = [value(prior_fy, field), value(ytd, field), value(prior_ytd, field)]
                number = None if any(item is None for item in parts) else parts[0] + parts[1] - parts[2]
                refs = [(row[field].get("ref") or row[field].get("bridge_ref"))
                        for row in (prior_fy, ytd, prior_ytd) if row.get(field)]
            point[field] = None if number is None else {
                "value": _text(number), "refs": [ref for ref in refs if ref]}
        sources = [annual] if annual is not None else [prior_fy, ytd, prior_ytd]
        point["available_on"] = max(row.get("available_on") or "9999-12-31" for row in sources)
        for share_field, eps_field in (("diluted_shares", "diluted_eps"),
                                       ("basic_shares", "basic_eps")):
            numbers = [value(row, share_field) for row in sources]
            weighted = None
            if all(number is not None for number in numbers):
                days = [(_date(row["end"]) - _date(row["start"])).days + 1 for row in sources]
                if annual is not None:
                    weighted = numbers[0]
                else:
                    span = days[0] + days[1] - days[2]
                    if span > 0:
                        weighted = (numbers[0] * days[0] + numbers[1] * days[1]
                                    - numbers[2] * days[2]) / span
            refs = [row[share_field].get("ref") for row in sources if row.get(share_field)]
            point[share_field] = None if weighted is None or weighted <= 0 else {
                "value": _text(weighted), "refs": [ref for ref in refs if ref],
                "basis": "annual_weighted" if annual is not None else "ttm_day_weighted"}
            # Filed annual EPS is authoritative, including dilution/rounding adjustments.
            filed = value(annual, eps_field)
            if annual is not None and filed is not None:
                point[eps_field] = {"value": _text(filed), "refs": [annual[eps_field].get("ref")],
                                    "basis": "filed_annual"}
            else:
                numerator = (point.get("net_income_to_common") or {}).get("value")
                point[eps_field] = None if numerator is None or weighted is None or weighted <= 0 else {
                    "value": _text(Decimal(numerator) / weighted, "0.0001"),
                    "refs": [*refs, *(point["net_income_to_common"].get("refs") or [])],
                    "basis": "ttm_common_income_over_day_weighted_shares"}
        points.append(point)
    return points


# ---------------------------------------------------------------------------
# the return bridge
# ---------------------------------------------------------------------------


def return_bridge_ref(company_ref: str) -> str:
    return "holding-period-return:" + content_hash({"company": company_ref})[:32]


def _ln(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return value.ln()


def decompose(factors: Mapping[str, Decimal | None], dividend_yield: Decimal | None) -> dict[str, Any]:
    """Multiplicative factors -> additive contributions (percentage points).

    price factor = Π factors; each factor's share of the price return is its
    log weight, so the contributions add up to the price return exactly (to
    rounding).  Dividends are added on top: total = price return + yield.
    """

    known = {name: value for name, value in factors.items() if value is not None}
    price_factor = Decimal(1)
    for value in known.values():
        price_factor *= value
    price_return = price_factor - 1
    logs = {name: _ln(value) for name, value in known.items() if value > 0}
    total_log = sum(logs.values(), Decimal(0))
    contributions: dict[str, str | None] = {}
    for name in factors:
        if name not in known:
            contributions[name] = None
        elif total_log != 0 and len(logs) == len(known):
            contributions[name] = _pct(price_return * logs[name] / total_log)
        else:
            contributions[name] = _pct(known[name] - 1)
    contributions["dividends"] = _pct(dividend_yield)
    total = None if dividend_yield is None else price_return + dividend_yield
    return {"factors": {name: _text(value, "0.000001") for name, value in factors.items()
                        if value is not None} | {name: None for name in factors if factors[name] is None},
            "contributions_pp": contributions, "price_return_percent": _pct(price_return),
            "dividend_yield_percent": _pct(dividend_yield),
            "total_return_percent": _pct(total)}


def _unavailable(kind: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"leg": kind, "label": LEG_LABELS[kind], "status": "unavailable", "reason": reason,
            **extra}


def _latest_point(series: Mapping[str, Any], *, on_or_before: str | None = None,
                  field: str = "diluted_eps") -> Mapping[str, Any] | None:
    source = series.get("ttm_vintages") if on_or_before is not None else None
    points = [point for point in (source if source is not None else series.get("ttm") or ())
              if point.get(field) is not None
              and (on_or_before is None or (point["end"] <= on_or_before
                   and (point.get("available_on") or "9999-12-31") <= on_or_before))]
    return max(points, key=lambda point: (point["end"], point.get("available_on") or "")) if points else None


def _close_on_or_before(price: Mapping[str, Any], day: str) -> Mapping[str, Any] | None:
    bars = [bar for bar in price.get("bars") or () if bar.get("date") and bar["date"] <= day
            and _dec(bar.get("close")) is not None]
    return max(bars, key=lambda bar: bar["date"]) if bars else None


def _trailing_pe(snapshot: Mapping[str, Any]) -> str | None:
    for metric in snapshot.get("metrics") or ():
        if metric.get("metric") == "trailing_pe" and metric.get("status") == "available":
            return metric.get("value")
    return None


def dividend_cash_window(evidence: Mapping[str, Any] | None, start: str, end: str) -> dict[str, Any]:
    """Cash paid within (start,end], never a fiscal aggregate or ex-date proxy."""
    if evidence is None or evidence.get("coverage_start", "9999") > start or evidence.get("coverage_end", "") < end:
        return {"status": "not_collected", "cash_per_share": None, "payments": [],
                "reason": "payment-date evidence does not cover the holding window"}
    try:
        exact = validate_dividend_evidence(evidence)
    except ReturnAttributionConflict as exc:
        return {"status": "invalid_evidence", "cash_per_share": None, "payments": [], "reason": str(exc)}
    payments = [row for row in exact["payments"] if start < row["payment_date"] <= end]
    return {"status": "confirmed_zero" if not payments else "paid", "cash_per_share":
            _text(sum((Decimal(row["cash_per_share"]) for row in payments), Decimal(0))),
            "share_class_ref": exact["share_class_ref"], "payments": payments,
            "evidence_hash": content_hash(exact), "currency": exact["currency"]}


def validate_dividend_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    exact = json.loads(canonical_json(value))
    expected = {"company_ref", "coverage_start", "coverage_end", "share_class_ref", "currency",
                "source_ref", "source_hash", "payments"}
    if set(exact) != expected or not all(isinstance(exact.get(key), str) and exact[key]
            for key in expected - {"payments"}):
        raise ReturnAttributionConflict("dividend evidence has incomplete source or share-class identity")
    if len(exact["source_hash"]) != 64 or any(ch not in "0123456789abcdef" for ch in exact["source_hash"]):
        raise ReturnAttributionConflict("dividend source hash is invalid")
    try:
        if _date(exact["coverage_start"]) > _date(exact["coverage_end"]):
            raise ValueError("reversed coverage")
        seen = set()
        for row in exact["payments"]:
            if set(row) != {"payment_date", "cash_per_share", "evidence_ref"} or not row["evidence_ref"]:
                raise ValueError("payment lacks exact date and evidence")
            day = _date(row["payment_date"])
            cash = _dec(row["cash_per_share"])
            if cash is None or cash < 0 or not _date(exact["coverage_start"]) <= day <= _date(exact["coverage_end"]):
                raise ValueError("invalid payment")
            if row["evidence_ref"] in seen:
                raise ValueError("duplicate payment evidence")
            seen.add(row["evidence_ref"])
    except (ValueError, TypeError) as exc:
        raise ReturnAttributionConflict("dividend payment evidence is invalid") from exc
    return exact


def _realised_leg(series: Mapping[str, Any], snapshot: Mapping[str, Any] | None,
                  price: Mapping[str, Any] | None,
                  dividend_payments: Mapping[str, Any] | None = None) -> dict[str, Any]:
    kind = "realised_trailing_year"
    if snapshot is None or price is None:
        return _unavailable(kind, "还没有估值快照或价格序列")
    end_day = snapshot["price"]["bar_date"]
    start_day = (_date(end_day) - timedelta(days=REALISED_WINDOW_DAYS)).isoformat()
    p_end = _dec(snapshot["price"]["close"])
    start_bar = _close_on_or_before(price, start_day)
    if start_bar is None or p_end is None:
        return _unavailable(kind, f"价格序列没有 {start_day} 之前的收盘价")
    p_start = Decimal(start_bar["close"])
    end_point = _latest_point(series, on_or_before=end_day)
    start_point = _latest_point(series, on_or_before=start_day)
    if end_point is None or start_point is None:
        return _unavailable(kind, "每股序列凑不齐窗口两端的过去四季 EPS（缺年报或上年同期累计）",
                            price_start=_text(p_start), price_end=_text(p_end))
    eps_end, eps_start = Decimal(end_point["diluted_eps"]["value"]), Decimal(start_point["diluted_eps"]["value"])
    if eps_end <= 0 or eps_start <= 0:
        return _unavailable(kind, "过去四季稀释 EPS 不为正，市盈率没有意义，不做倍数分解")
    shares_end = _dec((end_point.get("diluted_shares") or {}).get("value"))
    shares_start = _dec((start_point.get("diluted_shares") or {}).get("value"))
    eps_factor = eps_end / eps_start
    share_factor = (shares_start / shares_end) if shares_end and shares_start else None
    earnings_factor = eps_factor / share_factor if share_factor else eps_factor
    multiple_factor = (p_end / p_start) / eps_factor
    dps = _dec((end_point.get("dps") or {}).get("value"))
    payments = dividend_cash_window(dividend_payments, start_bar["date"], end_day)
    if dividend_payments is not None and ((snapshot.get("shares") or {}).get("share_class_ref") != dividend_payments.get("share_class_ref")
            or snapshot.get("price", {}).get("currency") != dividend_payments.get("currency")):
        payments = {"status": "share_class_or_currency_unbound", "cash_per_share": None, "payments": [],
                    "reason": "price/share class or currency does not bind exact payment evidence"}
    dividend_yield = None if payments["cash_per_share"] is None else Decimal(payments["cash_per_share"]) / p_start
    out = decompose({"earnings_growth": earnings_factor, "share_count": share_factor,
                     "multiple_change": multiple_factor}, dividend_yield)
    notes = ["窗口两端只读取截至交易日已披露的EPS与股数版本；季度EPS不相加"]
    if share_factor is None:
        notes.append("缺稀释股数行：回购效应并入盈利增长，未单独拆出")
    if payments["cash_per_share"] is None:
        notes.append("缺支付日及股类证据：股息与总回报不可用")
    return {
        "leg": kind, "label": LEG_LABELS[kind], "status": "available",
        "window": {"start": start_bar["date"], "end": end_day},
        "price_start": _text(p_start), "price_end": _text(p_end),
        "eps_start": {"value": _text(eps_start), "end": start_point["end"], "rule": start_point["rule"]},
        "eps_end": {"value": _text(eps_end), "end": end_point["end"], "rule": end_point["rule"]},
        "pe_start": _text(p_start / eps_start, "0.01"), "pe_end": _text(p_end / eps_end, "0.01"),
        "diluted_shares_start": _text(shares_start), "diluted_shares_end": _text(shares_end),
        "dps_trailing": _text(dps), "dividend_payments": payments,
        **out, "notes": notes,
        "inputs": {"price_version_ref": snapshot["price"]["version_ref"],
                   "price_start_bar": start_bar["date"],
                   "valuation_snapshot_ref": snapshot["id"],
                   "per_share_points": [start_point["end"], end_point["end"]]},
        "expectation_source_kind": None,
    }


def _model_cells(model: Mapping[str, Any], role: str) -> list[Mapping[str, Any]] | None:
    for result in model.get("results") or ():
        if result.get("role") == role or result.get("ref") == f"result:{role}":
            cells = [cell for cell in result.get("cells") or ()
                     if (cell.get("period") or {}).get("kind") == "quarter"]
            cells.sort(key=lambda cell: cell["period"]["end"])
            return cells
    return None


def _first_computed(cells: Sequence[Mapping[str, Any]] | None, after: str) -> list[Mapping[str, Any]] | None:
    if not cells:
        return None
    ahead = [cell for cell in cells if cell["period"]["end"] > after][:FORWARD_QUARTERS]
    if len(ahead) < FORWARD_QUARTERS or any(cell.get("status") != "computed"
                                            or _dec(cell.get("value")) is None for cell in ahead):
        return None
    return ahead


#: 2026-10-09 (P0, research artifact quality review): the earnings a driver
#: leg compounds.  Core earnings are the model's operating income after the
#: model's own tax rate -- non-operating securities gains and losses (GOOGL's
#: ~$99B of unrealised gains) and the one-off lines the model holds at zero
#: never enter, on either side of the ratio.  Revenue x trailing net margin is
#: gone: it carried exactly the polluted margin the model refused to forecast.
CORE_EARNINGS_BASIS = "driver_model_operating_tax_proxy"
CORE_EARNINGS_RULE = ("营业税后近似 = 营业利润 ×（1 − 经异常筛查的GAAP税率）；不含营业外证券损益，"
                      "过去四季与未来四季同一口径")
#: A forward quarter whose operating margin is below this is "deeply negative".
LOSS_MARGIN = Decimal("-0.05")


def _structure_role_refs(model: Mapping[str, Any], role: str) -> list[str]:
    structure = model.get("financial_statement_structure") or {}
    return [str(line["ref"]) for line in structure.get("lines") or ()
            if isinstance(line, Mapping) and line.get("role") == role]


def _trailing_core(model: Mapping[str, Any], on_or_before: str) -> dict[str, Any] | None:
    """Last four filed quarters' operating income, pretax and tax, from the model's own history."""

    structure = model.get("financial_statement_structure")
    if not isinstance(structure, Mapping):
        return None
    try:
        from .model_forecast_driver import structure_historical_values

        values, _refs = structure_historical_values(model.get("drivers") or [], structure)
    except Exception:  # noqa: BLE001 - an unreplayable history is "unavailable"
        return None

    def series(role: str) -> dict[str, Decimal] | None:
        refs = _structure_role_refs(model, role)
        return values.get(refs[0]) if len(refs) == 1 and refs[0] in values else None

    operating = series("operating_income")
    if not operating:
        return None
    ends = sorted(end for end in operating if end <= on_or_before)[-4:]
    if len(ends) < 4:
        return None
    pretax, tax = series("pretax_income"), series("income_tax_expense")
    rate = None
    if pretax and tax and all(end in pretax and end in tax for end in ends):
        total = sum((pretax[end] for end in ends), Decimal(0))
        if total > 0:
            rate = sum((tax[end] for end in ends), Decimal(0)) / total
    if any(not 70 <= (_date(right) - _date(left)).days <= 105 for left, right in zip(ends, ends[1:])):
        return None
    review = []
    if not pretax or not tax or not all(end in pretax and end in tax for end in ends):
        review.append("缺同期税前利润或税额，不能审查营业外/一次性税项")
    else:
        rates = [tax[end] / pretax[end] for end in ends if pretax[end] > 0]
        if len(rates) != 4 or any(not Decimal("0.05") <= item <= Decimal("0.40") for item in rates):
            review.append("季度税前利润非正或有效税率超出5%–40%近似适用范围")
        if rates and max(rates) - min(rates) > Decimal("0.10"):
            review.append("季度税率相差超过10个百分点，存在离散税项风险")
        if any(abs(pretax[end] - operating[end]) > max(abs(operating[end]), Decimal(1)) * Decimal("0.20") for end in ends):
            review.append("营业外损益超过营业利润20%，GAAP有效税率不能自动充当核心税率")
    return {"ends": ends, "operating_income": sum((operating[end] for end in ends), Decimal(0)),
            "tax_rate": rate if rate is not None and Decimal(0) <= rate <= 1 else None,
            "tax_review": {"status": "clear_for_labelled_approximation" if not review else "requires_core_tax_authority",
                           "reasons": review, "rule_ref": "core-tax-proxy-review:1"}}


def model_profit_check(model: Mapping[str, Any] | None, *, quarters: int = 8) -> dict[str, Any]:
    """Whether the model's own forecast is loss-making, quarter by quarter.

    Read by every displayed return: a return shown next to a model that goes
    into operating or net losses carries the contradiction, never silently.
    """

    if model is None:
        return {"status": "no_model"}
    losses = []
    for role in ("operating_income", "net_income", "parent_net_income"):
        cells = [cell for cell in (_model_cells(model, role) or ())
                 if cell.get("kind", "estimate") == "estimate"][:quarters]
        for cell in cells:
            value = _dec(cell.get("value")) if cell.get("status") == "computed" else None
            if value is not None and value < 0:
                losses.append({"role": role, "period_end": cell["period"]["end"],
                               "value": _text(value, "1")})
    if not losses:
        return {"status": "ok"}
    first = min(losses, key=lambda item: item["period_end"])
    worst = min(losses, key=lambda item: Decimal(item["value"]))
    label = {"operating_income": "营业利润", "net_income": "净利润", "parent_net_income": "归母净利润"}
    notice = (f"矛盾提示：驱动模型自 {first['period_end']} 起预测{label[first['role']]}为负"
              f"（最低 {label[worst['role']]} {worst['value']}，{worst['period_end']}）；"
              "此处的回报数字并非来自这条亏损路径，读数时须同时看模型。")
    return {"status": "loss_making", "losses": losses[:16], "notice": notice}


def _driver_leg(series: Mapping[str, Any], snapshot: Mapping[str, Any] | None,
                model: Mapping[str, Any] | None) -> dict[str, Any]:
    kind = "expected_driver_model"
    if snapshot is None:
        return _unavailable(kind, "还没有估值快照")
    if model is None:
        return _unavailable(kind, "还没有驱动模型")
    point = _latest_point(series, field="net_income_to_common")
    if point is None or point.get("diluted_eps") is None:
        return _unavailable(kind, "每股序列没有过去四季的净利润与 EPS")
    p_now = Decimal(snapshot["price"]["close"])
    after = point["end"]
    net = None
    for role in ("parent_net_income", "net_income"):
        net = _first_computed(_model_cells(model, role), after)
        if net is not None:
            break
    if net is None:
        # 2026-10-09: no proxy.  Revenue x trailing net margin carried GOOGL's
        # securities gains (54.75% margin) and META's pre-loss margin into an
        # "expected return" the model itself did not support.
        return _unavailable(kind, "驱动模型没有算出未来四季净利润；不再用“收入 × 过去四季净利率”代理，"
                                  "预期回报不可得", model_ref=model.get("id"),
                            reason_code="model_net_income_unavailable")
    operating = _first_computed(_model_cells(model, "operating_income"), after)
    pretax = _first_computed(_model_cells(model, "pretax_income"), after)
    tax = _first_computed(_model_cells(model, "income_tax_expense"), after)
    if operating is None:
        return _unavailable(kind, "驱动模型没有算出未来四季营业利润，无法得到正常化核心利润（不含证券损益）",
                            model_ref=model.get("id"), reason_code="model_operating_income_unavailable")
    rate = None
    if pretax is not None and tax is not None:
        pretax_total = sum((Decimal(cell["value"]) for cell in pretax), Decimal(0))
        if pretax_total > 0:
            rate = sum((Decimal(cell["value"]) for cell in tax), Decimal(0)) / pretax_total
    trailing = _trailing_core(model, after)
    if rate is None or not Decimal(0) <= rate <= 1:
        rate = None if trailing is None else trailing["tax_rate"]
    if rate is None or trailing is None:
        return _unavailable(kind, "驱动模型缺税率或过去四季营业利润，无法按同一口径算核心利润",
                            model_ref=model.get("id"), reason_code="core_earnings_inputs_missing")
    oi_fwd = sum((Decimal(cell["value"]) for cell in operating), Decimal(0))
    if trailing["tax_review"]["status"] != "clear_for_labelled_approximation":
        return _unavailable(kind, "；".join(trailing["tax_review"]["reasons"]), model_ref=model.get("id"),
                            reason_code="normalised_core_tax_authority_required",
                            core_operating_income_ttm=_text(trailing["operating_income"], "1"),
                            core_operating_income_forward=_text(oi_fwd, "1"), tax_review=trailing["tax_review"])
    if not Decimal("0.05") <= rate <= Decimal("0.40"):
        return _unavailable(kind, "模型税率超出核心税率近似适用范围，需正常化税率证据", model_ref=model.get("id"),
                            reason_code="normalised_core_tax_authority_required")
    core_fwd = oi_fwd * (1 - rate)
    # One rate on both sides: a discrete tax item in the trailing year must
    # not move "growth" (the model's rate already leaves such quarters out).
    core_ttm = trailing["operating_income"] * (1 - rate)
    if core_ttm <= 0:
        return _unavailable(kind, "过去四季核心利润不为正，倍数分解没有意义",
                            model_ref=model.get("id"), reason_code="trailing_core_not_positive")
    if core_fwd <= 0:
        return _unavailable(kind, "驱动模型未来四季核心利润为负（模型预测亏损），不给出预期回报",
                            model_ref=model.get("id"), reason_code="model_core_earnings_negative",
                            core_earnings_forward=_text(core_fwd, "1"))
    share_cells = _first_computed(_model_cells(model, "diluted_weighted_average_shares"), after)
    shares_now = _dec((point.get("diluted_shares") or {}).get("value"))
    if share_cells is not None and shares_now:
        weights = [Decimal((_date(cell["period"]["end"]) - _date(cell["period"]["start"])).days + 1) for cell in share_cells]
        shares_fwd = sum((Decimal(cell["value"]) * weight for cell, weight in zip(share_cells, weights)), Decimal(0)) / sum(weights)
        share_factor = shares_now / shares_fwd
        share_basis = "driver_model_diluted_shares"
    else:
        share_factor = Decimal(1) if shares_now else None
        share_basis = "held_flat" if shares_now else "missing_diluted_shares"
    earnings_factor = core_fwd / core_ttm
    eps_now = None if not shares_now else core_ttm / shares_now
    dps = _dec((point.get("dps") or {}).get("value"))
    dividend_yield = None if dps is None else dps / p_now
    out = decompose({"earnings_growth": earnings_factor, "share_count": share_factor,
                     "multiple_change": Decimal(1)}, dividend_yield)
    notes = ["估值倍数按当前核心市盈率延续（市场价格隐含），不假设重估",
             f"盈利口径：{CORE_EARNINGS_RULE}（模型税率 {_pct(rate)}%）"]
    if share_basis == "held_flat":
        notes.append("模型没有稀释股数预测：股数按最近一期持平")
    elif share_basis == "missing_diluted_shares":
        notes.append("缺稀释股数行：回购效应无法拆出，核心每股收益不列示")
    eps_fwd = None if eps_now is None else eps_now * earnings_factor * (share_factor or Decimal(1))
    return {
        "leg": kind, "label": LEG_LABELS[kind] + "（营业税后近似）", "status": "available",
        "is_core_approximation": True, "core_eps_status": "unavailable_without_noncore_tax_allocation_authority",
        "window": {"start": operating[0]["period"]["start"], "end": operating[-1]["period"]["end"]},
        "price_start": _text(p_now),
        "eps_start": {"value": None if eps_now is None else _text(eps_now, "0.0001"),
                      "end": point["end"], "rule": "operating_tax_proxy_ttm / day_weighted_diluted_shares", "basis": "operating_tax_proxy"},
        "eps_end": {"value": None if eps_fwd is None else _text(eps_fwd, "0.0001"),
                    "end": operating[-1]["period"]["end"],
                    "rule": "eps_now × earnings_factor × share_factor"},
        "earnings_basis": CORE_EARNINGS_BASIS, "share_basis": share_basis,
        "operating_tax_proxy_earnings_ttm": _text(core_ttm, "1"), "operating_tax_proxy_earnings_forward": _text(core_fwd, "1"),
        "gaap_tax_proxy_rate": _text(rate, "0.000001"),
        "core_method": "operating_income_after_labelled_GAAP_tax_proxy",
        "tax_basis": "GAAP_effective_rate_after_no_material_nonoperating_or_discrete_tax_anomaly",
        "tax_review": trailing["tax_review"],
        "limitations": ["该税率为经异常筛查的GAAP近似，不等同披露的正常化核心税率；核心营业税后盈利不含融资成本"],
        "core_trailing_quarters": trailing["ends"],
        "net_income_forward": _text(sum((Decimal(cell["value"]) for cell in net), Decimal(0)), "1"),
        "dps_trailing": _text(dps), **out, "notes": notes,
        "inputs": {"forecast_model_version_ref": model.get("id"),
                   "forecast_model_hash": model.get("content_hash"),
                   "cell_refs": [cell.get("ref") for cell in operating]
                   + [cell.get("ref") for cell in (tax or ())],
                   "valuation_snapshot_ref": snapshot["id"], "per_share_point": point["end"]},
        "expectation_source_kind": "market_implied",
        "expectation_sources": {"earnings": None, "multiple": "market_implied"},
    }


def _consensus_row(consensus: Mapping[str, Any]) -> Mapping[str, Any] | None:
    last = (consensus.get("fiscal_calendar") or {}).get("last_reported_period_end") or ""
    rows = [row for row in consensus.get("eps_estimates") or ()
            if row.get("period_kind") == "fiscal_year" and _dec(row.get("avg")) is not None
            and _dec(row.get("year_ago")) not in (None, Decimal(0))
            and str(row.get("period_end") or "") > last]
    order = {"0y": 0, "+1y": 1}
    rows.sort(key=lambda row: (order.get(row.get("vendor_period"), 9), row.get("period_end") or ""))
    return rows[0] if rows else None


def _relabelled_eps(consensus: Mapping[str, Any],
                    relabel: Mapping[str, Any] | None) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    """EPS rows and calendar: batch B's relabelled periods when given, else the stored ones."""

    if relabel and relabel.get("eps_estimates") is not None and relabel.get("calendar"):
        calendar = relabel["calendar"]
        return ([row for row in relabel["eps_estimates"] if not row.get("duplicate")],
                {"last_reported_period_end": calendar.get("last_reported_period_end"),
                 "fiscal_year_end_month": calendar.get("fiscal_year_end_month"),
                 "basis": "relabelled"})
    stored = consensus.get("fiscal_calendar") or {}
    month = None
    fye = str(stored.get("fiscal_year_end") or "")
    if len(fye) >= 2 and fye[:2].isdigit():
        month = int(fye[:2])
    return (list(consensus.get("eps_estimates") or ()),
            {"last_reported_period_end": stored.get("last_reported_period_end"),
             "fiscal_year_end_month": month, "basis": "stored"})


def _window_eps_growth(consensus: Mapping[str, Any],
                       relabel: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Consensus EPS over the 12 months the target covers, against the same months a year earlier.

    The next four fiscal quarters after the last reported one
    (``financial_period.ntm_aligned``: whole fiscal years from the annual
    estimate, partial years from quarterly estimates or a quarter-share of the
    annual one) over the same construction on the vendor's year-ago figures --
    one basis (the vendor's, usually adjusted) and one window, the one the
    target price is for.  None when either side cannot be built.
    """

    from .financial_period import ntm_aligned

    rows, calendar = _relabelled_eps(consensus, relabel)
    last, month = calendar.get("last_reported_period_end"), calendar.get("fiscal_year_end_month")
    if not last or not month:
        return None
    quarterly, annual, quarterly_ago, annual_ago = {}, {}, {}, {}
    for row in rows:
        if row.get("reported") or not row.get("label"):
            continue
        target = quarterly if row.get("period_kind") == "quarter" else annual
        target_ago = quarterly_ago if row.get("period_kind") == "quarter" else annual_ago
        if _dec(row.get("avg")) is not None:
            target[str(row["label"])] = row["avg"]
        if _dec(row.get("year_ago")) is not None:
            target_ago[str(row["label"])] = row["year_ago"]
    ahead = ntm_aligned(str(last), int(month), quarterly=quarterly, annual=annual)
    behind = ntm_aligned(str(last), int(month), quarterly=quarterly_ago, annual=annual_ago)
    if ahead.get("status") != "available" or behind.get("status") != "available":
        return None
    shape = [(item["part"], item["source"]) for item in ahead["components"]]
    if shape != [(item["part"], item["source"]) for item in behind["components"]]:
        return None  # the two sides would not be the same construction
    ntm_value, ltm_value = _dec(ahead["value"]), _dec(behind["value"])
    if ntm_value is None or ltm_value is None or ltm_value <= 0:
        return None
    return {"ntm": ntm_value, "ltm": ltm_value, "window": list(ahead["window"]),
            "components": ahead["components"], "calendar_basis": calendar["basis"],
            "last_reported_period_end": str(last)}


def _target_leg(kind: str, series: Mapping[str, Any], snapshot: Mapping[str, Any] | None,
                consensus: Mapping[str, Any] | None, *, target: Decimal | None,
                target_source: str, target_refs: Sequence[str],
                relabel: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if snapshot is None:
        return _unavailable(kind, "还没有估值快照")
    if consensus is None:
        return _unavailable(kind, "还没有一致预期")
    if target is None or target <= 0:
        return _unavailable(kind, "没有可用的目标价均值")
    # 2026-10-09 (batch B follow-up): the EPS growth is taken over the same
    # 12 months as the target -- the next four fiscal quarters on batch B's
    # relabelled periods -- and only falls back to the next fiscal year,
    # labelled, when that window cannot be built.
    window = _window_eps_growth(consensus, relabel)
    row = None
    if window is None:
        row = _consensus_row(consensus)
        if row is None:
            return _unavailable(kind, "一致预期里既凑不出未来 12 个月的 EPS，也没有同时给出均值与上年数的下一财年 EPS")
        eps_now, eps_then = Decimal(row["avg"]), Decimal(row["year_ago"])
    else:
        eps_now, eps_then = window["ntm"], window["ltm"]
    p_now = Decimal(snapshot["price"]["close"])
    growth_factor = eps_now / eps_then
    if growth_factor <= 0:
        return _unavailable(kind, "一致预期 EPS 由负转正或相反，增长率没有意义")
    multiple_factor = (target / p_now) / growth_factor
    point = _latest_point(series)
    dps = _dec(((point or {}).get("dps") or {}).get("value"))
    dividend_yield = None if dps is None else dps / p_now
    out = decompose({"earnings_growth": growth_factor, "share_count": None,
                     "multiple_change": multiple_factor}, dividend_yield)
    start = snapshot["price"]["bar_date"]
    end = (_date(start) + timedelta(days=TARGET_WINDOW_DAYS)).isoformat()
    target_text = _pct(target / p_now - 1)
    yield_text = _pct(dividend_yield)
    if window is not None:
        span = f"{window['window'][0]}–{window['window'][-1]}"
        growth_note = (f"EPS 增长取一致预期未来四个财季（{span}，最近已报告财期截至 "
                       f"{window['last_reported_period_end']}）对其上年同期四个财季，与目标价的 12 个月窗口一致")
        eps_start = {"value": _text(eps_then), "end": window["last_reported_period_end"],
                     "rule": "consensus_year_ago_of_next_four_quarters", "window": window["window"]}
        eps_end = {"value": _text(eps_now), "end": None, "rule": "consensus_next_four_quarters",
                   "window": window["window"], "components": window["components"]}
        eps_row = span
    else:
        growth_note = (f"EPS 增长取一致预期 {row.get('label')} 对上年（{row.get('period_end')} 财年）；"
                       "未能凑出未来四个财季，财年与 12 个月窗口未完全对齐")
        eps_start = {"value": row["year_ago"], "end": None, "rule": "consensus_year_ago"}
        eps_end = {"value": row["avg"], "end": row.get("period_end"), "rule": row.get("label")}
        eps_row = row.get("label")
    return {
        "leg": kind, "label": LEG_LABELS[kind], "status": "available",
        "window": {"start": start, "end": end},
        "target_implied_percent": target_text, "dividend_yield_percent": yield_text,
        "price_start": _text(p_now), "price_target": _text(target),
        "eps_start": eps_start,
        "eps_end": eps_end,
        "eps_window_basis": "next_four_fiscal_quarters" if window is not None else "next_fiscal_year",
        "pe_start": _text(p_now / eps_then, "0.01") if eps_then > 0 else None,
        "pe_end": _text(target / eps_now, "0.01") if eps_now > 0 else None,
        "dps_trailing": _text(dps), **out,
        "notes": [f"总回报 = 目标价隐含 {target_text}% + 股息 {yield_text or '0'}%（12 个月，自 {start} 起）；"
                  "盈利增长只是对目标价隐含回报的拆分，估值倍数变化为残差",
                  growth_note,
                  "一致预期 EPS 多为调整后口径，与申报 GAAP EPS 不同；增长率在同一口径内计算",
                  "股数变化已包含在一致预期 EPS 增长里，不单独拆出"],
        "inputs": {"consensus_version_ref": consensus.get("id"),
                   "consensus_hash": consensus.get("content_hash"),
                   "eps_row": eps_row, "target_refs": list(target_refs)[:12],
                   "consensus_periods": ("relabelled" if window is not None
                                         and window["calendar_basis"] == "relabelled" else "stored"),
                   "valuation_snapshot_ref": snapshot["id"]},
        "expectation_source_kind": target_source,
        "expectation_sources": {"earnings": "consensus", "multiple": target_source},
    }


def build_return_bridge(company_ref: str, *, series: Mapping[str, Any],
                        series_ref: str | None, snapshot: Mapping[str, Any] | None,
                        price: Mapping[str, Any] | None, model: Mapping[str, Any] | None,
                        consensus: Mapping[str, Any] | None,
                        consensus_relabel: Mapping[str, Any] | None = None,
                        dividend_payments: Mapping[str, Any] | None = None) -> dict[str, Any]:
    legs = [_realised_leg(series, snapshot, price, dividend_payments), _driver_leg(series, snapshot, model)]
    vendor_target = _dec(((consensus or {}).get("price_target") or {}).get("mean"))
    legs.append(_target_leg("expected_consensus", series, snapshot, consensus,
                            target=vendor_target, target_source="consensus",
                            target_refs=[(consensus or {}).get("id") or ""],
                            relabel=consensus_relabel))
    report = (consensus or {}).get("report_consensus") or {}
    legs.append(_target_leg("expected_sell_side", series, snapshot, consensus,
                            target=_dec(report.get("mean")), target_source="sell_side_note",
                            target_refs=list(report.get("estimate_refs") or []),
                            relabel=consensus_relabel))
    # 2026-10-09: a return is never shown next to a loss-making model
    # without the contradiction said beside it.
    consistency = model_profit_check(model)
    if consistency["status"] == "loss_making":
        for leg in legs:
            if leg.get("status") == "available" and str(leg.get("leg")).startswith("expected"):
                leg["contradiction"] = consistency["notice"]
                leg["notes"] = list(leg.get("notes") or []) + [consistency["notice"]]
    return {
        "schema_version": SCHEMA_VERSION, "kind": KIND, "object": "HoldingPeriodReturnBridge",
        "formula_ref": RETURN_FORMULA_REF, "company_ref": company_ref,
        "bridge_ref": return_bridge_ref(company_ref),
        "as_of": None if snapshot is None else snapshot["price"]["bar_date"],
        "model_consistency": consistency,
        "legs": legs,
        "inputs": {
            "per_share_series_version_ref": series_ref,
            "valuation_snapshot_ref": None if snapshot is None else snapshot.get("id"),
            "valuation_snapshot_hash": None if snapshot is None else snapshot.get("content_hash"),
            "trailing_pe": None if snapshot is None else _trailing_pe(snapshot),
            "price_version_ref": None if price is None else price.get("id"),
            "forecast_model_version_ref": None if model is None else model.get("id"),
            "consensus_version_ref": None if consensus is None else consensus.get("id"),
        },
        "formula": "总回报 = 盈利增长 × 股数变化 × 倍数变化 − 1 + 股息率；贡献按对数权重分摊价格回报",
    }


# ---------------------------------------------------------------------------
# expectation sources
# ---------------------------------------------------------------------------

#: Conviction calls already name their market-view sources (P15d's closed
#: list); this is how each maps onto the buy side's vocabulary.
CONVICTION_SOURCE_KIND: Mapping[str, str | None] = {
    "consensus": "consensus", "sell_side_rating": "sell_side_note", "sales_note": "sell_side_note",
    "management_framing": "management_guidance", "debate_market_position": None,
    "crowd_narrative": None,
}
CLAIM_PREFIX_KIND: Mapping[str, str] = {
    "claim:transcript:": "management_guidance", "claim:alphaengine:": "sell_side_note",
    "claim:sales_notes:": "sell_side_note",
}
REF_PREFIX_KIND: Mapping[str, str] = {
    "consensus-estimate-version:": "consensus", "street-estimate:": "sell_side_note",
    "market-price-series-version:": "market_implied",
    "valuation-snapshot-version:": "market_implied",
}


def classify_ref(connection: sqlite3.Connection, ref: str) -> tuple[str | None, str]:
    for prefix, kind in REF_PREFIX_KIND.items():
        if ref.startswith(prefix):
            return kind, f"ref_prefix:{prefix}"
    if ref.startswith("claim-version:") and table_exists(connection, "claim_versions"):
        row = connection.execute("SELECT claim_ref FROM claim_versions WHERE claim_version_id=?",
                                 (ref,)).fetchone()
        claim_ref = None if row is None else row[0]
        for prefix, kind in CLAIM_PREFIX_KIND.items():
            if claim_ref and claim_ref.startswith(prefix):
                return kind, f"claim_source:{prefix}"
        return None, f"claim_source:{(claim_ref or 'unknown').split(':')[1] if claim_ref and ':' in claim_ref else 'unknown'}"
    if ref.startswith("company-scenario-version:") and table_exists(connection, "company_scenario_versions"):
        return "owner_view", "owner_adopted_scenario"
    return None, "unclassified"


def _primary(kinds: Sequence[str]) -> str | None:
    if not kinds:
        return None
    counts = {kind: kinds.count(kind) for kind in EXPECTATION_SOURCE_KINDS if kind in kinds}
    return sorted(counts, key=lambda kind: (-counts[kind], EXPECTATION_SOURCE_KINDS.index(kind)))[0]


def _tag(subject_kind: str, subject: Mapping[str, Any], field: str, company_ref: str,
         entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    kinds = [entry["kind"] for entry in entries if entry["kind"] is not None]
    return {"subject_kind": subject_kind, "subject_ref": subject["id"],
            "subject_hash": subject["content_hash"], "field": field, "company_ref": company_ref,
            "classifier_ref": CLASSIFIER_REF, "kinds": sorted(set(kinds)),
            "primary_kind": _primary(kinds), "entries": list(entries),
            "unclassified": [entry["ref"] for entry in entries if entry["kind"] is None]}


def consensus_tags(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    company_ref = record["company_ref"]
    vendor = {"ref": record["id"], "kind": "consensus", "basis": f"vendor:{record.get('source_ref')}"}
    tags = [_tag("consensus_estimate", record, field, company_ref, [vendor])
            for field in ("eps_estimates", "revenue_estimates", "price_target", "recommendations")
            if record.get(field)]
    report = record.get("report_consensus")
    if report:
        refs = list(report.get("estimate_refs") or [])[:20]
        tags.append(_tag("consensus_estimate", record, "report_consensus", company_ref,
                         [{"ref": ref, "kind": "sell_side_note", "basis": "street_estimate"}
                          for ref in refs] or [{"ref": record["id"], "kind": "sell_side_note",
                                                "basis": "report_consensus"}]))
    return tags


def debate_tags(connection: sqlite3.Connection, record: Mapping[str, Any]) -> list[dict[str, Any]]:
    tags = []
    for debate in record.get("debates") or ():
        market = debate.get("market_position") or {}
        if not market.get("available"):
            continue
        entries = []
        for ref in market.get("refs") or ():
            kind, basis = classify_ref(connection, ref)
            entries.append({"ref": ref, "kind": kind, "basis": basis})
        tags.append(_tag("debate_map", record, f"debates[{debate.get('debate_ref')}].market_position",
                         record["subject_ref"], entries))
    return tags


def conviction_tags(connection: sqlite3.Connection, record: Mapping[str, Any]) -> list[dict[str, Any]]:
    market = ((record.get("market_view_alignment") or {}).get("market_view")
              or (record.get("variant_view") or {}).get("market_view") or {})
    tags = []
    if market.get("available"):
        entries = [{"ref": f"market_view_source:{source}", "kind": CONVICTION_SOURCE_KIND.get(source),
                    "basis": "conviction_market_view_source"} for source in market.get("sources") or ()]
        for ref in market.get("refs") or ():
            kind, basis = classify_ref(connection, ref)
            entries.append({"ref": ref, "kind": kind, "basis": basis})
        tags.append(_tag("conviction_call", record, "market_view", record["company_ref"], entries))
    held = [item.get("source_ref") for item in record.get("scenarios") or () if item.get("source_ref")]
    if held:
        tags.append(_tag("conviction_call", record, "scenarios", record["company_ref"],
                         [{"ref": ref, "kind": "owner_view", "basis": "owner_adopted_scenario"}
                          for ref in held]))
    return tags


# ---------------------------------------------------------------------------
# reading the inputs
# ---------------------------------------------------------------------------


def _latest_json(connection: sqlite3.Connection, table: str, where: str, args: tuple,
                 order: str) -> dict[str, Any] | None:
    if not table_exists(connection, table):
        return None
    row = connection.execute(f"SELECT record_json FROM {table} WHERE {where} ORDER BY {order} LIMIT 1",
                             args).fetchone()
    return None if row is None else json.loads(row[0])


def read_inputs(connection: sqlite3.Connection, company_ref: str) -> dict[str, Any]:
    _row_factory(connection)
    snapshot = _latest_json(connection, "valuation_snapshot_versions", "company_ref=?",
                            (company_ref,), "version_number DESC")
    price = None
    if snapshot is not None:
        price = _latest_json(connection, "market_price_series_versions", "version_id=?",
                             (snapshot["price"]["version_ref"],), "version_number DESC")
    model = _latest_json(connection, "forecast_model_versions", "company_ref=?", (company_ref,),
                         "created_at DESC, version_number DESC")
    consensus = _latest_json(connection, "consensus_estimate_versions", "company_ref=?",
                             (company_ref,), "version_number DESC")
    # Batch B's relabelled periods (read-only); the stored record is kept as
    # is for the tags and the input refs.
    relabel = None
    if consensus is not None:
        from .forecast_assumption_sources import relabelled_consensus

        held = relabelled_consensus(connection, company_ref, consensus)
        relabel = None if held is None else held["relabel"]
    dividends = _latest_json(connection, "dividend_payment_evidence_versions", "company_ref=?",
                             (company_ref,), "created_at DESC, version_id DESC")
    if dividends is not None:
        asserted = dividends.pop("content_hash", None)
        if asserted != content_hash(dividends):
            raise ReturnAttributionConflict("dividend payment evidence hash drifted")
    return {"dividend_payments": dividends, "snapshot": snapshot, "price": price, "model": model, "consensus": consensus,
            "consensus_relabel": relabel}


# ---------------------------------------------------------------------------
# the authority
# ---------------------------------------------------------------------------


def _assert_actor(actor_ref: str, mission: Mapping[str, Any] | None) -> None:
    if actor_ref.startswith("automation:"):
        if mission is None or actor_ref != mission["autonomy"]["automation_principal"]:
            raise ReturnAttributionConflict("automation actor is not the mission principal")
        if WRITE_SCOPE not in mission["autonomy"]["may_write"]:
            raise ReturnAttributionConflict(f"mission does not grant {WRITE_SCOPE} writes")
    elif not actor_ref.startswith("human:"):
        raise ReturnAttributionConflict("actor_ref must be a human: or automation: principal")


_VERSIONED = {
    "income_bridge_versions": ("bridge_ref", "income-bridge-version:"),
    "per_share_series_versions": ("series_ref", "per-share-series-version:"),
    "holding_period_return_bridges": ("bridge_ref", "holding-period-return-version:"),
}


def return_bridge_bindings(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ``upstream_bindings`` of a return bridge: model and consensus versions it read."""

    from .product_consistency import upstream_bindings

    inputs = body.get("inputs") or {}
    refs: list[Any] = [{"ref": inputs.get("forecast_model_version_ref"), "role": "model"},
                       {"ref": inputs.get("consensus_version_ref"), "role": "consensus"}]
    for leg in body.get("legs") or ():
        leg_inputs = (leg or {}).get("inputs") or {}
        if isinstance(leg_inputs, Mapping):
            refs.append({"ref": leg_inputs.get("forecast_model_version_ref"), "role": "model"})
    return upstream_bindings(refs)


class ReturnAttributionAuthority:
    """Append-only versions of the four objects.  Writes only through a DaltonStore."""

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("ReturnAttributionAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        _row_factory(self.connection)
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def record_dividend_evidence(self, evidence: Mapping[str, Any], *,
                                 mission: Mapping[str, Any] | None, actor_ref: str,
                                 state_dir: str | Path) -> dict[str, Any]:
        _assert_actor(actor_ref, mission)
        body = validate_dividend_evidence(evidence)
        if mission is not None and body["company_ref"] not in {row["company_ref"] for row in mission.get("universe", [])}:
            raise ReturnAttributionConflict("dividend company is outside the mission")
        from .acquired_sec_filings import _spool_bytes
        if body["source_ref"] != "raw-sink:" + body["source_hash"]:
            raise ReturnAttributionConflict("dividend source must bind a held raw artifact")
        raw, problem = _spool_bytes(Path(state_dir), body["source_hash"])
        if raw is None:
            raise ReturnAttributionConflict("dividend held source cannot be verified: " + str(problem))
        try:
            source = json.loads(raw)
        except ValueError as exc:
            raise ReturnAttributionConflict("dividend source is not typed payment evidence") from exc
        expected_source = {key: value for key, value in body.items() if key not in {"source_ref", "source_hash"}}
        if source != expected_source:
            raise ReturnAttributionConflict("dividend evidence differs from its exact held source")
        digest = content_hash(body)
        version_id = "dividend-payment-evidence:" + digest
        wire = {**body, "content_hash": digest}
        with self.store._transaction() as cur:
            cur.execute("INSERT OR IGNORE INTO dividend_payment_evidence_versions "
                        "(version_id, company_ref, record_json, content_hash, actor_ref, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (version_id, body["company_ref"], canonical_json(wire),
                        digest, actor_ref, datetime.now(timezone.utc).isoformat()))
        return {"id": version_id, "content_hash": digest}

    def _publish(self, table: str, body: Mapping[str, Any], columns: Mapping[str, Any], *,
                 mission: Mapping[str, Any] | None, actor_ref: str) -> dict[str, Any]:
        ref_column, prefix = _VERSIONED[table]
        ref = body[ref_column]
        body_hash = content_hash(dict(body))
        with self.store._transaction() as cur:
            row = cur.execute(
                f"SELECT version_id, version_number, body_hash FROM {table} WHERE {ref_column}=? "
                "ORDER BY version_number DESC LIMIT 1", (ref,)).fetchone()
            if row is not None and row["body_hash"] == body_hash:
                return {"status": "unchanged", "id": row["version_id"],
                        "version": row["version_number"]}
            number = 1 if row is None else int(row["version_number"]) + 1
            created_at = _now()
            record = _record({
                **body, "id": prefix + content_hash({"ref": ref, "version": number})[:32],
                "version": number, "prior_version_ref": None if row is None else row["version_id"],
                # Batch an (binding only, outside body_hash): the exact model
                # and consensus versions the bridge was computed from.
                **({"upstream_bindings": return_bridge_bindings(body)}
                   if table == "holding_period_return_bridges" else {}),
                "body_hash": body_hash, "actor_ref": actor_ref,
                "mission_version_ref": None if mission is None else mission.get("id"),
                "created_at": created_at,
            })
            names = ["version_id", ref_column, "version_number", "prior_version_id", "company_ref",
                     *columns.keys(), "body_hash", "record_json", "content_hash", "actor_ref",
                     "created_at"]
            values = [record["id"], ref, number, record["prior_version_ref"], body["company_ref"],
                      *columns.values(), body_hash, canonical_json(record), record["content_hash"],
                      actor_ref, created_at]
            cur.execute(f"INSERT INTO {table}({','.join(names)}) VALUES({','.join('?' * len(names))})",
                        values)
        return {"status": "published", "id": record["id"], "version": number, "record": record}

    def publish_income_bridge(self, body: Mapping[str, Any], *, mission: Mapping[str, Any] | None,
                              actor_ref: str) -> dict[str, Any]:
        _assert_actor(actor_ref, mission)
        return self._publish("income_bridge_versions", body, {
            "period_start": body["period"]["start"], "period_end": body["period"]["end"],
            "period_semantics": body["period"]["semantics"], "status": body["status"]},
            mission=mission, actor_ref=actor_ref)

    def publish_per_share_series(self, body: Mapping[str, Any], *,
                                 mission: Mapping[str, Any] | None, actor_ref: str) -> dict[str, Any]:
        _assert_actor(actor_ref, mission)
        return self._publish("per_share_series_versions", body, {
            "period_count": len(body["periods"]), "missing_count": len(body["missing"])},
            mission=mission, actor_ref=actor_ref)

    def publish_return_bridge(self, body: Mapping[str, Any], *, mission: Mapping[str, Any] | None,
                              actor_ref: str) -> dict[str, Any]:
        _assert_actor(actor_ref, mission)
        return self._publish("holding_period_return_bridges", body, {
            "as_of": body["as_of"] or "",
            "available_leg_count": sum(1 for leg in body["legs"] if leg["status"] == "available")},
            mission=mission, actor_ref=actor_ref)

    def record_tag(self, tag: Mapping[str, Any], *, mission: Mapping[str, Any] | None,
                   actor_ref: str) -> str:
        _assert_actor(actor_ref, mission)
        tag_id = "expectation-source-tag:" + content_hash({
            "subject": tag["subject_ref"], "field": tag["field"],
            "classifier": tag["classifier_ref"]})[:32]
        with self.store._transaction() as cur:
            if cur.execute("SELECT 1 FROM expectation_source_tags WHERE tag_id=?",
                           (tag_id,)).fetchone() is not None:
                return "unchanged"
            created_at = _now()
            record = _record({**tag, "schema_version": SCHEMA_VERSION, "kind": KIND,
                              "object": "ExpectationSourceTag", "id": tag_id,
                              "actor_ref": actor_ref, "created_at": created_at})
            cur.execute(
                "INSERT INTO expectation_source_tags(tag_id,subject_kind,subject_ref,subject_hash,"
                "field,company_ref,classifier_ref,primary_kind,record_json,content_hash,actor_ref,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (tag_id, tag["subject_kind"], tag["subject_ref"], tag["subject_hash"], tag["field"],
                 tag["company_ref"], tag["classifier_ref"], tag["primary_kind"],
                 canonical_json(record), record["content_hash"], actor_ref, created_at))
        return "recorded"


def supplement_per_share_facts(collected: Mapping[str, Any], facts: Mapping[str, Any] | None) -> dict[str, Any]:
    """Missing face-statement rows from exact SEC companyfacts accession/vintage.

    A consolidated companyfact cannot identify a share class. It is never
    substituted for an existing face-statement fact or a later restatement.
    """
    if facts is None:
        return dict(collected)
    digest = str(facts.get("raw_hash") or "")
    if len(digest) != 64:
        raise ReturnAttributionConflict("supplementary companyfacts source hash is missing")
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    concepts = {concept for values in [*PER_SHARE_CONCEPTS.values(), ANCHOR_CONCEPTS["net_income_to_common"]]
                for concept in values}
    periods = {}
    for key, held in collected["periods"].items():
        period = {**held, "lines": dict(held["lines"])}
        for concept in sorted(concepts):
            if ("income", concept) in period["lines"]:
                continue
            units = ((gaap.get(concept.split(":", 1)[1]) or {}).get("units") or {})
            candidates = [(unit, row) for unit, rows in units.items() for row in rows
                          if row.get("start") == key[0] and row.get("end") == key[1]
                          and row.get("accn") == period["filing"]["accession"]
                          and row.get("filed") == period["filing"]["filed"] and _dec(row.get("val")) is not None]
            if len({(unit, str(row["val"])) for unit, row in candidates}) != 1:
                continue
            unit, raw = candidates[0]
            normalized_unit = "usdPerShare" if unit == "USD/shares" else "usd" if unit == "USD" else unit
            identity = {"source_hash": digest, "concept": concept, "fact": raw}
            ref = "sec-companyfact:" + content_hash(identity)
            period["lines"][("income", concept)] = {"line_id": ref, "authority_ref": ref,
                "statement": "income", "ordinal": 1000000 + len(period["lines"]), "concept": concept,
                "label": concept.split(":", 1)[1], "parent_concept": None, "balance": None,
                "period_start": key[0], "period_end": key[1], "value": str(raw["val"]), "unit": normalized_unit,
                "source_evidence": {"raw_ref": "raw-sink:" + digest, "fact": raw, "content_hash": content_hash(identity)}}
        periods[key] = period
    return {**collected, "periods": periods, "supplementary_source_ref": "raw-sink:" + digest}


def compute_company(connection: sqlite3.Connection, company_ref: str, *,
                    state_dir: str | Path | None = None,
                    facts_authority: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Every body for one company, without writing.  Pure over the Core's rows."""

    if facts_authority is None and state_dir is not None:
        from .valuation_history import company_facts
        facts_authority = company_facts(connection, state_dir, company_ref)
    collected = supplement_per_share_facts(collect_periods(connection, company_ref), facts_authority)
    bridges = build_income_bridges(collected, company_ref)
    series = build_per_share_series(company_ref, collected, bridges)
    vintages = []
    for filed in sorted({item["filed"] for item in _filings(connection, company_ref)}):
        prior = supplement_per_share_facts(collect_periods(connection, company_ref, as_of=filed), facts_authority)
        prior_series = build_per_share_series(company_ref, prior, build_income_bridges(prior, company_ref))
        vintages.extend(prior_series["ttm"])
    if facts_authority is not None:
        from .valuation_history import eps_points, ttm_eps
        digest = facts_authority["raw_hash"]
        for raw in ttm_eps(eps_points(facts_authority["facts"])):
            ref = "sec-companyfacts-ttm:" + content_hash({"raw_hash": digest, "point": {
                key: str(value) if isinstance(value, Decimal) else value for key, value in raw.items()}})
            vintages.append({"end": raw["period_end"], "available_on": raw["effective"],
                "rule": "filed_annual_eps" if raw["basis"] == "filed_annual_eps" else TTM_RULE_YTD,
                "diluted_eps": {"value": _text(raw["value"]), "basis": raw["basis"], "refs": [ref]},
                "diluted_shares": raw.get("diluted_shares"), "net_income_to_common": raw.get("common_income"),
                "source_evidence": {"raw_ref": "raw-sink:" + digest, "source_accessions": raw["source_accessions"],
                                    "content_hash": ref.split(":", 1)[1]}, "source_kind": "held_companyfacts"})
    series["ttm_vintages"] = list({canonical_json(point): point for point in vintages}.values())
    from .statement_context import statement_rows
    classes = []
    for filing in _filings(connection, company_ref):
        for line in statement_rows(connection, filing["ingest_id"]):
            dimensions = line.get("dimensions") or {}
            stock = [(axis, member) for axis, member in dimensions.items() if axis.rsplit(":", 1)[-1] in {"StockClassAxis", "StatementClassOfStockAxis", "ClassesOfShareCapitalAxis"}]
            if len(stock) != 1 or len(dimensions) != 1:
                continue
            if line.get("concept") not in {concept for concepts in PER_SHARE_CONCEPTS.values() for concept in concepts}:
                continue
            classes.append({"share_class_ref": stock[0][1], "axis": stock[0][0], "concept": line["concept"],
                            "start": line["period_start"], "end": line["period_end"], "value": line["value"],
                            "unit": line["unit"], "ref": line.get("authority_ref") or statement_line_ref(line["line_id"]),
                            "statement_context_ref": line.get("statement_context_ref"), "available_on": filing["filed"],
                            "accession": filing["accession"]})
    if collected.get("supplementary_source_ref"):
        series["supplementary_source_ref"] = collected["supplementary_source_ref"]
    series["share_class_periods"] = classes
    series["share_class_rule"] = "exact StockClassAxis/member; classes remain separate; no value-based mapping"
    inputs = read_inputs(connection, company_ref)
    return {"collected": collected, "bridges": bridges, "series": series, "inputs": inputs}


def refresh_company(store: Any, mission: Mapping[str, Any], company_ref: str) -> dict[str, Any]:
    """Recompute and append whatever changed for one company."""

    actor = mission["autonomy"]["automation_principal"]
    connection = store.connection
    computed = compute_company(connection, company_ref, state_dir=Path(store.path).parent if store.path != ":memory:" else None)
    counts = {"bridges": 0, "bridges_published": 0, "series_published": 0,
              "return_published": 0, "tags_recorded": 0}
    inputs = computed["inputs"]
    if not computed["bridges"] and inputs["snapshot"] is None:
        # Nothing filed and nothing priced: an empty object is not a fact.
        return {**counts, "complete": 0, "residual": 0, "missing_rows": 0,
                "per_share_missing": 0, "legs_available": 0, "no_inputs": 1}
    authority = ReturnAttributionAuthority(store)
    for body in computed["bridges"]:
        counts["bridges"] += 1
        if authority.publish_income_bridge(body, mission=mission, actor_ref=actor)["status"] == "published":
            counts["bridges_published"] += 1
    series_out = authority.publish_per_share_series(computed["series"], mission=mission, actor_ref=actor)
    counts["series_published"] += series_out["status"] == "published"
    body = build_return_bridge(company_ref, series=computed["series"], series_ref=series_out["id"],
                               snapshot=inputs["snapshot"], price=inputs["price"],
                               model=inputs["model"], consensus=inputs["consensus"],
                               consensus_relabel=inputs.get("consensus_relabel"),
                               dividend_payments=inputs.get("dividend_payments"))
    returned = authority.publish_return_bridge(body, mission=mission, actor_ref=actor)
    counts["return_published"] += returned["status"] == "published"
    tags: list[dict[str, Any]] = []
    if inputs["consensus"] is not None:
        tags.extend(consensus_tags(inputs["consensus"]))
    if table_exists(connection, "debate_map_versions"):
        for row in connection.execute(
                "SELECT record_json FROM debate_map_versions d WHERE subject_ref=? AND version_number="
                "(SELECT MAX(version_number) FROM debate_map_versions WHERE map_ref=d.map_ref)",
                (company_ref,)):
            tags.extend(debate_tags(connection, json.loads(row[0])))
    if table_exists(connection, "conviction_call_proposals"):
        for row in connection.execute(
                "SELECT record_json FROM conviction_call_proposals WHERE company_ref=?", (company_ref,)):
            tags.extend(conviction_tags(connection, json.loads(row[0])))
    for tag in tags:
        if authority.record_tag(tag, mission=mission, actor_ref=actor) == "recorded":
            counts["tags_recorded"] += 1
    statuses = [item["status"] for item in computed["bridges"]]
    return {**counts, "complete": statuses.count("complete"), "residual": statuses.count("residual"),
            "missing_rows": statuses.count("missing_rows"),
            "per_share_missing": len(computed["series"]["missing"]),
            "legs_available": sum(1 for leg in body["legs"] if leg["status"] == "available"),
            "no_inputs": 0}


def refresh_industry_tags(store: Any, mission: Mapping[str, Any]) -> int:
    """Debate maps about the industry carry market positions too."""

    connection = store.connection
    industry = mission.get("industry_ref")
    if not industry or not table_exists(connection, "debate_map_versions"):
        return 0
    authority = ReturnAttributionAuthority(store)
    actor = mission["autonomy"]["automation_principal"]
    recorded = 0
    for row in connection.execute(
            "SELECT record_json FROM debate_map_versions d WHERE subject_ref=? AND version_number="
            "(SELECT MAX(version_number) FROM debate_map_versions WHERE map_ref=d.map_ref)",
            (industry,)):
        for tag in debate_tags(connection, json.loads(row[0])):
            recorded += authority.record_tag(tag, mission=mission, actor_ref=actor) == "recorded"
    return recorded


# ---------------------------------------------------------------------------
# read views (cockpit)
# ---------------------------------------------------------------------------


def latest_return_bridge(connection: sqlite3.Connection, company_ref: str,
                         before: str | None = None) -> dict[str, Any] | None:
    if not table_exists(connection, "holding_period_return_bridges"):
        return None
    _row_factory(connection)
    sql = ("SELECT record_json, content_hash FROM holding_period_return_bridges WHERE company_ref=?"
           + (" AND created_at<=?" if before else "") + " ORDER BY version_number DESC LIMIT 1")
    row = connection.execute(sql, (company_ref, before) if before else (company_ref,)).fetchone()
    return None if row is None else _decode(row, "HoldingPeriodReturnBridge")


def latest_series(connection: sqlite3.Connection, company_ref: str) -> dict[str, Any] | None:
    if not table_exists(connection, "per_share_series_versions"):
        return None
    _row_factory(connection)
    row = connection.execute(
        "SELECT record_json, content_hash FROM per_share_series_versions WHERE company_ref=? "
        "ORDER BY version_number DESC LIMIT 1", (company_ref,)).fetchone()
    return None if row is None else _decode(row, "PerShareSeriesVersion")


def latest_bridges(connection: sqlite3.Connection, company_ref: str) -> list[dict[str, Any]]:
    if not table_exists(connection, "income_bridge_versions"):
        return []
    _row_factory(connection)
    rows = connection.execute(
        "SELECT record_json, content_hash FROM income_bridge_versions b WHERE company_ref=? AND "
        "version_number=(SELECT MAX(version_number) FROM income_bridge_versions WHERE bridge_ref=b.bridge_ref) "
        "ORDER BY period_end, period_start", (company_ref,)).fetchall()
    return [_decode(row, "IncomeBridgeVersion") for row in rows]


def company_tags(connection: sqlite3.Connection, company_refs: Sequence[str]) -> list[dict[str, Any]]:
    if not table_exists(connection, "expectation_source_tags"):
        return []
    _row_factory(connection)
    marks = ",".join("?" * len(company_refs))
    return [json.loads(row[0]) for row in connection.execute(
        f"SELECT record_json FROM expectation_source_tags WHERE company_ref IN ({marks}) "
        "ORDER BY created_at", tuple(company_refs))]


#: One unit per bridge table, chosen by its largest amount, so a column of
#: figures reads in one scale: 亿美元 from 1 亿 up, else 万美元, else 美元.
AMOUNT_SCALES = ((Decimal("100000000"), "亿美元"), (Decimal("10000"), "万美元"),
                 (Decimal("1"), "美元"))
SHARE_SCALES = ((Decimal("100000000"), "亿股"), (Decimal("10000"), "万股"))


def amount_scale(values: Sequence[Any]) -> tuple[Decimal, str]:
    """The divisor and unit a table of dollar amounts is shown in."""

    numbers = [abs(number) for number in (_dec(value) for value in values) if number is not None]
    largest = max(numbers, default=Decimal(0))
    for divisor, unit in AMOUNT_SCALES:
        if largest >= divisor:
            return divisor, unit
    return Decimal(1), "美元"


def amount_in(value: Any, divisor: Decimal) -> str | None:
    """``69672977000`` at 1 亿 -> ``696.73``: thousands separators, two places."""

    number = _dec(value)
    if number is None:
        return None if value in (None, "") else str(value)
    return f"{(number / divisor).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN):,}"


def share_count_text(value: Any) -> str | None:
    """``626824946`` -> ``6.27 亿股``; small counts keep separators."""

    number = _dec(value)
    if number is None:
        return None if value in (None, "") else str(value)
    for divisor, unit in SHARE_SCALES:
        if abs(number) >= divisor:
            return f"{(number / divisor).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN):,} {unit}"
    return f"{number:,} 股"


def _bridge_view(bridge: Mapping[str, Any]) -> dict[str, Any]:
    rows = []
    for line in bridge["lines"]:
        rows.append({"label": LINE_CLASS_LABELS.get(line["classification"], line["classification"]),
                     "concept": line["concept"], "value": line["signed_value"] if line["step"] else line["value"],
                     "is_anchor": line["step"] is None, "ref": line["ref"]})
    divisor, unit = amount_scale([row["value"] for row in rows])
    for row in rows:
        row["display_value"] = amount_in(row["value"], divisor)
    steps = []
    for step in bridge["steps"]:
        steps.append({"step": step["step"], "step_label": STEP_LABELS[step["step"]],
                      "status": step["status"],
                      "status_label": {"complete": "勾稽一致", "residual": "有差额",
                                       "missing_anchor": "缺报表行"}[step["status"]],
                      "residual": step.get("residual"),
                      "residual_label": (None if step.get("residual") is None
                                         else f"{amount_in(step['residual'], divisor)} {unit}"),
                      "missing_labels": [LINE_CLASS_LABELS.get(item, item) for item in step["missing"]]})
    return {"ref": bridge["id"], "fiscal_label": bridge["period"]["fiscal_label"],
            "semantics_label": SEMANTICS_LABELS[bridge["period"]["semantics"]],
            "period": f"{bridge['period']['start']}..{bridge['period']['end']}",
            "accession": bridge["source"]["accession"], "status": bridge["status"],
            "status_label": STATUS_LABELS[bridge["status"]], "rows": rows, "steps": steps,
            "amount_unit": unit,
            "missing_labels": [LINE_CLASS_LABELS.get(item, item) for item in bridge["missing_rows"]],
            "net_income_to_common": bridge["net_income_to_common"]}


def _leg_view(leg: Mapping[str, Any]) -> dict[str, Any]:
    if leg["status"] != "available":
        return {"leg": leg["leg"], "label": leg["label"], "status": "unavailable",
                "reason": leg.get("reason")}
    components = [{"component": name, "label": COMPONENT_LABELS[name],
                   "contribution_pp": leg["contributions_pp"].get(name)} for name in COMPONENTS]
    source = leg.get("expectation_source_kind")
    return {"leg": leg["leg"], "label": leg["label"], "status": "available",
            "window": leg.get("window"), "total_return_percent": leg["total_return_percent"],
            "price_return_percent": leg["price_return_percent"], "components": components,
            "source_label": None if source is None else EXPECTATION_SOURCE_LABELS[source],
            "eps_start": (leg.get("eps_start") or {}).get("value"),
            "eps_end": (leg.get("eps_end") or {}).get("value"),
            "price_start": leg.get("price_start"), "price_end": leg.get("price_end"),
            "price_target": leg.get("price_target"), "notes": leg.get("notes") or [],
            "target_implied_percent": leg.get("target_implied_percent"),
            "dividend_yield_percent": leg.get("dividend_yield_percent"),
            "contradiction": leg.get("contradiction")}


def company_view(connection: sqlite3.Connection, company_ref: str,
                 industry_ref: str | None = None) -> dict[str, Any] | None:
    bridges = latest_bridges(connection, company_ref)
    series = latest_series(connection, company_ref)
    returns = latest_return_bridge(connection, company_ref)
    if not bridges and series is None and returns is None:
        return None
    shown = []
    for semantics in ("annual", "quarterly", "year_to_date"):
        same = [item for item in bridges if item["period"]["semantics"] == semantics]
        if same:
            shown.append(_bridge_view(same[-1]))
    counts = {status: sum(1 for item in bridges if item["status"] == status) for status in BRIDGE_STATUSES}
    per_share = []
    if series is not None:
        for row in series["periods"][-10:]:
            per_share.append({
                "fiscal_label": row["fiscal_label"], "semantics_label": SEMANTICS_LABELS[row["semantics"]],
                "period": f"{row['start']}..{row['end']}",
                **{field: (row.get(field) or {}).get("value") for field in
                   ("basic_eps", "diluted_eps", "basic_shares", "diluted_shares", "dps")},
                "dps_basis": (row.get("dps") or {}).get("basis"),
                "eps_check": row["eps_check"],
                **{f"{field}_label": share_count_text((row.get(field) or {}).get("value"))
                   for field in ("basic_shares", "diluted_shares")},
                "missing_labels": [PER_SHARE_LABELS[field] for field in row["missing"]]})
    ttm = None if series is None else _latest_point(series)
    tags = company_tags(connection, [company_ref] + ([industry_ref] if industry_ref else []))
    sources: dict[str, int] = {}
    for tag in tags:
        for kind in tag.get("kinds") or ():
            sources[kind] = sources.get(kind, 0) + 1
    missing_names = sorted({item["label"] for item in (series or {}).get("missing", [])})
    headline = (f"{len(bridges)} 个申报期的利润桥：{counts['complete']} 个完整勾稽、"
                f"{counts['residual']} 个有差额、{counts['missing_rows']} 个缺报表行")
    if missing_names:
        headline += f"；每股序列缺：{'、'.join(missing_names)}"
    return {
        "headline": headline, "bridge_counts": counts, "bridges": shown,
        "missing_rows": sorted({LINE_CLASS_LABELS.get(item, item) for b in bridges for item in b["missing_rows"]}),
        "per_share": per_share,
        "ttm": None if ttm is None else {"end": ttm["end"], "rule": TTM_RULE_TEXT.get(ttm["rule"]),
                                         "diluted_eps": (ttm.get("diluted_eps") or {}).get("value"),
                                         "dps": (ttm.get("dps") or {}).get("value")},
        "quarters_not_stated": [item["fiscal_label"] for item in (series or {}).get("quarters_not_stated", [])],
        "returns": None if returns is None else {
            "ref": returns["id"], "as_of": returns["as_of"],
            "contradiction": (returns.get("model_consistency") or {}).get("notice"),
            "legs": [_leg_view(leg) for leg in returns["legs"]]},
        "expectation_sources": [{"kind": kind, "label": EXPECTATION_SOURCE_LABELS[kind], "count": count}
                                for kind, count in sorted(sources.items(),
                                                          key=lambda item: EXPECTATION_SOURCE_KINDS.index(item[0]))],
        "unclassified_sources": sum(len(tag.get("unclassified") or ()) for tag in tags),
    }


# ---------------------------------------------------------------------------
# the weekly brief (deterministic; replayed by ref and hash)
# ---------------------------------------------------------------------------


def _leg_numbers(record: Mapping[str, Any] | None) -> dict[str, dict[str, Decimal | None]]:
    out: dict[str, dict[str, Decimal | None]] = {}
    for leg in (record or {}).get("legs") or ():
        if leg.get("status") != "available":
            out[leg["leg"]] = {}
            continue
        numbers = {"total": _dec(leg.get("total_return_percent"))}
        for name in COMPONENTS:
            numbers[name] = _dec((leg.get("contributions_pp") or {}).get(name))
        out[leg["leg"]] = numbers
    return out


def material_changes(prior: Mapping[str, Any] | None, current: Mapping[str, Any]) -> list[dict[str, Any]]:
    before, after = _leg_numbers(prior), _leg_numbers(current)
    changes = []
    for leg in LEG_KINDS:
        old, new = before.get(leg), after.get(leg)
        if new is None:
            continue
        if prior is not None and (not old) != (not new):
            changes.append({"leg": leg, "component": "status",
                            "from": "available" if old else "unavailable",
                            "to": "available" if new else "unavailable"})
            continue
        if not new or not old:
            continue
        for name in ("total", *COMPONENTS):
            a, b = old.get(name), new.get(name)
            if a is not None and b is not None and abs(b - a) >= MATERIAL_CHANGE_PP:
                changes.append({"leg": leg, "component": name, "from": _text(a, "0.01"),
                                "to": _text(b, "0.01")})
    return changes


def brief_entries(connection: sqlite3.Connection, company_refs: Sequence[str],
                  period_start: datetime, period_end: datetime) -> list[dict[str, Any]]:
    """One entry per company whose decomposition moved materially in the window."""

    if not table_exists(connection, "holding_period_return_bridges"):
        return []
    _row_factory(connection)
    out = []
    for company_ref in sorted(company_refs):
        current = latest_return_bridge(connection, company_ref, period_end.isoformat())
        if current is None or current["created_at"] <= period_start.isoformat():
            continue
        prior = latest_return_bridge(connection, company_ref, period_start.isoformat())
        changes = material_changes(prior, current)
        if prior is None:
            changes = [{"leg": leg["leg"], "component": "first", "from": None,
                        "to": leg.get("total_return_percent")}
                       for leg in current["legs"] if leg["status"] == "available"]
        if not changes:
            continue
        out.append({"company_ref": company_ref, "ref": current["id"], "hash": current["content_hash"],
                    "prior_ref": None if prior is None else prior["id"],
                    "prior_hash": None if prior is None else prior["content_hash"],
                    "changes": changes})
    return out


def replay_brief_entries(connection: sqlite3.Connection, entries: Any) -> None:
    if entries is None:
        return
    if not isinstance(entries, list):
        raise ReturnAttributionConflict("weekly brief return section is malformed")
    _row_factory(connection)
    for entry in entries:
        for ref, digest in ((entry.get("ref"), entry.get("hash")),
                            (entry.get("prior_ref"), entry.get("prior_hash"))):
            if ref is None:
                continue
            row = connection.execute(
                "SELECT content_hash FROM holding_period_return_bridges WHERE version_id=?",
                (ref,)).fetchone() if table_exists(connection, "holding_period_return_bridges") else None
            if row is None or row["content_hash"] != digest:
                raise ReturnAttributionConflict("weekly brief return section no longer replays")


def brief_lines(entries: Sequence[Mapping[str, Any]], tickers: Mapping[str, str]) -> list[str]:
    if not entries:
        return ["- 本期没有公司的回报分解发生实质变化（任一分项变动不足 5 个百分点）。"]
    lines = []
    for entry in entries:
        ticker = tickers.get(entry["company_ref"], entry["company_ref"])
        parts = []
        for change in entry["changes"][:4]:
            leg = LEG_LABELS.get(change["leg"], change["leg"])
            if change["component"] == "first":
                parts.append(f"{leg}首次算出，总回报 {change['to']}%")
            elif change["component"] == "status":
                parts.append(f"{leg}{'变为可算' if change['to'] == 'available' else '变为不可算'}")
            else:
                name = "总回报" if change["component"] == "total" else COMPONENT_LABELS[change["component"]]
                parts.append(f"{leg}{name} {change['from']} → {change['to']}")
        lines.append(f"- {ticker}｜回报分解变化：{'；'.join(parts)}｜{entry['ref']}")
    return lines


__all__ = [
    "BRIDGE_STATUSES", "CLASSIFIER_REF", "COMPONENTS", "EXPECTATION_SOURCE_KINDS",
    "EXPECTATION_SOURCE_LABELS", "FiscalCalendar", "ITEM_CONCEPTS", "LEG_KINDS", "LINE_CLASSES",
    "MATERIAL_CHANGE_PP", "PERIOD_SEMANTICS", "ReturnAttributionAuthority",
    "ReturnAttributionConflict", "ReturnAttributionError", "TABLES", "WRITE_SCOPE",
    "brief_entries", "brief_lines", "build_income_bridge", "build_income_bridges",
    "build_per_share_series", "build_return_bridge", "classify_ref", "collect_periods",
    "company_view", "compute_company", "consensus_tags", "conviction_tags", "debate_tags",
    "decompose", "latest_bridges", "latest_return_bridge", "latest_series", "material_changes",
    "read_inputs", "refresh_company", "refresh_industry_tags", "replay_brief_entries",
    "table_exists",
]
