"""Forecast coverage: complete and repair a company statement structure.

The model drafter picks which lines matter and how the company presents its
income statement.  It does not reliably give every reported line a forecast
method, and it sometimes writes a filed subtotal ("total operating expenses")
as a formula term, which makes every line below it unforecastable: a filed
subtotal is tie authority, never a forecast leaf.  The 2026-10-09 diagnosis
found ACN's model computing 7 of 19 lines for exactly these reasons.

This module is the deterministic backstop.  Given a drafted structure
definition, the company specification and the filed financial inputs, it
returns a 0.6 definition in which

* every reported line with at least ``MIN_HISTORY_QUARTERS`` quarters of
  history has a forecast method (``unavailable`` only with less history, and
  then with a written reason);
* a filed subtotal used as a formula term, and every filed earnings total
  (operating income, pretax income, net income, attributable net income), is
  replaced by a derived "sum of components" line whose formula ties exactly to
  the filed total on history -- the formula is searched over the company's own
  lines and accepted only when it reproduces every filed period;
* tax is a rate of pretax income, noncontrolling interest a share of net
  income, the diluted share count and a derived diluted EPS are added when the
  company files them;
* the cash-flow and balance-sheet leaves of a first-version three-statement
  model (buybacks, dividends, acquisitions, cash, debt, working capital) are
  added from a closed concept table when filed.

Nothing here guesses a number.  A repair is applied only when the filed
history proves it; otherwise the drafted line is kept and the coverage report
says why it cannot be forecast.
"""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal
from itertools import product
from typing import Any, Iterable, Mapping, Sequence

COVERAGE_RULE_REF = "rule:forecast-coverage:1"
PROPOSAL_METHOD_COMPLETION_REF = "rule:forecast-proposal-method-completion:0.2"
COVERAGE_SCHEMA_VERSION = "0.6"
UPGRADEABLE_STRUCTURE_VERSIONS = frozenset({"0.3", "0.4", "0.5", "0.6", "0.7"})
MIN_HISTORY_QUARTERS = 8
INCOME_DEPRECIATION_POLICY_REF = "filed-income-depreciation-method:0.1"
INCOME_DEPRECIATION_METHODS = frozenset({"share_of_line", "carry_forward_mean"})
# Share counts and per-share figures are not additive, so a fiscal fourth
# quarter is never filed as a quarter: two years of history is six quarters.
MIN_NON_ADDITIVE_QUARTERS = 6
MIN_TIE_PERIODS = 4
TRAILING_QUARTERS = 4

_QUARTER_MIN_DAYS, _QUARTER_MAX_DAYS = 80, 100

# Closed concept table for lines the code may add.  Ordered: the first filed
# concept wins.  Never a label match.
COVERAGE_CONCEPTS: dict[str, tuple[str, ...]] = {
    "depreciable_assets": ("us-gaap:PropertyPlantAndEquipmentNet", "us-gaap:PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization"),
    "amortizable_assets": ("us-gaap:IntangibleAssetsNetExcludingGoodwill", "us-gaap:FiniteLivedIntangibleAssetsNet"),
    "finance_lease_principal": ("us-gaap:FinanceLeasePrincipalPayments", "us-gaap:CapitalLeasePrincipalPayments"),
    "capitalized_software": ("us-gaap:PaymentsToAcquireSoftware", "us-gaap:PaymentsForSoftware"),
    "capitalized_contract_costs": ("us-gaap:PaymentsForCapitalizedContractCosts", "us-gaap:PaymentsToAcquireCapitalizedContractCosts"),
    "diluted_weighted_average_shares": (
        "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
    ),
    "diluted_eps": ("us-gaap:EarningsPerShareDiluted",),
    "share_repurchase": ("us-gaap:PaymentsForRepurchaseOfCommonStock",),
    "dividends_paid": (
        "us-gaap:PaymentsOfDividendsCommonStock", "us-gaap:PaymentsOfDividends",
        "us-gaap:PaymentsOfOrdinaryDividends",
    ),
    # 2026-10-09 (P0): depreciation and amortization, so operating cash flow
    # can be derived from net income + D&A + working capital (META v3 held
    # CFO at 57% of revenue while operating income went deeply negative).
    "depreciation_amortization": (
        "us-gaap:DepreciationDepletionAndAmortization",
        "us-gaap:DepreciationAmortizationAndAccretionNet",
        "us-gaap:DepreciationAndAmortization",
    ),
    "acquisitions": (
        "us-gaap:PaymentsToAcquireBusinessesNetOfCashAcquired",
        "us-gaap:PaymentsToAcquireBusinessesAndInterestInAffiliates",
        "us-gaap:PaymentsToAcquireBusinessesGross",
    ),
    "cash_and_equivalents": ("us-gaap:CashAndCashEquivalentsAtCarryingValue",),
    "debt_current": (
        "us-gaap:DebtCurrent", "us-gaap:LongTermDebtCurrent",
        "us-gaap:ShortTermBorrowings", "us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent",
    ),
    "debt_noncurrent": (
        "us-gaap:LongTermDebtNoncurrent",
        "us-gaap:LongTermDebtAndCapitalLeaseObligations", "us-gaap:LongTermDebt",
    ),
    "accounts_receivable": ("us-gaap:AccountsReceivableNetCurrent",),
    "accounts_payable": ("us-gaap:AccountsPayableCurrent",),
    "deferred_revenue": (
        "us-gaap:ContractWithCustomerLiabilityCurrent", "us-gaap:DeferredRevenueCurrent",
    ),
}
LEAF_LABELS: dict[str, str] = {
    "depreciable_assets": "可折旧固定资产净额", "amortizable_assets": "可摊销无形资产净额",
    "finance_lease_principal": "融资租赁本金", "capitalized_software": "资本化软件支出",
    "capitalized_contract_costs": "资本化合同成本支出",
    "diluted_weighted_average_shares": "稀释加权平均股数",
    "diluted_eps": "稀释每股收益",
    "diluted_eps_numerator": "稀释每股收益分子",
    "share_repurchase": "股票回购",
    "dividends_paid": "现金分红",
    "acquisitions": "并购支出（扣除取得的现金）",
    "depreciation_amortization": "折旧与摊销",
    "cash_and_equivalents": "现金及现金等价物",
    "debt_current": "短期债务",
    "debt_noncurrent": "长期债务",
    "accounts_receivable": "应收账款",
    "accounts_payable": "应付账款",
    "deferred_revenue": "递延收入（流动）",
}
CASH_LEAF_ROLES = ("share_repurchase", "dividends_paid", "acquisitions",
                   "depreciation_amortization", "finance_lease_principal", "capitalized_software", "capitalized_contract_costs")
