"""A synthetic company for rule tests: Synthetic Widgets Co. (ticker SYNW).

Not any covered company.  Twelve calendar quarters (2023 Q3 to 2026 Q2) of
filed lines generated from a few parameters, in the exact shape
``company_model_inputs.build_model_inputs`` produces, and a company model
specification whose drafted 0.6 structure the coverage completion runs on.

The parameters exist to put the review's error patterns into a company that
has never been modelled: a general-and-administrative spike the spec calls
one-off, research spending growing far faster than revenue, a share count
falling faster than its buybacks can fund, and a non-operating line full of
securities gains.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

COMPANY = "company:ticker:synw"
QUARTERS = [
    ("2023-07-01", "2023-09-30"), ("2023-10-01", "2023-12-31"),
    ("2024-01-01", "2024-03-31"), ("2024-04-01", "2024-06-30"),
    ("2024-07-01", "2024-09-30"), ("2024-10-01", "2024-12-31"),
    ("2025-01-01", "2025-03-31"), ("2025-04-01", "2025-06-30"),
    ("2025-07-01", "2025-09-30"), ("2025-10-01", "2025-12-31"),
    ("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-06-30"),
]
PRETAX = "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"
ACCESSION = "0009999999-26-000001"


def _cells(values: list[Decimal], *, instant: bool = False) -> dict[str, Any]:
    out = {}
    for (start, end), value in zip(QUARTERS, values):
        out[end] = {"basis": "reported", "period_start": None if instant else start,
                    "source_accessions": [ACCESSION], "unit": "usd" if not instant else "usd",
                    "value": format(value.quantize(Decimal(1)), "f")}
    return out


def _line(concept: str, label: str, values: list[Decimal], *, statement: str = "income",
          instant: bool = False, unit: str = "usd") -> dict[str, Any]:
    cells = _cells(values, instant=instant)
    for cell in cells.values():
        cell["unit"] = unit
    return {"ambiguous_periods": [], "cells": cells, "concept": concept, "derived_count": 0,
            "drawn_on_by": [], "duration_facts": [], "gaps": [], "is_split": False,
            "label": label, "period_basis": "instant" if instant else "duration",
            "statement": statement, "status": "filed"}


def _series(concept: str, role: str, values: list[Decimal]) -> dict[str, Any]:
    return {"concept": concept, "gaps": [], "role": role, "statement": "cash", "status": "filed",
            "unit": "usd",
            "series": {"ambiguous_periods": [], "cumulative_used": 0, "derived_count": 0,
                       "instants": [], "unclassified_periods": [],
                       "durations": [{"period_end": end, "period_kind": "quarter",
                                      "period_start": start, "source_accessions": [ACCESSION],
                                      "source_forms": ["10-Q"], "unit": "usd",
                                      "value": format(value.quantize(Decimal(1)), "f")}
                                     for (start, end), value in zip(QUARTERS, values)],
                       "quarters": [{"basis": "reported", "period_end": end,
                                     "period_start": start, "source_accessions": [ACCESSION],
                                     "source_forms": ["10-Q"], "unit": "usd",
                                     "value": format(value.quantize(Decimal(1)), "f")}
                                    for (start, end), value in zip(QUARTERS, values)]}}


def company(*, rnd_growth: str = "0.02", gna_spike: str | None = None,
            share_shrink: str = "0.005", buyback: str = "100000000",
            recent_buyback: str | None = None,
            nonoperating: str = "50000000") -> dict[str, Any]:
    """``{"spec", "table"}`` for Synthetic Widgets Co. with these patterns."""

    n = len(QUARTERS)

    def whole(values):
        return [value.quantize(Decimal(1)) for value in values]

    revenue = whole([Decimal(10_000_000_000) * Decimal("1.02") ** i for i in range(n)])
    cost = whole([value * Decimal("0.40") for value in revenue])
    rnd = whole([Decimal(800_000_000) * (1 + Decimal(rnd_growth)) ** i for i in range(n)])
    gna = whole([Decimal(1_000_000_000) * Decimal("1.01") ** i for i in range(n)])
    if gna_spike is not None:
        gna[-1] = gna[-1] + Decimal(gna_spike)
    operating = [r - c - d - g for r, c, d, g in zip(revenue, cost, rnd, gna)]
    nonop = [Decimal(nonoperating) * (1 + (i % 3)) for i in range(n)]
    pretax = [o + x for o, x in zip(operating, nonop)]
    tax = whole([p * Decimal("0.20") for p in pretax])
    net = [p - t for p, t in zip(pretax, tax)]
    shares = whole([Decimal(500_000_000) * (1 - Decimal(share_shrink)) ** i for i in range(n)])
    eps = [m / s for m, s in zip(net, shares)]
    buybacks = [Decimal(buyback) for _ in range(n)]
    if recent_buyback is not None:
        buybacks[-4:] = [Decimal(recent_buyback)] * 4
    dna = whole([value * Decimal("0.05") for value in revenue])
    receivables = whole([value * Decimal("0.30") for value in revenue])
    ocf = [m + d for m, d in zip(net, dna)]
    capex = whole([value * Decimal("0.06") for value in revenue])
    eps_line = _line("us-gaap:EarningsPerShareDiluted", "Diluted EPS",
                     [value.quantize(Decimal("0.01")) for value in eps], unit="usd_per_share")
    for (start, end), value in zip(QUARTERS, eps):
        eps_line["cells"][end]["value"] = format(value.quantize(Decimal("0.01")), "f")
    share_line = _line("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
                       "Diluted shares", shares, unit="shares")
    filed = [
        _line("us-gaap:Revenues", "Revenue", revenue),
        _line("us-gaap:CostOfRevenue", "Cost of revenue", cost),
        _line("us-gaap:ResearchAndDevelopmentExpense", "Research and development", rnd),
        _line("us-gaap:GeneralAndAdministrativeExpense", "General and administrative", gna),
        _line("us-gaap:OperatingIncomeLoss", "Operating income", operating),
        _line("us-gaap:NonoperatingIncomeExpense", "Other income (expense), net", nonop),
        _line(PRETAX, "Income before taxes", pretax),
        _line("us-gaap:IncomeTaxExpenseBenefit", "Income taxes", tax),
        _line("us-gaap:NetIncomeLoss", "Net income", net),
        share_line, eps_line,
        _line("us-gaap:PaymentsForRepurchaseOfCommonStock", "Repurchases", buybacks,
              statement="cash"),
        _line("us-gaap:DepreciationDepletionAndAmortization", "Depreciation and amortization",
              dna, statement="cash"),
        _line("us-gaap:AccountsReceivableNetCurrent", "Accounts receivable", receivables,
              statement="balance", instant=True),
    ]
    table = {
        "schema_version": "0.3", "company_ref": COMPANY, "spec_ref": "company-model-spec:synw",
        "state_hash": "0" * 64, "periods": [end for _start, end in QUARTERS[-8:]],
        "filed_lines": sorted(filed, key=lambda item: item["concept"]),
        "cash_flow_inputs": [
            _series("us-gaap:NetCashProvidedByUsedInOperatingActivities", "operating_cash_flow", ocf),
            _series("us-gaap:PaymentsToAcquirePropertyPlantAndEquipment", "capital_expenditure", capex),
        ],
    }

    def line(ref: str, concept: str | None, role: str, method: str, base: str | None = None,
             kind: str = "filed") -> dict[str, Any]:
        return {"annual_forecast_method": None, "annual_semantics": "sum_quarters",
                "concept": concept, "forecast_base_ref": base, "forecast_method": method,
                "kind": kind, "label": ref, "period_kind": "duration", "ref": ref, "role": role,
                "statement": "income", "unit": "usd"}

    def term(ref: str, coefficient: str) -> dict[str, str]:
        return {"line_ref": ref, "coefficient": coefficient}

    structure = {
        "schema_version": "0.6",
        "lines": [
            line("revenue", "us-gaap:Revenues", "revenue", "quarterly_growth"),
            line("cost", "us-gaap:CostOfRevenue", "cost_of_revenue", "share_of_line", "revenue"),
            line("rnd", "us-gaap:ResearchAndDevelopmentExpense", "operating_expense",
                 "quarterly_growth"),
            line("g-and-a", "us-gaap:GeneralAndAdministrativeExpense", "operating_expense",
                 "quarterly_growth"),
            line("operating-income", None, "operating_income", "formula", kind="derived"),
            line("nonoperating", "us-gaap:NonoperatingIncomeExpense",
                 "nonoperating_income_expense", "unavailable"),
            line("pretax-income", None, "pretax_income", "formula", kind="derived"),
            line("income-tax", "us-gaap:IncomeTaxExpenseBenefit", "income_tax_expense",
                 "share_of_line", "pretax-income"),
            line("net-income", None, "net_income", "formula", kind="derived"),
        ],
        "formulas": [
            {"operator": "sum", "output_ref": "operating-income", "evidence_refs": [ACCESSION],
             "tie_out_concept": "us-gaap:OperatingIncomeLoss",
             "terms": [term("revenue", "1"), term("cost", "-1"), term("rnd", "-1"),
                       term("g-and-a", "-1")]},
            {"operator": "sum", "output_ref": "pretax-income", "evidence_refs": [ACCESSION],
             "tie_out_concept": PRETAX,
             "terms": [term("operating-income", "1"), term("nonoperating", "1")]},
            {"operator": "sum", "output_ref": "net-income", "evidence_refs": [ACCESSION],
             "tie_out_concept": "us-gaap:NetIncomeLoss",
             "terms": [term("pretax-income", "1"), term("income-tax", "-1")]},
        ],
    }
    expense = [
        {"basis_concept": "us-gaap:CostOfRevenue", "because": "serving cost",
         "behaviour": "variable_with_revenue", "label": "Cost of revenue", "ref": "cost"},
        {"basis_concept": "us-gaap:ResearchAndDevelopmentExpense", "because": "engineers",
         "behaviour": "variable_with_headcount", "label": "R&D", "ref": "rnd"},
        {"basis_concept": "us-gaap:GeneralAndAdministrativeExpense",
         "because": "G&A jumped in the latest quarter on a legal accrual",
         "behaviour": "one_off" if gna_spike is not None else "fixed",
         "label": "G&A", "ref": "g-and-a"},
    ]
    spec = {
        "schema_version": "0.4", "spec_id": "company-model-spec:synw", "company_ref": COMPANY,
        "content_hash": "1" * 64, "decided_by": "automation:test", "state_hash": "0" * 64,
        "task_hash": "2" * 64, "revenue_anchor_concept": "us-gaap:Revenues",
        "expense_lines": expense,
        "horizon": {"because": "synthetic", "forecast_quarters": 8, "historical_quarters": 8},
        "forecast_statements": [
            {"because": "synthetic", "importance": "required", "statement": "income"},
            {"because": "synthetic", "importance": "supporting", "statement": "balance"},
            {"because": "synthetic", "importance": "required", "statement": "cash"},
        ],
        "financial_statement_structure": structure,
        "cash_flow_companion": {
            "schema_version": "0.1",
            "formula": {"operator": "sum", "output_ref": "free_cash_flow", "terms": [
                {"coefficient": "1", "role": "operating_cash_flow"},
                {"coefficient": "-1", "role": "capital_expenditure"}]},
            "lines": [
                {"because": "synthetic", "concept": "us-gaap:NetCashProvidedByUsedInOperatingActivities",
                 "forecast_base_ref": "revenue", "forecast_method": "share_of_line",
                 "role": "operating_cash_flow"},
                {"because": "synthetic", "concept": "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
                 "forecast_base_ref": "revenue", "forecast_method": "share_of_line",
                 "role": "capital_expenditure"},
            ],
        },
    }
    return {"spec": spec, "table": copy.deepcopy(table)}


# ---------------------------------------------------------------------------
# Batch D: a synthetic material pack and an STRL-shaped draft for SYNW
#
# The pack is batch C's own ``material_pack.MaterialPack`` with synthetic
# content: ``tags`` maps ``P…`` to ``doc-passage:`` refs (the passage text in
# ``text`` as ``evidence_index.render_passage_block`` prints it), ``N…`` to
# ``pack-json:/…`` pointers resolved through ``json["number_tags"]`` (our
# model's figures under ``/our_model``), ``C…`` to a Claim and ``O…`` to our
# earlier view (background, never a source).  The draft is what the two-step
# drafter should produce from it: every figure verbatim from a tag, a
# declared arithmetic, or 「我们的估计」 bound to our model.
# ---------------------------------------------------------------------------

PACK_AS_OF = "2026-08-15"


def _passage(document_ref: str, text: str) -> str:
    from dalton_core.evidence_index import make_passage_ref

    return make_passage_ref(document_ref, 0, len(text), text)


#: tag -> (document_ref, evidence_corpus kind, date, title, text)
PASSAGES = {
    "P1": ("doc:synw-q2-2026-call", "transcript", "2026-07-30", "SYNW Q2 2026 earnings call",
           "CFO: Second-quarter revenue was $1,250 million, up 8% year over year, and operating "
           "margin was 21.0%. Services were 60% of revenue and 75% of operating profit. Backlog "
           "ended the quarter at $4,800 million, and historically about 45% of backlog converts "
           "to revenue within 12 months."),
    "P2": ("doc:synw-q2-2026-release", "sec_exhibit", "2026-07-29", "SYNW Q2 2026 results release",
           "Synthetic Widgets Co. reported second-quarter 2026 diluted EPS of $1.05 and raised "
           "full-year 2026 revenue guidance to $5,000 million to $5,100 million."),
    "P3": ("doc:synw-10k-2025", "sec_10k", "2026-02-20", "SYNW 10-K 2025",
           "Services contracts run 36 months on average and renewal rates were 92% in 2025. "
           "Hardware units sell at an average price of $1,200, with gross margin of 28%."),
    "P4": ("doc:brokera-synw-2026-08-05", "broker_report", "2026-08-05", "BrokerA update",
           "BrokerA maintains a price target of $95 and estimates FY2027 EPS of $4.70."),
    "P5": ("doc:widget-industry-2026", "web_page", "2026-06-30", "Widget services market review",
           "The widget services market is about $60,000 million and grows 6% a year; the three "
           "largest vendors hold 35% combined share and the company holds 4%."),
    "P6": ("doc:synw-q1-2026-call", "transcript", "2026-04-30", "SYNW Q1 2026 earnings call",
           "CEO: we won 14 new large accounts this quarter."),
}
#: tag -> (pointer parts under the pack json, value, display)
NUMBER_TAGS = {
    "N1": (("numbers", "consensus", "periods", 0, "revenue", "mean"), 4950, "4,950"),
    "N2": (("numbers", "consensus", "periods", 0, "eps", "mean"), "4.00", "4.00"),
    "N3": (("numbers", "consensus", "periods", 1, "revenue", "mean"), 5400, "5,400"),
    "N4": (("numbers", "consensus", "periods", 1, "eps", "mean"), "4.50", "4.50"),
    "N5": (("numbers", "market", "price", "close"), "80.00", "80.00"),
    "N6": (("numbers", "market", "adv_30d", "value"), 45000000, "45.0"),
    "N7": (("numbers", "market", "range_52w", "low"), "62.00", "62.00"),
    "N8": (("numbers", "market", "range_52w", "high"), "88.00", "88.00"),
    "N9": (("numbers", "valuation", "long_pe", "median"), "16.0", "16.0"),
    "N10": (("numbers", "valuation", "long_pe", "percentile"), 70, "70"),
    "N11": (("numbers", "peers", "median_pe"), "18.0", "18.0"),
    "N12": (("numbers", "consensus", "target", "mean"), "92.00", "92.00"),
    "N13": (("numbers", "consensus", "target", "high"), "110.00", "110.00"),
    "N14": (("numbers", "consensus", "target", "low"), "75.00", "75.00"),
    "N15": (("numbers", "market", "earnings_reaction", "mean_abs_percent"), "6.5", "6.5"),
    "N16": (("numbers", "market", "implied_vol", "percent"), 38, "38"),
    "N17": (("our_model", "periods", 0, "revenue"), 5050, "5,050"),
    "N18": (("our_model", "periods", 0, "eps"), "4.20", "4.20"),
    "N19": (("our_model", "periods", 1, "revenue"), 5700, "5,700"),
    "N20": (("our_model", "periods", 1, "eps"), "5.00", "5.00"),
}
CLAIMS = {
    "C1": ("claim-version:" + "c" * 64, "Management said the company keeps gaining share in large accounts."),
}
OUR_VIEWS = {
    "O1": ("mission-deliverable-version:synw-prior", "上一版初筛（2026-05）：立场 中性"),
}


def _set(root: dict[str, Any], parts: tuple[Any, ...], value: Any) -> None:
    node: Any = root
    for index, part in enumerate(parts[:-1]):
        nxt = parts[index + 1]
        if isinstance(node, list):
            while len(node) <= part:
                node.append({} if not isinstance(nxt, int) else [])
            node = node[part]
        else:
            node = node.setdefault(part, [] if isinstance(nxt, int) else {})
    if isinstance(node, list):
        while len(node) <= parts[-1]:
            node.append({})
        node[parts[-1]] = value
    else:
        node[parts[-1]] = value


def material_pack(*, sufficiency: str = "sufficient", drop: tuple[str, ...] = (),
                  company: str = COMPANY) -> Any:
    """A ``material_pack.MaterialPack`` for SYNW: batch C's object, synthetic content."""

    from dalton_core.evidence_corpus import KIND_LABELS
    from dalton_core.material_pack import MaterialPack, pointer
    from dalton_core.store import content_hash

    tags: dict[str, str] = {}
    data: dict[str, Any] = {"numbers": {"consensus": {"periods": [{"label": "FY2026"}, {"label": "FY2027"}]}},
                            "our_model": {"periods": [{"label": "FY2026"}, {"label": "FY2027"}]}}
    data["our_model"].update(model={"version_ref": "forecast-model-version:synthetic-held:1"},
                             units={"revenue": "USDmillion", "eps": "USD/shares"}, eps_basis="gaap")
    data["numbers"]["consensus"]["eps_basis"] = {"vendor": "gaap"}
    data["numbers"]["consensus"]["units"] = {"revenue": "USDmillion", "eps": "USD/shares"}
    number_tags: dict[str, Any] = {}
    for tag, (parts, value, display) in NUMBER_TAGS.items():
        _set(data, parts, value)
        path = pointer(*parts)
        tags[tag] = "pack-json:" + path
        number_tags[tag] = {"pointer": path, "value": value, "display": display}
    data["number_tags"] = number_tags
    lines = [f"# 材料包 · SYNW · initial_screen · 截至 {PACK_AS_OF} · initial_screen:synthetic",
             "标签：[P…] 原文段落；[N…] 数字包字段；[C…] 旧结论；[O…] 我们的看法（非证据）。",
             "", "## 1. 原文段落"]
    inputs = []
    for tag, (doc, kind, day, title, text) in PASSAGES.items():
        tags[tag] = _passage(doc, text)
        lines += [f"[{tag}] · {KIND_LABELS[kind]} · {day} · 《{title}》", text, ""]
        inputs.append({"kind": "document", "ref": doc, "date": day, "doc_kind": kind})
    lines.append("## 2. 数字")
    lines.append(f"一致预期 FY2026：收入 [N1] 4,950（百万美元），EPS [N2] 4.00 美元；FY2027：收入 [N3] 5,400，"
                 f"EPS [N4] 4.50 美元（12 家券商）；目标价均值 [N12] 92.00，高 [N13] 110.00，低 [N14] 75.00 美元")
    lines.append("行情：收盘 [N5] 80.00 美元（2026-08-14）；30日ADV [N6] 45.0m美元；52周 [N7] 62.00–[N8] 88.00；"
                 "财报次日平均绝对涨跌 [N15] 6.5%；隐含波动率 [N16] 38%")
    lines.append("估值：过去 5 年前瞻 P/E 中位数 [N9] 16.0x，当前位于第 [N10] 70 百分位；同业前瞻 P/E 中位数 [N11] 18.0x")
    lines.append("我们的模型：FY2026 收入 [N17] 5,050（百万美元），EPS [N18] 4.20 美元；FY2027 收入 [N19] 5,700，"
                 "EPS [N20] 5.00 美元")
    lines += ["", "## 3. 旧结论与我们的看法"]
    for tag, (ref, text) in CLAIMS.items():
        tags[tag] = ref
        lines.append(f"[{tag}] {text}")
    for tag, (ref, text) in OUR_VIEWS.items():
        tags[tag] = ref
        lines.append(f"[{tag}] {text}")
    for tag in drop:
        tags.pop(tag, None)
    text = "\n".join(lines) + "\n"
    ok = sufficiency == "sufficient"
    suff = {"status": sufficiency, "drafting_allowed": ok,
            "reason": None if ok else "缺最新季度电话会纪要",
            "missing": [] if ok else [{"key": "latest_transcript", "request": {"id": "material-request:synw-call"}}],
            "wait_until": None if ok else "2026-08-16T00:00:00+00:00",
            "gap_request_refs": [] if ok else ["material-request:synw-call"]}
    pack = MaterialPack(
        company_ref=company, product="initial_screen", as_of=PACK_AS_OF,
        recipe_version="initial_screen:synthetic", text=text, tags=tags, json=data,
        sufficiency=suff, budget={"passages": {"limit": 200_000, "used": len(text), "items": 6, "dropped": 0}},
        inputs=inputs, watermark={"as_of": PACK_AS_OF, "synthetic": True}, built_at="2026-08-15T00:00:00+00:00")
    pack.content_hash = content_hash({"text": text, "tags": tags})
    return pack


def strl_worksheet() -> dict[str, Any]:
    return {
        "stance": "看多",
        "core_judgement": "一致预期低估了服务业务的经营杠杆，FY2027 EPS 差异来自利润率假设。",
        "watch_price": {
            "status": "given", "value": "85.0", "currency": "USD", "multiple": "17.0",
            "multiple_basis": "FY2027 前瞻 P/E", "eps": "5.00", "eps_tag": "N20",
            "expression": "17.0*5.00", "current_price": "80.00", "current_price_tag": "N5",
            "implied_return_pct": "6.3", "implied_expression": "(85.0/80.00-1)*100",
            "reference_tags": ["N12", "P4"],
        },
        "multiple_rationale": {"history": "过去 5 年中位数 16.0x", "peers": "同业中位数 18.0x",
                               "business": "多年期服务合同、续约率高"},
        "forecasts": [
            {"year": "FY2026", "metric": "revenue", "ours": "5050", "ours_tag": "N17",
             "consensus": "4950", "consensus_tag": "N1", "diff_pct": "2.0",
             "diff_expression": "(5050/4950-1)*100", "driver": "下半年服务收入延续第二季度增速"},
            {"year": "FY2026", "metric": "eps", "ours": "4.20", "ours_tag": "N18",
             "consensus": "4.00", "consensus_tag": "N2", "diff_pct": "5.0",
             "diff_expression": "(4.20/4.00-1)*100", "driver": "服务占比上升带动利润率"},
            {"year": "FY2027", "metric": "revenue", "ours": "5700", "ours_tag": "N19",
             "consensus": "5400", "consensus_tag": "N3", "diff_pct": "5.6",
             "diff_expression": "(5700/5400-1)*100", "driver": "backlog 转化节奏维持历史水平"},
            {"year": "FY2027", "metric": "eps", "ours": "5.00", "ours_tag": "N20",
             "consensus": "4.50", "consensus_tag": "N4", "diff_pct": "11.1",
             "diff_expression": "(5.00/4.50-1)*100", "driver": "服务占比上升带动营业利润率"},
        ],
        "key_passages": ["P1", "P2", "P3"],
        "trade": {"edge": "EPS 预期差", "crowding": "现价接近 52 周上沿",
                  "earnings_reaction": "财报日波动大", "catalysts": [{"date": "2026-10", "event": "Q3 财报"}]},
        "open_questions": [{"question": "服务分部利润率历史", "affects_conclusion": True,
                            "gap_kind": "not_acquired", "request_ref": "material-request:synw-seg-margin"}],
    }