BALANCE_LEAF_ROLES = (
    "cash_and_equivalents", "debt_current", "debt_noncurrent",
    "accounts_receivable", "accounts_payable", "deferred_revenue", "depreciable_assets", "amortizable_assets",
)
# Filed concepts that are non-recurring by their own definition.  A line the
# spec marks ``behaviour: one_off`` is treated the same way.
ONE_OFF_CONCEPTS = frozenset({
    "us-gaap:GainLossOnSaleOfBusiness",
    "us-gaap:GainLossOnDispositionOfAssets",
    "us-gaap:GainLossOnDispositionOfAssets1",
    "us-gaap:GoodwillImpairmentLoss",
    "us-gaap:AssetImpairmentCharges",
    "us-gaap:GainsLossesOnExtinguishmentOfDebt",
    "us-gaap:DisposalGroupNotDiscontinuedOperationGainLossOnDisposal",
})
# Company totals of operating cost presented as one filed line.
TOTAL_COST_CONCEPTS = frozenset({
    "us-gaap:CostsAndExpenses", "us-gaap:OperatingExpenses",
    "us-gaap:OperatingCostsAndExpenses", "us-gaap:BenefitsLossesAndExpenses",
})
EARNINGS_TOTAL_ROLES = (
    "gross_profit", "operating_income", "pretax_income",
    "income_from_continuing_operations", "net_income", "parent_net_income",
)
NON_ADDITIVE_ROLES = frozenset({"diluted_weighted_average_shares", "diluted_eps"})
# 2026-10-09 (P0): non-operating lines that may hold securities gains.
NONOPERATING_GAIN_ROLES = frozenset({"nonoperating_income_expense",
                                     "other_nonoperating_income_expense"})
NONOPERATING_MATERIAL_SHARE = Decimal("0.05")

# Chinese method labels shown in the model table's 方法 column.
METHOD_LABELS: dict[str, str] = {
    "year_over_year_growth": "同比增长", "seasonal_factor": "季度季节因子",
    "quarterly_growth": "驱动",
    "share_of_line": "比率",
    "formula": "公式",
    "carry_forward_mean": "延续",
    "zero_one_off": "一次性归零",
    "rate_times_balance": "利率×余额",
    "hold_last": "持有",
    "roll_forward": "滚动",
    "unavailable": "不可预测",
}


def coverage_concepts() -> list[str]:
    """Every concept the input table should hold for a 0.6 completion."""

    return sorted({concept for concepts in COVERAGE_CONCEPTS.values()
                   for concept in concepts})


def method_label(method: Any, *, base_role: Any = None) -> str:
    if method == "share_of_line" and base_role == "revenue":
        return "占收入比"
    return METHOD_LABELS.get(str(method), str(method or "—"))


# -- filed values -------------------------------------------------------------


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() else None


def _quarter(cell: Mapping[str, Any], end: str) -> bool:
    start = cell.get("period_start")
    if not start:
        return False
    try:
        days = (date.fromisoformat(str(end)) - date.fromisoformat(str(start))).days + 1
    except ValueError:
        return False
    return _QUARTER_MIN_DAYS <= days <= _QUARTER_MAX_DAYS


def filed_values(line: Mapping[str, Any]) -> dict[str, Decimal]:
    """Quarter values of a duration line, or instants of a balance line."""

    out: dict[str, Decimal] = {}
    instant = line.get("period_basis") == "instant"
    for end, cell in (line.get("cells") or {}).items():
        if not isinstance(cell, Mapping):
            continue
        if not instant and not _quarter(cell, str(end)):
            continue
        value = _decimal(cell.get("value"))
        if value is not None:
            out[str(end)] = value
    return out


def _filed_index(inputs: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(item.get("concept")): item for item in (inputs.get("filed_lines") or [])
        if isinstance(item, Mapping) and item.get("status") == "filed"
    }


def _single_unit(line: Mapping[str, Any]) -> str | None:
    units = {str(cell.get("unit")).casefold()
             for cell in (line.get("cells") or {}).values()
             if isinstance(cell, Mapping) and cell.get("unit")}
    return next(iter(units)) if len(units) == 1 else None


def _accessions(line: Mapping[str, Any]) -> set[str]:
    return {
        str(ref) for cell in (line.get("cells") or {}).values()
        if isinstance(cell, Mapping)
        for ref in (cell.get("source_accessions") or []) if ref
    }


class _Values:
    """Historical values of every structure line, filed or derived."""

    def __init__(self, lines: Mapping[str, Mapping[str, Any]],
                 formulas: Mapping[str, Mapping[str, Any]],
                 filed: Mapping[str, Mapping[str, Any]]):
        self.lines, self.formulas, self.filed = lines, formulas, filed
        self.memo: dict[str, dict[str, Decimal]] = {}

    def of(self, ref: str, trail: frozenset[str] = frozenset()) -> dict[str, Decimal]:
        if ref in self.memo:
            return self.memo[ref]
        line = self.lines.get(ref)
        out: dict[str, Decimal] = {}
        if line is None or ref in trail:
            return out
        if line.get("kind") == "filed":
            source = self.filed.get(str(line.get("concept")))
            out = {} if source is None else filed_values(source)
        else:
            formula = self.formulas.get(ref)
            if formula is not None and formula.get("operator") == "sum":
                out = combine([
                    (self.of(str(term["line_ref"]), trail | {ref}),
                     Decimal(str(term["coefficient"])))
                    for term in formula.get("terms") or []
                ])
            elif formula is not None:
                numerator = self.of(str(formula["numerator_ref"]), trail | {ref})
                denominator = self.of(str(formula["denominator_ref"]), trail | {ref})
                out = {end: numerator[end] / denominator[end]
                       for end in set(numerator) & set(denominator)
                       if denominator[end] != 0}
        self.memo[ref] = out
        return out


def combine(terms: Sequence[tuple[Mapping[str, Decimal], Decimal]]) -> dict[str, Decimal]:
    if not terms:
        return {}
    ends = set(terms[0][0])
    for values, _coefficient in terms[1:]:
        ends &= set(values)
    return {end: sum((coefficient * values[end] for values, coefficient in terms),
                     Decimal(0)) for end in ends}


def ties_exactly(calculated: Mapping[str, Decimal], filed: Mapping[str, Decimal],
                 *, minimum: int = MIN_TIE_PERIODS,
                 leaves: Sequence[Mapping[str, Decimal]] | None = None) -> bool:
    """Exact on every common period, or within the filed presentation rounding.

    ``leaves`` are the filed leaf series under the formula; with them a period
    may miss by the bound ``presentation_rounding_ties`` derives from the
    filed precision (IBM presents millions), exactly as the structure replay
    will accept it.
    """

    from .company_financial_statement_structure import presentation_rounding_ties

    common = set(calculated) & set(filed)
    if len(common) < min(minimum, max(len(filed), 1)) or not common:
        return False
    for end in common:
        if calculated[end] == filed[end]:
            continue
        if leaves is None:
            return False
        values = [series[end] for series in leaves if end in series]
        if len(values) != len(leaves) or not presentation_rounding_ties(
                calculated[end], filed[end], values):
            return False
    return True


def ties_per_share(numerator: Mapping[str, Decimal], shares: Mapping[str, Decimal],
                   eps: Mapping[str, Decimal], *, minimum: int = MIN_TIE_PERIODS) -> bool:
    common = set(numerator) & set(shares) & set(eps)
    if len(common) < minimum:
        return False
    for end in common:
        if shares[end] <= 0:
            return False
        quantum = Decimal(1).scaleb(eps[end].as_tuple().exponent)
        if (numerator[end] / shares[end]).quantize(quantum) != eps[end]:
            return False
    return True


# -- the completion -------------------------------------------------------------