def _d(expression: str, result: str, text: str, metric: str, period: str, unit: str) -> dict[str, Any]:
    return {"derivation": {"expression": expression, "result": result}, "text": text,
            "metric": metric, "period": period, "unit": unit}


def _n(tag: str, metric: str, period: str, unit: str) -> dict[str, Any]:
    return {"tag": tag, "metric": metric, "period": period, "unit": unit}


def strl_document() -> dict[str, Any]:
    """Step two's reply for SYNW: an STRL-shaped document that should pass."""

    multiple = _d("(16.0+18.0)/2", "17.0", "倍数 17.0x", "前瞻 P/E", "FY2027", "x")
    watch = _d("17.0*5.00", "85.0", "观察价 85.0 美元", "观察价", "12M", "美元")
    implied = _d("(85.0/80.00-1)*100", "6.3", "空间 6.3%", "隐含空间", "12M", "%")
    eps27 = {"estimate": "N20", "text": "我们的估计 FY2027 EPS 5.00 美元", "metric": "EPS",
             "period": "FY2027", "unit": "美元"}
    hist = [_n("N9", "历史前瞻 P/E 中位数", "5Y", "x"), _n("N10", "历史分位", "5Y", "百分位")]
    peers = _n("N11", "同业前瞻 P/E 中位数", "FY2027", "x")
    price = _n("N5", "股价", "2026-08-14", "美元")
    sections = [
        {"key": "conclusion",
         "body": ("立场：看多。观察价 85.0 美元，按 17.0x FY2027 前瞻 P/E 乘以我们的估计 EPS 得出，"
                  "较现价 80.00 美元有 6.3% 的空间。\n\n"
                  "核心判断：一致预期低估了服务业务的经营杠杆。我们预计 FY2027 EPS 高于一致预期 11.1%，"
                  "差异来自服务占比上升带来的利润率改善，收入假设与一致预期接近。证伪条件是服务续约率"
                  "明显低于 2025 年的 92%，或下半年订单转化明显放慢。"),
         "claims": [], "sources": ["N4", "N5", "P3"],
         "numbers": [price, *hist, peers, _n("N4", "一致预期 EPS", "FY2027", "美元"), eps27,
                     {"tag": "P3", "quote": "renewal rates were 92% in 2025", "metric": "续约率",
                      "period": "2025", "unit": "%"},
                     multiple, watch, implied,
                     _d("(5.00/4.50-1)*100", "11.1", "高于一致预期 11.1%", "EPS 差异", "FY2027", "%")]},
        {"key": "business_model",
         "body": ("服务是利润中心：第二季度服务占收入 60%、占经营利润 75%，公司整体营业利润率为 21.0%"
                  "（第二季度电话会）。服务合同平均期限 36 个月，2025 年续约率 92%（年报），收入以多年期"
                  "合同为主。\n\n"
                  "硬件按台销售，平均单价 1,200 美元，毛利率 28%（年报），主要作用是带出后续服务合同。\n\n"
                  "季末 backlog 为 4,800 百万美元，历史上约 45% 在 12 个月内转化为收入（第二季度电话会），"
                  "按此节奏推算未来 12 个月可转化约 2,160 百万美元。"),
         "claims": [], "sources": ["P1", "P3"],
         "numbers": [{"tag": "P1", "quote": "Services were 60% of revenue and 75% of operating profit",
                      "metric": "服务收入与经营利润占比", "period": "FY2026Q2", "unit": "%"},
                     {"tag": "P1", "quote": "operating margin was 21.0%", "metric": "营业利润率",
                      "period": "FY2026Q2", "unit": "%"},
                     {"tag": "P3", "quote": "Services contracts run 36 months on average and renewal rates were 92% in 2025",
                      "metric": "合同期限与续约率", "period": "2025", "unit": "月/%"},
                     {"tag": "P3", "quote": "Hardware units sell at an average price of $1,200, with gross margin of 28%",
                      "metric": "硬件单价与毛利率", "period": "2025", "unit": "美元/%"},
                     {"tag": "P1", "quote": "Backlog ended the quarter at $4,800 million, and historically about 45% of backlog converts to revenue within 12 months",
                      "metric": "backlog 与转化率", "period": "FY2026Q2", "unit": "百万美元/%"},
                     _d("4800*45/100", "2160", "约 2,160 百万美元", "12 个月可转化 backlog", "FY2026Q3-FY2027Q2", "百万美元")]},
        {"key": "industry",
         "body": ("行业规模约 60,000 百万美元，年增速 6%，前三大厂商合计份额 35%，公司份额 4%（行业报告）。"
                  "市场分散，公司靠多年期服务合同与客户粘性守住份额。\n\n"
                  "管理层称公司正在大客户中赢得份额，目前没有独立的份额数据可以核实这一点，"
                  "我们只把它当作公司观点。"),
         "claims": ["C1"], "sources": ["P5"],
         "numbers": [{"tag": "P5", "quote": "The widget services market is about $60,000 million and grows 6% a year; the three largest vendors hold 35% combined share and the company holds 4%",
                      "metric": "市场规模、增速与份额", "period": "2026", "unit": "百万美元/%"}]},
        {"key": "forecast_vs_consensus",
         "body": ("我们预计 FY2026 收入 5,050 百万美元、EPS 4.20 美元，分别高于一致预期（12 家券商）的 "
                  "4,950 百万美元和 4.00 美元 2.0% 与 5.0%。\n\n"
                  "我们预计 FY2027 收入 5,700 百万美元、EPS 5.00 美元，比一致预期的 5,400 百万美元和 "
                  "4.50 美元高 5.6% 与 11.1%。差异主要来自利润率假设：我们假设服务占比延续第二季度的水平，"
                  "带动营业利润率高于一致预期隐含的水平。FY2026 收入与公司上调后的 5,000 百万美元至 "
                  "5,100 百万美元指引区间一致（发布稿）。"),
         "claims": [], "sources": ["N1", "N2", "N3", "N4", "P2"],
         "numbers": [{"estimate": "N17", "metric": "收入", "period": "FY2026", "unit": "百万美元"},
                     {"estimate": "N18", "metric": "EPS", "period": "FY2026", "unit": "美元"},
                     {"estimate": "N19", "metric": "收入", "period": "FY2027", "unit": "百万美元"},
                     {"estimate": "N20", "metric": "EPS", "period": "FY2027", "unit": "美元"},
                     _n("N1", "一致预期收入", "FY2026", "百万美元"), _n("N2", "一致预期 EPS", "FY2026", "美元"),
                     _n("N3", "一致预期收入", "FY2027", "百万美元"), _n("N4", "一致预期 EPS", "FY2027", "美元"),
                     {"tag": "P2", "quote": "raised full-year 2026 revenue guidance to $5,000 million to $5,100 million",
                      "metric": "收入指引", "period": "FY2026", "unit": "百万美元"},
                     _d("(5050/4950-1)*100", "2.0", "收入高 2.0%", "收入差异", "FY2026", "%"),
                     _d("(4.20/4.00-1)*100", "5.0", "EPS 高 5.0%", "EPS 差异", "FY2026", "%"),
                     _d("(5700/5400-1)*100", "5.6", "收入高 5.6%", "收入差异", "FY2027", "%"),
                     _d("(5.00/4.50-1)*100", "11.1", "EPS 高 11.1%", "EPS 差异", "FY2027", "%")]},
        {"key": "valuation",
         "body": ("观察价 85.0 美元 = 17.0x × FY2027 EPS 5.00 美元（我们的估计），较现价 80.00 美元有 "
                  "6.3% 的空间。\n\n"
                  "倍数取 17.0x，为历史与同业的中点：过去 5 年前瞻 P/E 中位数为 16.0x，当前位于第 70 百分位；"
                  "同业前瞻 P/E 中位数为 18.0x。业务属性上，服务收入为多年期合同、续约率高，应享有高于硬件"
                  "公司的倍数，但增速有限，倍数取在同业中位数之下。\n\n"
                  "作为参照，一致预期目标价均值为 92.00 美元，区间 75.00 至 110.00 美元；BrokerA 的目标价为 "
                  "95 美元，FY2027 EPS 预测 4.70 美元。我们的观察价低于一致预期目标价，主要因为我们给的倍数更保守。"),
         "claims": [], "sources": ["N5", "N9", "N11", "N12", "P4"],
         "numbers": [dict(eps27), price, *hist, peers,
                     _n("N12", "一致预期目标价均值", "12M", "美元"), _n("N13", "一致预期目标价最高", "12M", "美元"),
                     _n("N14", "一致预期目标价最低", "12M", "美元"),
                     {"tag": "P4", "quote": "BrokerA maintains a price target of $95 and estimates FY2027 EPS of $4.70",
                      "metric": "卖方目标价与 EPS", "period": "FY2027", "unit": "美元"},
                     multiple, watch, implied]},
        {"key": "trading",
         "body": ("预期差：我们的 FY2027 EPS 高于一致预期两位数，若下半年服务利润率兑现，业绩超预期的胜率较高。\n\n"
                  "拥挤度：30 日 ADV 45.0 百万美元，52 周区间 62.00–88.00 美元，现价接近区间上沿，追高的赔率一般。"
                  "财报反应：过去 8 次财报次日平均绝对涨跌幅 6.5%，期权隐含波动率 38%，押注财报的期权并不便宜。\n\n"
                  "催化日历：10 月下旬第三季度财报；明年 2 月年报与 FY2027 指引。"),
         "claims": [], "sources": ["N4", "N6", "N15"],
         "numbers": [_n("N6", "30 日 ADV", "2026-08-14", "百万美元"), _n("N7", "52 周低点", "52W", "美元"),
                     _n("N8", "52 周高点", "52W", "美元"), _n("N15", "财报次日平均绝对涨跌幅", "8Q", "%"),
                     _n("N16", "隐含波动率", "2026-08-14", "%")]},
        {"key": "risks",
         "body": ("需求风险：若大客户推迟 IT 预算，新签订单放慢，backlog 转化会拖累 FY2027 收入，证伪信号是"
                  "连续两个季度 book-to-bill 低于 1。\n\n"
                  "利润率风险：服务交付人力成本上升会压缩利润率，若服务毛利率同比下降超过 2 个百分点，我们的 EPS "
                  "假设不成立。估值风险：若同业倍数回落到历史中位数，观察价对应的倍数也需下调。"),
         "claims": [], "sources": ["P1"], "numbers": []},
        {"key": "tracking",
         "body": ("每月：主要客户的 IT 招标与续约公告（公司官网与客户公告），续约率低于 2025 年水平即警示。\n\n"
                  "每季：服务收入占比与营业利润率（季报），服务占比回落说明利润率假设偏乐观；book-to-bill"
                  "（电话会），低于 1 为负面信号；一致预期 FY2027 EPS 的修正方向（一致预期数据源），向我们的估计"
                  "靠拢说明预期差在兑现。"),
         "claims": [], "sources": ["N4"], "numbers": []},
        {"key": "unknowns",
         "body": ("我们不知道下半年大客户的续约节奏：公司不按客户公布续约数据，已查年报与第二季度电话会。\n\n"
                  "我们也没有取得服务业务的分部利润率历史序列：已发起 material-request:synw-seg-margin，"
                  "已尝试：年报分部附注、第二季度电话会、BrokerA 研报，均只有收入口径。"
                  "这两点决定服务利润率假设的可信度。"),
         "claims": [], "sources": [], "numbers": [],
         "gap_items": [{"kind": "company_not_disclosed", "need": "按客户的续约数据"},
                       {"kind": "not_acquired", "need": "服务业务分部利润率历史序列",
                        "request_ref": "material-request:synw-seg-margin",
                        "tried": "年报分部附注、第二季度电话会、BrokerA 研报"}]},
    ]
    return {"sections": sections}