def _history_count(line: Mapping[str, Any], filed: Mapping[str, Mapping[str, Any]]) -> int:
    source = filed.get(str(line.get("concept")))
    return 0 if source is None else len(filed_values(source))


def _recurring(source: Mapping[str, Any] | None) -> bool:
    """Booked in at least three of every four filed quarters (a cost, not an event)."""

    if source is None:
        return False
    values = filed_values(source)
    if not values:
        return False
    booked = sum(1 for value in values.values() if value != 0)
    return booked * 4 >= len(values) * 3


def _required_history(line: Mapping[str, Any]) -> int:
    if line.get("role") in {"debt_current", "debt_noncurrent"}:
        # A debt balance is an instant carried explicitly from its own date.
        # The interest mechanism separately needs four matched debt/rate quarters.
        return 1
    return (MIN_NON_ADDITIVE_QUARTERS if line.get("role") in NON_ADDITIVE_ROLES
            else MIN_HISTORY_QUARTERS)


def _one_off_concepts(spec: Mapping[str, Any]) -> set[str]:
    return {
        str(item.get("basis_concept"))
        for item in (spec.get("expense_lines") or [])
        if isinstance(item, Mapping) and item.get("behaviour") == "one_off"
        and item.get("basis_concept")
    } | set(ONE_OFF_CONCEPTS)


def _spec_concepts(spec: Mapping[str, Any]) -> set[str]:
    concepts = {str(spec.get("revenue_anchor_concept") or "")}
    concepts |= {
        str(item.get("basis_concept")) for item in (spec.get("expense_lines") or [])
        if isinstance(item, Mapping) and item.get("basis_concept")
    }
    return concepts


def _unique_ref(lines: Mapping[str, Any], wanted: str) -> str:
    ref, number = wanted, 2
    while ref in lines:
        ref, number = f"{wanted}-{number}", number + 1
    return ref


def _evidence(term_refs: Iterable[str], lines: Mapping[str, Mapping[str, Any]],
              formulas: Mapping[str, Mapping[str, Any]],
              filed: Mapping[str, Mapping[str, Any]], tie: str | None) -> list[str]:
    """Statement accessions behind the operands, newest last, at most eight."""

    seen: set[str] = set()
    found: set[str] = set()

    def walk(ref: str) -> None:
        if ref in seen:
            return
        seen.add(ref)
        line = lines.get(ref) or {}
        if line.get("kind") == "filed":
            source = filed.get(str(line.get("concept")))
            if source is not None:
                found.update(_accessions(source))
            return
        formula = formulas.get(ref) or {}
        refs = ([str(item["line_ref"]) for item in formula.get("terms") or []]
                if formula.get("operator") == "sum" else
                [str(formula.get("numerator_ref")), str(formula.get("denominator_ref"))])
        for item in refs:
            walk(item)

    for ref in term_refs:
        walk(ref)
    if tie is not None and tie in filed:
        narrowed = found & _accessions(filed[tie])
        found = narrowed or found
    return sorted(found)[-8:]


def _sign_options(terms: Sequence[tuple[str, int, bool]]) -> list[list[tuple[str, int]]]:
    """Canonical signs first, then every flip of the sign-ambiguous terms."""

    ambiguous = [index for index, item in enumerate(terms) if item[2]]
    options: list[list[tuple[str, int]]] = []
    for flips in product((False, True), repeat=len(ambiguous)):
        flipped = {index for index, flip in zip(ambiguous, flips) if flip}
        options.append([(ref, -sign if index in flipped else sign)
                        for index, (ref, sign, _ambiguous) in enumerate(terms)])
    return options[:64]



def income_depreciation_policy(spec: Mapping[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any] | None:
    """Bind a drafter's explicit income D&A assumption, never a cash fallback."""
    from .store import content_hash
    definition = spec.get("financial_statement_structure") or {}
    if definition.get("schema_version") not in {COVERAGE_SCHEMA_VERSION, "0.7"}:
        return None
    filed = _filed_index(inputs)
    capex_method = next((x.get("forecast_method") for x in (spec.get("cash_flow_companion") or {}).get("lines", [])
                         if x.get("role") == "capital_expenditure"), None)
    asset_sources = [filed[c] for role in ("depreciable_assets", "amortizable_assets")
                     for c in COVERAGE_CONCEPTS[role] if c in filed]
    capex_history = next((x.get("series", {}).get("quarters", []) for x in inputs.get("cash_flow_inputs", [])
                          if x.get("role") == "capital_expenditure" and x.get("status") == "filed"), [])
    # A mature asset roll with an explicit capex assumption stays unchanged.
    # Only an unexecutable roll may retain the already-declared income method.
    bindings = []
    for line in definition.get("lines") or []:
        if (line.get("statement") != "income" or line.get("kind") != "filed"
                or line.get("concept") not in COVERAGE_CONCEPTS["depreciation_amortization"]
                or line.get("forecast_method") not in INCOME_DEPRECIATION_METHODS):
            continue
        if _history_count(line, filed) < MIN_HISTORY_QUARTERS:
            continue
        source = filed.get(line["concept"])
        common = set(filed_values(source)) if source is not None else set()
        for asset in asset_sources:
            common &= set(filed_values(asset))
        if capex_method == "share_of_line" and len(capex_history) >= 4 and asset_sources and len(common) >= 4 and all(
                sum((filed_values(asset)[end] for asset in asset_sources), Decimal(0)) > 0 for end in common):
            continue
        if source is None or source.get("statement") != "income":
            continue
        if line["forecast_method"] == "share_of_line":
            base = next((x for x in definition.get("lines") or [] if x["ref"] == line.get("forecast_base_ref")), None)
            if base is None or base.get("role") != "revenue" or base.get("kind") != "filed":
                continue
            revenue = filed.get(base.get("concept"))
            amounts = filed_values(source);bases = filed_values(revenue) if revenue else {}
            if len(set(amounts) & set(bases)) < MIN_HISTORY_QUARTERS or any(v <= 0 for v in bases.values()):
                continue
        bindings.append({"line_ref":line["ref"],"concept":line["concept"],
            "method":line["forecast_method"],"forecast_base_ref":line.get("forecast_base_ref"),
            "source_history_hash":content_hash({end:str(value) for end,value in filed_values(source).items()}),
            "assumption":"保留模型明确选择的利润表折旧预测方法；这是预测假设，不是现金流资产滚动事实。"})
    if not bindings:
        return None
    body={"contract_ref":INCOME_DEPRECIATION_POLICY_REF,"lines":bindings}
    return {**body,"content_hash":content_hash(body)}

class _Completion:
    def __init__(self, definition: Mapping[str, Any], spec: Mapping[str, Any],
                 inputs: Mapping[str, Any]):
        self.definition = copy.deepcopy(dict(definition))
        self.income_depreciation_policy = income_depreciation_policy(spec, inputs)
        self.spec = spec
        self.filed = _filed_index(inputs)
        self.lines: dict[str, dict[str, Any]] = {
            str(item["ref"]): dict(item) for item in self.definition.get("lines") or []
        }
        self.order: list[str] = [str(item["ref"]) for item in self.definition.get("lines") or []]
        self.formulas: dict[str, dict[str, Any]] = {
            str(item["output_ref"]): dict(item)
            for item in self.definition.get("formulas") or []
        }
        self.formula_order: list[str] = list(self.formulas)
        self.repairs: list[dict[str, Any]] = []
        self.sources: dict[str, str] = {ref: "drafted" for ref in self.order}
        self.reasons: dict[str, str] = {}
        self.one_off = _one_off_concepts(spec)
        self.spec_concepts = _spec_concepts(spec)
        # A note-backed numerator is proven over exact filed terms; those
        # terms stay filed whatever role they carry.
        notes = {str(item.get("ref")) for item in self.definition.get("note_evidence") or []
                 if isinstance(item, Mapping)}
        self.protected: set[str] = {
            str(term["line_ref"])
            for formula in self.formulas.values()
            if formula.get("tie_out_concept") is None
            and any(str(ref) in notes for ref in formula.get("evidence_refs") or [])
            for term in formula.get("terms") or []
        }

    # helpers ---------------------------------------------------------------

    def values(self) -> _Values:
        return _Values(self.lines, self.formulas, self.filed)

    def by_role(self, *roles: str) -> list[str]:
        return [ref for ref in self.order if self.lines[ref].get("role") in roles]

    def terms_used(self) -> set[str]:
        used: set[str] = set()
        for formula in self.formulas.values():
            if formula.get("operator") == "sum":
                used.update(str(item["line_ref"]) for item in formula.get("terms") or [])
            else:
                used.update({str(formula.get("numerator_ref")),
                             str(formula.get("denominator_ref"))})
        return used

    def unit(self) -> str:
        revenue = self.by_role("revenue")
        return str(self.lines[revenue[0]].get("unit") or "usd") if revenue else "usd"

    def add_line(self, line: dict[str, Any], *, after: str | None = None) -> str:
        ref = _unique_ref(self.lines, str(line["ref"]))
        line["ref"] = ref
        self.lines[ref] = line
        if after is not None and after in self.order:
            self.order.insert(self.order.index(after) + 1, ref)
        else:
            self.order.append(ref)
        self.sources[ref] = "added"
        return ref

    def derive(self, ref: str, terms: Sequence[tuple[str, int]], *, kind: str,
               detail: str) -> None:
        """Turn a filed total into a derived line tied to its own filed concept."""

        line = self.lines[ref]
        tie = str(line["concept"])
        line.update({
            "kind": "derived", "concept": None, "forecast_method": "formula",
            "forecast_base_ref": None,
            "role": ("company_presented_subtotal"
                     if line.get("role") == "company_presented_component"
                     else line.get("role")),
        })
        if "annual_forecast_method" in line:
            line["annual_forecast_method"] = None
        self.formulas[ref] = {
            "output_ref": ref, "operator": "sum",
            "terms": [{"line_ref": term, "coefficient": str(sign)} for term, sign in terms],
            "tie_out_concept": tie,
            "evidence_refs": _evidence([term for term, _ in terms], self.lines,
                                       self.formulas, self.filed, tie),
        }
        if ref not in self.formula_order:
            self.formula_order.append(ref)
        self.sources[ref] = "repaired"
        self.repairs.append({"kind": kind, "line_ref": ref, "detail": detail,
                             "tie_out_concept": tie})

    def try_derive(self, ref: str, candidates: Sequence[Sequence[tuple[str, int, bool]]],
                   *, kind: str, detail: str) -> bool:
        if ref in self.protected:
            return False
        line = self.lines[ref]
        tie = self.filed.get(str(line.get("concept")))
        if tie is None:
            return False
        target = filed_values(tie)
        values = self.values()
        for candidate in candidates:
            if not candidate or any(term == ref for term, _s, _a in candidate):
                continue
            if any(term not in self.lines for term, _s, _a in candidate):
                continue
            leaves = [values.of(ref) for ref in self.filed_leaves(
                [term for term, _s, _a in candidate])]
            for option in _sign_options(candidate):
                calculated = combine([(values.of(term), Decimal(sign))
                                      for term, sign in option])
                if ties_exactly(calculated, target, leaves=leaves):
                    self.derive(ref, option, kind=kind, detail=detail)
                    return True
        return False

    # steps -----------------------------------------------------------------

    def repair_company_totals(self) -> None:
        used = self.terms_used()
        for ref in list(self.order):
            line = self.lines[ref]
            if line.get("kind") != "filed" or line.get("statement", "income") != "income":
                continue
            concept = str(line.get("concept"))
            is_total = concept in TOTAL_COST_CONCEPTS or (
                line.get("role") == "company_presented_component" and ref in used)
            if not is_total or concept in self.spec_concepts:
                continue
            costs = [item for item in self.by_role("cost_of_revenue") if item != ref]
            opex = [item for item in self.by_role("operating_expense") if item != ref]
            components = [item for item in self.by_role("company_presented_component")
                          if item != ref and item not in used
                          and str(self.lines[item].get("concept")) not in TOTAL_COST_CONCEPTS]
            others = [item for item in self.by_role(
                "interest_expense", "interest_income", "other_operating_income_expense",
                "nonoperating_income_expense", "other_nonoperating_income_expense")
                if item != ref and item not in used]
            signed = ([(item, 1, True) for item in components]
                      + [(item, -1 if self.lines[item].get("role") == "interest_income" else 1,
                          True) for item in others])
            candidates = [
                [(item, 1, False) for item in costs + opex + components],
                [(item, 1, False) for item in costs + opex],
                [(item, 1, False) for item in opex + components],
                [(item, 1, False) for item in opex],
                # A company total that nets income items and interest into
                # "expense and other income" (IBM): signs are searched, the
                # filed history decides.
                [(item, 1, False) for item in opex] + signed,
                [(item, 1, False) for item in costs + opex] + signed,
            ]
            self.try_derive(ref, candidates, kind="subtotal_derived",
                            detail="reported subtotal replaced by the sum of its filed components")

    def repair_earnings_totals(self) -> None:
        for role in EARNINGS_TOTAL_ROLES:
            for ref in self.by_role(role):
                line = self.lines[ref]
                if line.get("kind") != "filed" or str(line.get("concept")) in self.spec_concepts:
                    continue
                candidates = self._earnings_candidates(role, ref)
                kind = "pretax_formula" if role == "pretax_income" else "total_derived"
                self.try_derive(ref, candidates, kind=kind,
                                detail=f"filed {role} replaced by its tied formula")

    def _earnings_candidates(self, role: str, ref: str) -> list[list[tuple[str, int, bool]]]:
        def lines(*roles: str) -> list[str]:
            return [item for item in self.by_role(*roles) if item != ref]

        revenue = lines("revenue")
        costs = lines("cost_of_revenue")
        opex = lines("operating_expense")
        other_operating = lines("other_operating_income_expense")
        subtotals = [item for item in lines("company_presented_subtotal")
                     if self.lines[item].get("kind") == "derived"]
        components = [item for item in lines("company_presented_component")
                      if item not in self.terms_used()]
        out: list[list[tuple[str, int, bool]]] = []
        if role == "gross_profit" and revenue:
            out.append([(revenue[0], 1, False)] + [(item, -1, False) for item in costs])
        elif role == "operating_income" and revenue:
            gross = lines("gross_profit")
            if gross:
                out.append([(gross[0], 1, False)] + [(item, -1, False) for item in opex]
                           + [(item, 1, True) for item in other_operating])
            for subtotal in subtotals:
                out.append([(revenue[0], 1, False), (subtotal, -1, False)]
                           + [(item, 1, True) for item in other_operating])
            out.append([(revenue[0], 1, False)]
                       + [(item, -1, False) for item in costs + opex]
                       + [(item, -1, True) for item in components]
                       + [(item, 1, True) for item in other_operating])
        elif role == "pretax_income":
            operating = lines("operating_income")
            nonoperating = (
                [(item, 1, False) for item in lines("interest_income")]
                + [(item, -1, False) for item in lines("interest_expense")]
                + [(item, 1, True) for item in lines(
                    "nonoperating_income_expense", "other_nonoperating_income_expense")]
            )
            if operating:
                out.append([(operating[0], 1, False)] + nonoperating)
                in_operating = self._formula_leaves(operating[0])
                extra = [(item, 1, True) for item in other_operating
                         if item not in in_operating]
                if extra:
                    out.append([(operating[0], 1, False)] + nonoperating + extra)
            gross = lines("gross_profit")
            if gross and not operating:
                out.append([(gross[0], 1, False)]
                           + [(item, -1, False) for item in opex]
                           + [(item, -1, True) for item in components]
                           + [(item, 1, True) for item in other_operating]
                           + nonoperating)
        elif role == "income_from_continuing_operations":
            pretax, tax = lines("pretax_income"), lines("income_tax_expense")
            if pretax and tax:
                out.append([(pretax[0], 1, False), (tax[0], -1, False)])
        elif role == "net_income":
            pretax, tax = lines("pretax_income"), lines("income_tax_expense")
            continuing = lines("income_from_continuing_operations")
            discontinued = lines("discontinued_operations")
            if continuing:
                out.append([(continuing[0], 1, False)]
                           + [(item, 1, True) for item in discontinued])
            if pretax and tax:
                out.append([(pretax[0], 1, False), (tax[0], -1, False)]
                           + [(item, 1, True) for item in discontinued])
        elif role == "parent_net_income":
            net = lines("net_income")
            if net:
                out.append([(net[0], 1, False)]
                           + [(item, -1, True) for item in lines("noncontrolling_interest")])
        return out

    def filed_leaves(self, refs: Sequence[str]) -> list[str]:
        out: list[str] = []
        for ref in refs:
            line = self.lines.get(ref) or {}
            formula = self.formulas.get(ref)
            if line.get("kind") == "filed" or formula is None or formula.get(
                    "operator") != "sum":
                out.append(ref)
            else:
                out.extend(self.filed_leaves(
                    [str(term["line_ref"]) for term in formula.get("terms") or []]))
        return out

    def _formula_leaves(self, ref: str) -> set[str]:
        formula = self.formulas.get(ref)
        if formula is None or formula.get("operator") != "sum":
            return {ref}
        out: set[str] = set()
        for term in formula.get("terms") or []:
            out |= self._formula_leaves(str(term["line_ref"]))
        return out

    def add_eps_chain(self) -> None:
        unit = self.unit()
        shares_refs = self.by_role("diluted_weighted_average_shares")
        if not shares_refs:
            concept = self._first_filed("diluted_weighted_average_shares", unit_kind="shares")
            if concept is None:
                return
            shares_refs = [self.add_line({
                "ref": "diluted-shares", "role": "diluted_weighted_average_shares",
                "label": LEAF_LABELS["diluted_weighted_average_shares"], "kind": "filed",
                "concept": concept, "statement": "income", "unit": "shares",
                "period_kind": "duration", "annual_semantics": "direct_annual",
                "forecast_method": "unavailable", "forecast_base_ref": None,
                "annual_forecast_method": "unavailable",
            })]
            self.repairs.append({"kind": "line_added", "line_ref": shares_refs[0],
                                 "detail": "diluted share count added from the filings",
                                 "tie_out_concept": None})
        shares_ref = shares_refs[0]
        eps_refs = self.by_role("diluted_eps")
        eps_ref = eps_refs[0] if eps_refs else None
        if eps_ref is not None and self.lines[eps_ref].get("kind") == "derived":
            return
        eps_concept = (str(self.lines[eps_ref]["concept"]) if eps_ref is not None
                       else self._first_filed("diluted_eps", unit_kind="per_share"))
        if eps_concept is None or self.lines[shares_ref].get("kind") != "filed":
            return
        values = self.values()
        eps_values = filed_values(self.filed[eps_concept])
        shares_values = values.of(shares_ref)
        numerators = self.by_role("diluted_eps_numerator")
        candidates: list[tuple[list[tuple[str, int]], str | None]] = []
        if numerators:
            candidates.append(([], None))
        parents = self.by_role("parent_net_income")
        nets = self.by_role("net_income")
        nci = self.by_role("noncontrolling_interest")
        base = parents[0] if parents else (nets[0] if nets else None)
        if base is None:
            return
        candidates.append(([(base, 1)], self._tie_of(base)))
        for item in nci:
            candidates.append(([(base, 1), (item, 1)], None))
        for terms, tie in candidates:
            if not terms:
                numerator_ref = numerators[0]
                numerator_values = values.of(numerator_ref)
            else:
                # ``tie`` None: no filed total of its own (attributable net
                # income plus the exchangeable-share interest); the structure
                # then proves it through the filed diluted EPS divide.
                numerator_values = combine([(values.of(ref), Decimal(sign))
                                            for ref, sign in terms])
            if not ties_per_share(numerator_values, shares_values, eps_values):
                continue
            if terms:
                numerator_ref = self.add_line({
                    "ref": "diluted-eps-numerator", "role": "diluted_eps_numerator",
                    "label": LEAF_LABELS["diluted_eps_numerator"], "kind": "derived",
                    "concept": None, "statement": "income", "unit": unit,
                    "period_kind": "duration", "annual_semantics": "sum_quarters",
                    "forecast_method": "formula", "forecast_base_ref": None,
                    "annual_forecast_method": None,
                }, after=base)
                self.formulas[numerator_ref] = {
                    "output_ref": numerator_ref, "operator": "sum",
                    "terms": [{"line_ref": ref, "coefficient": str(sign)}
                              for ref, sign in terms],
                    "tie_out_concept": tie,
                    "evidence_refs": _evidence([ref for ref, _ in terms], self.lines,
                                               self.formulas, self.filed, tie),
                }
                self.formula_order.append(numerator_ref)
            eps_line = {
                "ref": "diluted-eps", "role": "diluted_eps", "label": LEAF_LABELS["diluted_eps"],
                "kind": "derived", "concept": None, "statement": "income",
                "unit": f"{unit}_per_share", "period_kind": "duration",
                "annual_semantics": "annual_ratio", "forecast_method": "formula",
                "forecast_base_ref": None, "annual_forecast_method": None,
            }
            if eps_ref is not None:
                existing = self.lines[eps_ref]
                existing.update({key: value for key, value in eps_line.items()
                                 if key not in {"ref", "label", "unit", "annual_semantics"}})
                self.sources[eps_ref] = "repaired"
            else:
                eps_ref = self.add_line(eps_line)
            self.formulas[eps_ref] = {
                "output_ref": eps_ref, "operator": "divide",
                "numerator_ref": numerator_ref, "denominator_ref": shares_ref,
                "tie_out_concept": eps_concept,
                "evidence_refs": _evidence([numerator_ref, shares_ref], self.lines,
                                           self.formulas, self.filed, eps_concept),
            }
            if eps_ref not in self.formula_order:
                self.formula_order.append(eps_ref)
            self.repairs.append({"kind": "eps_chain", "line_ref": eps_ref,
                                 "detail": "diluted EPS = numerator / diluted shares, tied to filed EPS",
                                 "tie_out_concept": eps_concept})
            return

    def _tie_of(self, ref: str) -> str | None:
        line = self.lines[ref]
        if line.get("kind") == "filed":
            return str(line.get("concept"))
        formula = self.formulas.get(ref) or {}
        return formula.get("tie_out_concept")

    def _first_filed(self, role: str, *, unit_kind: str) -> str | None:
        used = {str(line.get("concept")) for line in self.lines.values()
                if line.get("concept")}
        used |= {str(item.get("tie_out_concept")) for item in self.formulas.values()
                 if item.get("tie_out_concept")}
        candidates = COVERAGE_CONCEPTS.get(role, ())
        if role in {"debt_current", "debt_noncurrent"}:
            # A taxonomy change must not choose an older balance merely because
            # that exact concept precedes the current disclosed concept in a list.
            candidates = sorted(candidates, key=lambda c:max(filed_values(self.filed.get(c) or {}), default=""), reverse=True)
        for concept in candidates:
            source = self.filed.get(concept)
            if source is None or concept in used:
                continue
            unit = _single_unit(source)
            if unit is None:
                continue
            if unit_kind == "shares" and unit != "shares":
                continue
            if unit_kind == "per_share" and not unit.endswith("_per_share"):
                continue
            if unit_kind == "currency" and unit != self.unit():
                continue
            return concept
        return None

    def add_statement_leaves(self) -> None:
        for role in CASH_LEAF_ROLES + BALANCE_LEAF_ROLES:
            if self.by_role(role):
                continue
            concept = self._first_filed(role, unit_kind="currency")
            if concept is None:
                continue
            source = self.filed[concept]
            statement = "cash" if role in CASH_LEAF_ROLES else "balance"
            if source.get("statement") != statement:
                continue
            expected_kind = "duration" if statement == "cash" else "instant"
            if source.get("period_basis") != expected_kind:
                continue
            if len(filed_values(source)) < (1 if role in {"depreciable_assets", "amortizable_assets", "debt_current", "debt_noncurrent"} else MIN_HISTORY_QUARTERS):
                continue
            ref = self.add_line({
                "ref": role.replace("_", "-"), "role": role, "label": LEAF_LABELS[role],
                "kind": "filed", "concept": concept, "statement": statement,
                "unit": self.unit(), "period_kind": expected_kind,
                "annual_semantics": "sum_quarters" if statement == "cash" else "not_applicable",
                "forecast_method": "unavailable", "forecast_base_ref": None,
                "annual_forecast_method": None,
            })
            self.repairs.append({"kind": "line_added", "line_ref": ref,
                                 "detail": f"{statement} statement leaf added from the filings",
                                 "tie_out_concept": None})

    def complete_methods(self) -> None:
        revenue = (self.by_role("revenue") or [None])[0]
        pretax = (self.by_role("pretax_income") or [None])[0]
        net = (self.by_role("net_income") or [None])[0]
        cash = (self.by_role("cash_and_equivalents") or [None])[0]
        debt = (self.by_role("debt_noncurrent") or self.by_role("debt_current") or [None])[0]
        for ref in self.order:
            line = self.lines[ref]
            if line.get("kind") != "filed":
                continue
            role = str(line.get("role"))
            drafted = str(line.get("forecast_method"))
            history = _history_count(line, self.filed)
            required = _required_history(line)
            concept = str(line.get("concept"))
            if role == "interest_expense":
                self._set(ref, "rate_times_balance" if debt else "unavailable", debt)
                self.reasons[ref] = ("利息按当期债务余额×历史季度利率预测" if debt else "缺少已申报债务余额，利息不可预测；禁止环比或均值替代")
                continue
            if concept in COVERAGE_CONCEPTS["depreciation_amortization"]:
                policy = self.income_depreciation_policy or {}
                if any(item["line_ref"] == ref for item in policy.get("lines", [])) and self._valid(ref, drafted):
                    self.reasons[ref] = "保留模型明确选择的利润表折旧预测方法，按完整申报历史核验；不将现金流资产滚动要求覆盖此假设。"
                    self.repairs.append({"kind":"income_depreciation_method", "line_ref":ref,
                        "detail":self.reasons[ref], "tie_out_concept":None})
                    continue
                assets = (self.by_role("depreciable_assets") or [None])[0]
                self._set(ref, "roll_forward" if assets else "unavailable", assets)
                self.reasons[ref] = ("折旧与摊销由资产期初余额＋资本开支−折旧的滚动表计算" if assets else "缺少可折旧资产基底，折旧不可预测；禁止收入占比或环比替代")
                continue
            if role in {"depreciable_assets", "amortizable_assets"}:
                self._set(ref, "hold_last", None)
                self.reasons[ref] = "申报资产基底；可折旧固定资产余额由折旧滚动表更新"
                continue
            if role in EARNINGS_TOTAL_ROLES:
                # A filed total stays tie authority; only a tied formula may
                # forecast it, and the repair above found none.
                if drafted != "unavailable":
                    self._set(ref, "unavailable", None)
                self.reasons[ref] = (
                    "已申报小计只作核对：在公司自己的行里找不到与历史逐期完全相等的组成项公式")
                continue
            if concept in TOTAL_COST_CONCEPTS and history >= required and revenue:
                # A cost total whose components could not be proven: forecast
                # the total itself as a share of revenue rather than leave the
                # chain below it empty.  Its components are not also terms.
                self._set(ref, "share_of_line", revenue)
                self.reasons[ref] = "已申报费用合计无法逐期由组成项核对，按占收入比预测"
                continue
            if role == "diluted_eps":
                self._set(ref, "unavailable", None)
                self.reasons[ref] = "每股收益只能由净利润 ÷ 稀释股数推导，而历史上这一推导与申报值不一致"
                continue
            recurring_one_off = (
                concept in self.one_off and concept not in ONE_OFF_CONCEPTS
                and line.get("statement", "income") == "income"
                and history >= required and _recurring(self.filed.get(concept)))
            if drafted == "zero_one_off" and recurring_one_off:
                drafted = "unavailable"  # re-decided below: a recurring line is never zeroed
            from .forecast_seasonality import seasonal_profile
            source = self.filed.get(concept)
            profile = seasonal_profile([
                {"period_end": end, "value": value}
                for end, value in filed_values(source).items()
            ]) if source is not None and line.get("period_kind") == "duration" else None
            if profile and profile["significant"] and drafted in {"quarterly_growth", "unavailable"}:
                self._set(ref, "year_over_year_growth", None)
                self.reasons[ref] = "历史季度分布存在稳定季节性，按上年同季×同比增长预测，禁止平均环比外推"
                continue
            if drafted != "unavailable" and self._valid(ref, drafted):
                # A method the drafter chose is kept: the completion fills
                # gaps, it does not overrule a judgement.
                if drafted == "carry_forward_mean":
                    self.reasons[ref] = "延续：按过去四季平均值"
                elif drafted == "zero_one_off":
                    self.reasons[ref] = "一次性项目，预测期取零"
                continue
            if concept in self.one_off and line.get("statement", "income") == "income":
                if (concept not in ONE_OFF_CONCEPTS and history >= required
                        and _recurring(self.filed.get(concept))):
                    # 2026-10-09 (P0): a line the spec calls one-off that is
                    # booked every quarter (META's G&A, carrying a legal
                    # accrual) is a recurring cost with a one-off in it: it
                    # is forecast by its role, on a base the assumptions
                    # normalise (``forecast_coverage_assumptions.normalise_base``),
                    # never held at zero and never compounded from the spike.
                    method, base = self._method_for(role, ref, revenue=revenue, pretax=pretax,
                                                    net=net, cash=cash, debt=debt)
                    self._set(ref, method, base)
                    self.reasons[ref] = ("规格标注含一次性项目，但这一行每季都有发生额：按常规方法预测，"
                                         "基期剔除一次性部分")
                    continue
                self._set(ref, "zero_one_off", None)
                self.reasons[ref] = "规格标注为一次性项目，预测期取零"
                continue
            if history < required:
                self._set(ref, "unavailable", None)
                self.reasons[ref] = (
                    f"只有 {history} 个季度的申报历史（少于 {required} 个），无法可靠外推")
                continue
            method, base = self._method_for(role, ref, revenue=revenue, pretax=pretax,
                                            net=net, cash=cash, debt=debt)
            self._set(ref, method, base)
            if method == "carry_forward_mean":
                self.reasons[ref] = "延续：按过去四季平均值"
            elif method == "unavailable":
                self.reasons[ref] = "这一类行没有确定性的预测方法"

    def isolate_nonoperating_gains(self) -> None:
        """Hold a material non-operating line at zero instead of extrapolating it.

        2026-10-09 (P0): GOOGL's "other income" carried ~$99B of unrealised
        securities gains; carried forward as a trailing mean it put $38B a
        quarter of gains into every forecast quarter's net income.  A
        non-operating line whose trailing four-quarter mean magnitude exceeds
        ``NONOPERATING_MATERIAL_SHARE`` of revenue is investment gains and
        losses, not earnings power: it is forecast at zero, with the reason.
        Small, steady lines (interest on cash) keep their method.
        """

        revenue_refs = self.by_role("revenue")
        if not revenue_refs:
            return
        revenue_source = self.filed.get(str(self.lines[revenue_refs[0]].get("concept")))
        revenue = filed_values(revenue_source) if revenue_source is not None else {}
        for ref in self.order:
            line = self.lines[ref]
            if (line.get("kind") != "filed" or line.get("statement", "income") != "income"
                    or line.get("role") not in NONOPERATING_GAIN_ROLES
                    or line.get("forecast_method") in {"zero_one_off", "unavailable"}):
                continue
            source = self.filed.get(str(line.get("concept")))
            if source is None:
                continue
            values = filed_values(source)
            ends = [end for end in sorted(values) if end in revenue][-TRAILING_QUARTERS:]
            if len(ends) < TRAILING_QUARTERS:
                continue
            magnitude = sum((abs(values[end]) for end in ends), Decimal(0))
            sales = sum((revenue[end] for end in ends), Decimal(0))
            if sales > 0 and magnitude > sales * NONOPERATING_MATERIAL_SHARE:
                self._set(ref, "zero_one_off", None)
                self.reasons[ref] = (
                    f"营业外损益过去四季平均占收入 {(magnitude / sales * 100).quantize(Decimal('0.1'))}%，"
                    "含证券投资浮盈/浮亏等不可外推项目：预测期取零，核心利润不含这部分")

    def _method_for(self, role: str, ref: str, *, revenue: str | None, pretax: str | None,
                    net: str | None, cash: str | None, debt: str | None
                    ) -> tuple[str, str | None]:
        if role == "revenue":
            return "quarterly_growth", None
        if role == "diluted_weighted_average_shares":
            return "quarterly_growth", None
        if role in {"cost_of_revenue", "operating_expense", "company_presented_component"}:
            return ("share_of_line", revenue) if revenue else ("carry_forward_mean", None)
        if role == "income_tax_expense":
            return ("share_of_line", pretax) if pretax else ("carry_forward_mean", None)
        if role == "noncontrolling_interest":
            return ("share_of_line", net) if net else ("carry_forward_mean", None)
        if role == "interest_income" and cash is not None:
            return "rate_times_balance", cash
        if role == "interest_expense" and debt is not None:
            return "rate_times_balance", debt
        if role == "depreciation_amortization":
            return "unavailable", None
        if role in CASH_LEAF_ROLES:
            return "carry_forward_mean", None
        if role == "cash_and_equivalents":
            return "roll_forward", None
        if role in {"debt_current", "debt_noncurrent"}:
            return "hold_last", None
        if role in {"accounts_receivable", "accounts_payable", "deferred_revenue"}:
            return ("share_of_line", revenue) if revenue else ("hold_last", None)
        return "carry_forward_mean", None

    def _valid(self, ref: str, method: str) -> bool:
        line = self.lines[ref]
        base = line.get("forecast_base_ref")
        if method in {"share_of_line", "rate_times_balance"}:
            return base in self.lines and base != ref
        return True

    def _set(self, ref: str, method: str, base: str | None) -> None:
        line = self.lines[ref]
        if line.get("forecast_method") == method and line.get("forecast_base_ref") == base:
            return
        line["forecast_method"] = method
        line["forecast_base_ref"] = base
        if self.sources.get(ref) == "drafted":
            self.sources[ref] = "completed"

    def shares_annual(self) -> None:
        """Annual EPS needs day-weighted annual shares; enable it only when proven.

        The structure replay accepts ``day_weighted_quarters`` only after four
        quarter averages tie to a direct annual filing, so the completion asks
        the same question first and never selects a method the replay would
        then reject (which would block the whole forecast).
        """

        from .company_financial_statement_structure import _historical_annual_share_replay

        for ref in self.by_role("diluted_weighted_average_shares"):
            line = self.lines[ref]
            if line.get("annual_forecast_method") is None:
                line["annual_forecast_method"] = "unavailable"
            source = self.filed.get(str(line.get("concept")))
            if (line.get("kind") == "filed" and source is not None
                    and line.get("annual_forecast_method") == "unavailable"
                    and line.get("forecast_method") in {"quarterly_growth", "year_over_year_growth", "seasonal_factor", "share_of_line"}
                    and _historical_annual_share_replay(source)["status"] == "validated"):
                line["annual_forecast_method"] = "day_weighted_quarters"
                self.repairs.append({
                    "kind": "annual_shares", "line_ref": ref,
                    "detail": "annual diluted shares day-weighted from quarters; the filed "
                              "annual count ties", "tie_out_concept": None})
            elif (line.get("kind") == "filed" and source is not None
                    and line.get("annual_forecast_method") == "unavailable"):
                from .annual_share_forecast import annual_share_source
                evidence = annual_share_source(source)
                if evidence["status"] == "validated":
                    line["annual_forecast_method"] = "annual_growth_from_filed"
                    self.repairs.append({"kind":"annual_shares", "line_ref":ref,
                        "detail":"Annual diluted shares forecast independently from direct consecutive annual filings; no quarterly-average identity is asserted.", "tie_out_concept":None})

    def coverage(self) -> list[dict[str, Any]]:
        out = []
        for ref in self.order:
            line = self.lines[ref]
            method = str(line.get("forecast_method"))
            history = (_history_count(line, self.filed) if line.get("kind") == "filed"
                       else None)
            out.append({
                "line_ref": ref,
                "statement": str(line.get("statement") or "income"),
                "method": method,
                "history_quarters": history,
                "source": self.sources.get(ref, "drafted"),
                "reason": self.reasons.get(ref),
            })
        return out

    def run(self) -> dict[str, Any]:
        self.repair_company_totals()
        self.repair_earnings_totals()
        self.add_eps_chain()
        self.add_statement_leaves()
        self.complete_methods()
        self.isolate_nonoperating_gains()
        self.shares_annual()
        lines = [self.lines[ref] for ref in self.order]
        for line in lines:
            line.setdefault("statement", "income")
            line.setdefault("annual_forecast_method", None)
        return {
            "schema_version": "0.7" if self.definition.get("schema_version") == "0.7" else COVERAGE_SCHEMA_VERSION,
            "lines": lines,
            "formulas": [self.formulas[ref] for ref in self.formula_order
                         if ref in self.formulas],
            **({"note_evidence": self.definition["note_evidence"]}
               if "note_evidence" in self.definition else {}),
            "auto_repairs": self.repairs,
            "coverage": self.coverage(),
            **({"income_depreciation_forecast_policy": self.income_depreciation_policy}
               if self.income_depreciation_policy is not None else {}),
        }


def complete_structure_definition(
    definition: Mapping[str, Any], *, company_spec: Mapping[str, Any],
    financial_inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """The 0.6 definition: methods completed, subtotals and EPS repaired.

    Pure and deterministic over (definition, spec, filed inputs).  The result
    carries ``auto_repairs`` (what changed and why) and ``coverage`` (each
    line's method, history depth and, when not forecastable, the reason).
    """

    if definition.get("schema_version") not in UPGRADEABLE_STRUCTURE_VERSIONS:
        raise ValueError("statement structure version cannot be completed")
    base = dict(definition)
    base.pop("auto_repairs", None)
    base.pop("coverage", None)
    return _Completion(base, company_spec, financial_inputs).run()


def complete_methods_by_role(proposal: Mapping[str, Any],
                             spec_one_off: Iterable[str] = ()) -> dict[str, Any]:
    """Proposal-time completion: methods by role, no history available yet.

    The drafter's proposal is validated before any filed number is bound, so
    this fills only what the role decides; the history-dependent repairs run
    when the structure is materialized against the filings.
    """

    body = copy.deepcopy(dict(proposal))
    lines = [dict(item) for item in body.get("lines") or [] if isinstance(item, Mapping)]
    roles: dict[str, list[str]] = {}
    for line in lines:
        roles.setdefault(str(line.get("role")), []).append(str(line.get("ref")))
    one_off = set(spec_one_off) | set(ONE_OFF_CONCEPTS)

    def first(*names: str) -> str | None:
        for name in names:
            if roles.get(name):
                return roles[name][0]
        return None

    revenue, pretax, net = first("revenue"), first("pretax_income"), first("net_income")
    cash = first("cash_and_equivalents")
    debt = first("debt_noncurrent", "debt_current")
    for line in lines:
        if line.get("kind") != "filed" or line.get("forecast_method") != "unavailable":
            continue
        role = str(line.get("role"))
        if body.get("schema_version") == "0.7" and line.get("statement") == "cash" and line.get("annual_semantics") == "direct_annual":
            # Annual evidence is not permission to create a quarterly method.
            # Materialization retains the declaration and reports quarter gaps.
            continue
        if role in EARNINGS_TOTAL_ROLES or role in {"diluted_eps", "diluted_eps_numerator"}:
            continue
        if str(line.get("concept")) in TOTAL_COST_CONCEPTS:
            continue
        if line.get("statement", "income") == "income" and str(line.get("concept")) in one_off:
            line["forecast_method"], line["forecast_base_ref"] = "zero_one_off", None
            continue
        completion = _Completion({"lines": [], "formulas": []}, {}, {})
        method, base = completion._method_for(
            role, str(line.get("ref")), revenue=revenue, pretax=pretax, net=net,
            cash=cash, debt=debt)
        if role == "company_presented_component" and str(line.get("ref")) in {
            str(term.get("line_ref"))
            for formula in body.get("formulas") or [] if isinstance(formula, Mapping)
            for term in formula.get("terms") or [] if isinstance(term, Mapping)
        }:
            # Possibly a filed total used as a term; the materialized repair
            # decides with history whether it is a sum of components.
            method, base = ("share_of_line", revenue) if revenue else (method, base)
        line["forecast_method"], line["forecast_base_ref"] = method, base
    body["lines"] = lines
    return body


def forecast_computability(lines: Sequence[Mapping[str, Any]],
                           formulas: Sequence[Mapping[str, Any]]) -> dict[str, str | None]:
    """For every line: ``None`` when its forecast can be computed, else why not."""

    by_ref = {str(item["ref"]): item for item in lines}
    by_output = {str(item["output_ref"]): item for item in formulas}
    memo: dict[str, str | None] = {}

    def blocked(ref: str, trail: frozenset[str]) -> str | None:
        if ref in memo:
            return memo[ref]
        if ref in trail:
            return "公式存在循环"
        line = by_ref.get(ref)
        if line is None:
            return f"引用了不存在的行 {ref}"
        method = str(line.get("forecast_method"))
        reason: str | None = None
        if method == "unavailable":
            reason = f"{ref} 没有预测方法"
        elif method in {"share_of_line", "rate_times_balance"}:
            inner = blocked(str(line.get("forecast_base_ref")), trail | {ref})
            reason = None if inner is None else f"{ref} 的基数不可预测（{inner}）"
        elif method == "formula":
            formula = by_output.get(ref)
            if formula is None:
                reason = f"{ref} 缺少公式"
            else:
                refs = ([str(item["line_ref"]) for item in formula.get("terms") or []]
                        if formula.get("operator") == "sum" else
                        [str(formula.get("numerator_ref")),
                         str(formula.get("denominator_ref"))])
                for item in refs:
                    inner = blocked(item, trail | {ref})
                    if inner is not None:
                        reason = inner
                        break
        memo[ref] = reason
        return reason

    return {str(item["ref"]): blocked(str(item["ref"]), frozenset()) for item in lines}


__all__ = [
    "COVERAGE_CONCEPTS", "COVERAGE_RULE_REF", "METHOD_LABELS", "MIN_HISTORY_QUARTERS",
    "MIN_NON_ADDITIVE_QUARTERS", "UPGRADEABLE_STRUCTURE_VERSIONS",
    "complete_methods_by_role", "complete_structure_definition", "coverage_concepts",
    "filed_values", "forecast_computability", "method_label",
]
