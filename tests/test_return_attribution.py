"""ADR-0014: income bridges, per-share series, return attribution, expectation sources."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from dalton_core.store import DaltonStore, content_hash

AUTOMATION = "automation:coverage-mission"
CO = "company:ticker:acme"
MILLION = 1_000_000

STATEMENT_DDL = """
CREATE TABLE IF NOT EXISTS coverage_mission_statement_filings (
    ingest_id TEXT PRIMARY KEY, company_ref TEXT, accession TEXT, form TEXT, filed TEXT,
    report_date TEXT);
CREATE TABLE IF NOT EXISTS coverage_mission_statement_lines (
    line_id TEXT PRIMARY KEY, ingest_id TEXT, statement TEXT, ordinal INTEGER, concept TEXT,
    label TEXT, level INTEGER DEFAULT 1, parent_concept TEXT, is_breakdown INTEGER DEFAULT 0,
    dimension_axis TEXT, dimension_member TEXT, period_start TEXT, period_end TEXT,
    value TEXT, unit TEXT, balance TEXT);
CREATE TABLE IF NOT EXISTS coverage_mission_pointer (mission_ref TEXT, mission_version_id TEXT);
"""
PRETAX = "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"


def mission(scopes=("valuation",), universe=(CO,)) -> dict:
    return {"id": "coverage-mission-version:test:1", "industry_ref": "industry:test",
            "autonomy": {"automation_principal": AUTOMATION, "may_write": list(scopes)},
            "universe": [{"company_ref": ref} for ref in universe]}


def period_lines(start: str, end: str, *, oi: int, interest: int = 10, invest: int = 4,
                 other: int = -2, tax_rate: Decimal = Decimal("0.2"), nci: int = 1,
                 shares: int | None = 100, dps: str | None = None, revenue: int | None = None,
                 with_oi: bool = True) -> list[dict]:
    """One period's income lines, in millions, consistent by construction."""

    pretax = oi - interest + invest + other
    tax = int(Decimal(pretax) * tax_rate)
    ni = pretax - tax - nci
    rows = []

    def line(concept, value, parent=None, balance=None, unit="usd"):
        rows.append({"concept": concept, "value": str(value), "parent": parent, "balance": balance,
                     "unit": unit, "start": start, "end": end})

    if revenue is not None:
        line("us-gaap:Revenues", revenue * MILLION, "us-gaap:OperatingIncomeLoss", "credit")
    if with_oi:
        line("us-gaap:OperatingIncomeLoss", oi * MILLION, PRETAX, "credit")
        line("us-gaap:InterestExpense", interest * MILLION, PRETAX, "debit")
        line("us-gaap:InvestmentIncomeInterest", invest * MILLION, PRETAX, "credit")
        line("us-gaap:OtherNonoperatingIncomeExpense", other * MILLION, PRETAX, "credit")
    line(PRETAX, pretax * MILLION, "us-gaap:ProfitLoss", "credit")
    line("us-gaap:IncomeTaxExpenseBenefit", tax * MILLION, "us-gaap:ProfitLoss", "debit")
    line("us-gaap:NetIncomeLossAttributableToNoncontrollingInterest", nci * MILLION, "us-gaap:NetIncomeLoss")
    line("us-gaap:NetIncomeLoss", ni * MILLION, None, "credit")
    line("us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic", ni * MILLION, None, "credit")
    if shares is not None:
        eps = (Decimal(ni) / Decimal(shares)).quantize(Decimal("0.01"))
        line("us-gaap:EarningsPerShareBasic", eps, unit="usdPerShare")
        line("us-gaap:EarningsPerShareDiluted", eps, unit="usdPerShare")
        line("us-gaap:WeightedAverageNumberOfSharesOutstandingBasic", shares * MILLION, unit="shares")
        line("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", shares * MILLION, unit="shares")
    else:
        eps = (Decimal(ni) / Decimal(100)).quantize(Decimal("0.01"))
        line("us-gaap:EarningsPerShareDiluted", eps, unit="usdPerShare")
        line("us-gaap:EarningsPerShareBasic", eps, unit="usdPerShare")
    if dps is not None:
        line("us-gaap:CommonStockDividendsPerShareCashPaid", dps, unit="usdPerShare")
    return rows


def calendar_company_filings(*, shares: int | None = 100, with_oi: bool = True) -> list[tuple]:
    """A calendar-year filer: one 10-K (FY2025, FY2024) and 10-Qs with comparatives.

    Quarterly OI grows 10 per quarter from 100 in Q1 2024; no fourth quarter is
    ever stated on its own.
    """

    def q(year, n):
        starts = {1: "01-01", 2: "04-01", 3: "07-01", 4: "10-01"}
        ends = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
        return f"{year}-{starts[n]}", f"{year}-{ends[n]}"

    oi = {(2024, n): 100 + 10 * (n - 1) for n in range(1, 5)}
    oi.update({(2025, n): 140 + 10 * (n - 1) for n in range(1, 5)})
    oi.update({(2026, n): 180 + 10 * (n - 1) for n in range(1, 3)})
    kwargs = dict(shares=shares, with_oi=with_oi, dps="0.50", revenue=None)

    def ytd(year, n):
        total = sum(oi[(year, k)] for k in range(1, n + 1))
        return period_lines(f"{year}-01-01", q(year, n)[1], oi=total, interest=10 * n, invest=4 * n,
                            other=-2 * n, nci=n, **{**kwargs, "dps": str(Decimal("0.50") * n)})

    def quarter(year, n):
        return period_lines(*q(year, n), oi=oi[(year, n)], **kwargs)

    filings = []
    filings.append(("acc-10k-2025", "10-K", "2026-02-10", "2025-12-31",
                    ytd(2025, 4) + ytd(2024, 4)))
    for year, filed_year in ((2025, 2025), (2026, 2026)):
        filings.append((f"acc-q1-{year}", "10-Q", f"{filed_year}-05-01", f"{year}-03-31",
                        quarter(year, 1) + quarter(year - 1, 1)))
        filings.append((f"acc-q2-{year}", "10-Q", f"{filed_year}-08-01", f"{year}-06-30",
                        quarter(year, 2) + ytd(year, 2) + quarter(year - 1, 2) + ytd(year - 1, 2)))
    filings.append(("acc-q3-2025", "10-Q", "2025-11-01", "2025-09-30",
                    quarter(2025, 3) + ytd(2025, 3) + quarter(2024, 3) + ytd(2024, 3)))
    return filings


def install_filings(connection: sqlite3.Connection, company_ref: str, filings) -> None:
    connection.executescript(STATEMENT_DDL)
    for accession, form, filed, report, lines in filings:
        ingest = f"ingest:{company_ref}:{accession}"
        connection.execute("INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?,?,?)",
                           (ingest, company_ref, accession, form, filed, report))
        for ordinal, line in enumerate(lines):
            connection.execute(
                "INSERT INTO coverage_mission_statement_lines(line_id, ingest_id, statement, ordinal, "
                "concept, label, parent_concept, period_start, period_end, value, unit, balance) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"line:{accession}:{ordinal}", ingest, line.get("statement", "income"), ordinal,
                 line["concept"], line["concept"].split(":")[1], line["parent"], line["start"],
                 line["end"], line["value"], line["unit"], line["balance"]))
    connection.commit()


def memory_core(filings=None, company_ref: str = CO) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    install_filings(connection, company_ref, filings if filings is not None else calendar_company_filings())
    return connection


class IncomeBridgeTests(unittest.TestCase):

    def bridges(self, filings=None):
        from dalton_core.return_attribution import build_income_bridges, collect_periods

        connection = memory_core(filings)
        self.addCleanup(connection.close)
        collected = collect_periods(connection, CO)
        return collected, build_income_bridges(collected, CO)

    def test_typed_bridge_reconciles_where_operating_income_less_tax_does_not(self) -> None:
        """The 09-11 finding: net income != operating income - tax; the bridge explains it."""

        _collected, bridges = self.bridges()
        q2 = next(b for b in bridges if b["period"]["start"] == "2026-04-01")
        self.assertEqual(q2["status"], "complete", q2["steps"])
        self.assertEqual(q2["period"]["semantics"], "quarterly")
        self.assertEqual(q2["period"]["fiscal_label"], "FY2026 Q2")
        oi = Decimal(q2["anchors"]["operating_income"]["value"])
        tax = next(Decimal(line["value"]) for line in q2["lines"] if line["classification"] == "tax")
        ni = Decimal(q2["anchors"]["net_income"]["value"])
        self.assertNotEqual(oi - tax, ni)
        classes = {line["classification"]: line["sign"] for line in q2["lines"] if line["step"]}
        self.assertEqual(classes, {"interest_expense": -1, "interest_income": 1,
                                   "other_non_operating": 1, "tax": -1, "minority_interest": -1})
        for line in q2["lines"]:
            self.assertTrue(line["ref"].startswith("statement-line:"))
            self.assertEqual(line["accession"], "acc-q2-2026")
        self.assertEqual([step["residual"] for step in q2["steps"][:2]], ["0", "0"])
        self.assertEqual(q2["net_income_to_common"]["basis"], "filed")

    def test_a_residual_is_recorded_not_absorbed(self) -> None:
        filings = calendar_company_filings()
        accession, form, filed, report, lines = filings[-1]
        for line in lines:
            if line["concept"] == PRETAX and line["start"] == "2025-07-01":
                line["value"] = str(int(line["value"]) + 50 * MILLION)
        _collected, bridges = self.bridges(filings)
        q3 = next(b for b in bridges if b["period"]["start"] == "2025-07-01")
        self.assertEqual(q3["status"], "residual")
        self.assertEqual(q3["residuals"][0]["step"], "operating_to_pretax")
        self.assertEqual(Decimal(q3["residuals"][0]["residual"]), 50 * MILLION)

    def test_a_missing_operating_income_line_is_named(self) -> None:
        """IBM and DXC file no operating income line: the bridge says so."""

        _collected, bridges = self.bridges(calendar_company_filings(with_oi=False))
        bridge = bridges[0]
        self.assertEqual(bridge["status"], "missing_rows")
        self.assertEqual(bridge["missing_rows"], ["operating_income"])
        self.assertEqual(bridge["steps"][1]["status"], "complete")

    def test_a_subtotal_is_expanded_to_its_typed_children(self) -> None:
        rows = [
            {"concept": "us-gaap:OperatingIncomeLoss", "value": "874000000", "parent": PRETAX},
            {"concept": "us-gaap:NonoperatingIncomeExpense", "value": "1000000", "parent": PRETAX},
            {"concept": "us-gaap:ForeignCurrencyTransactionGainLossBeforeTax", "value": "7000000",
             "parent": "us-gaap:NonoperatingIncomeExpense"},
            {"concept": "us-gaap:InterestExpenseNonoperating", "value": "13000000",
             "parent": "us-gaap:NonoperatingIncomeExpense"},
            {"concept": "us-gaap:InvestmentIncomeInterestAndDividend", "value": "18000000",
             "parent": "us-gaap:NonoperatingIncomeExpense"},
            {"concept": "us-gaap:OtherNonoperatingIncomeExpense", "value": "-11000000",
             "parent": "us-gaap:NonoperatingIncomeExpense"},
            {"concept": PRETAX, "value": "875000000", "parent": "us-gaap:NetIncomeLoss"},
            {"concept": "us-gaap:IncomeLossFromEquityMethodInvestments", "value": "-8000000",
             "parent": "us-gaap:NetIncomeLoss"},
            {"concept": "us-gaap:IncomeTaxExpenseBenefit", "value": "231000000",
             "parent": "us-gaap:NetIncomeLoss"},
            {"concept": "us-gaap:NetIncomeLoss", "value": "636000000", "parent": None},
        ]
        for row in rows:
            row.update(start="2026-04-01", end="2026-06-30", unit="usd", balance=None)
        _collected, bridges = self.bridges([("acc-ctsh", "10-Q", "2026-07-30", "2026-06-30", rows)])
        bridge = bridges[0]
        self.assertEqual(bridge["status"], "complete", bridge["steps"])
        self.assertEqual(bridge["subtotals"][0]["gap"], "0")
        equity = next(line for line in bridge["lines"] if line["classification"] == "equity_method")
        self.assertEqual(equity["step"], "pretax_to_net_income")

    def test_net_income_falls_back_to_the_cash_flow_statement(self) -> None:
        """Amazon's income statement lines carry no NetIncomeLoss; its cash flow does."""

        filings = calendar_company_filings()
        for _a, _f, _d, _r, lines in filings:
            for line in lines:
                if line["concept"] == "us-gaap:NetIncomeLoss":
                    line["statement"] = "cash"
        _collected, bridges = self.bridges(filings)
        self.assertTrue(all(b["status"] == "complete" for b in bridges), [b["missing_rows"] for b in bridges])
        self.assertEqual(bridges[0]["anchors"]["net_income"]["statement"], "cash")


class PerShareSeriesTests(unittest.TestCase):

    def series(self, filings=None, extra=None):
        from dalton_core.return_attribution import (
            build_income_bridges, build_per_share_series, collect_periods,
        )

        connection = memory_core(filings)
        self.addCleanup(connection.close)
        if extra:
            install_filings(connection, CO, extra)
        collected = collect_periods(connection, CO)
        return build_per_share_series(CO, collected, build_income_bridges(collected, CO))

    def test_period_semantics_are_explicit_and_no_quarter_is_derived(self) -> None:
        series = self.series()
        semantics = {(row["start"], row["end"]): row["semantics"] for row in series["periods"]}
        self.assertEqual(semantics[("2025-01-01", "2025-12-31")], "annual")
        self.assertEqual(semantics[("2025-01-01", "2025-09-30")], "year_to_date")
        self.assertEqual(semantics[("2025-07-01", "2025-09-30")], "quarterly")
        self.assertNotIn(("2025-10-01", "2025-12-31"), semantics)
        self.assertIn("FY2025 Q4", [item["fiscal_label"] for item in series["quarters_not_stated"]])
        self.assertEqual(series["fiscal_year_end"], "12-31")
        self.assertEqual(series["missing"], [])
        self.assertTrue(all(row["eps_check"] == "consistent" for row in series["periods"]))

    def test_trailing_twelve_months_use_one_stated_rule(self) -> None:
        from dalton_core.return_attribution import TTM_RULE_ANNUAL, TTM_RULE_YTD

        series = self.series()
        points = {point["end"]: point for point in series["ttm"]}
        self.assertEqual(points["2025-12-31"]["rule"], TTM_RULE_ANNUAL)
        latest = points["2026-06-30"]
        self.assertEqual(latest["rule"], TTM_RULE_YTD)
        self.assertEqual([item["role"] for item in latest["components"]],
                         ["prior_fiscal_year", "year_to_date", "prior_year_to_date"])
        # Net income TTM = FY2025 + 6M 2026 - 6M 2025, all three filed.
        by_period = {(row["start"], row["end"]): row for row in series["periods"]}
        expected = (Decimal(by_period[("2025-01-01", "2025-12-31")]["net_income_to_common"]["value"])
                    + Decimal(by_period[("2026-01-01", "2026-06-30")]["net_income_to_common"]["value"])
                    - Decimal(by_period[("2025-01-01", "2025-06-30")]["net_income_to_common"]["value"]))
        self.assertEqual(Decimal(latest["net_income_to_common"]["value"]), expected)
        self.assertEqual(latest["diluted_shares"]["basis"], "ttm_day_weighted")

    def test_a_missing_diluted_share_count_is_flagged(self) -> None:
        """The DXC case: EPS filed, share counts not on the face of the statement."""

        series = self.series(calendar_company_filings(shares=None))
        fields = {item["field"] for item in series["missing"]}
        self.assertEqual(fields, {"basic_shares", "diluted_shares"})
        self.assertTrue(all(row["eps_check"] == "not_checkable" for row in series["periods"]))

    def test_a_ten_q_trailing_year_is_not_a_fiscal_year(self) -> None:
        """Amazon states twelve months to each quarter end in its 10-Q cash flow."""

        rows = [{"concept": "us-gaap:NetIncomeLoss", "value": "99", "parent": None,
                 "statement": "cash", "start": "2025-07-01", "end": "2026-06-30", "unit": "usd",
                 "balance": None}]
        series = self.series(extra=[("acc-ttm", "10-Q", "2026-08-02", "2026-06-30", rows)])
        self.assertIn("2025-07-01..2026-06-30", series["skipped_periods"])

    def test_dividends_per_share_derived_from_cash_is_labelled(self) -> None:
        filings = calendar_company_filings()
        for _a, _f, _d, _r, lines in filings:
            for line in list(lines):
                if line["concept"] == "us-gaap:CommonStockDividendsPerShareCashPaid":
                    lines.remove(line)
            lines.append({"concept": "us-gaap:PaymentsOfDividendsCommonStock", "value": str(50 * MILLION),
                          "parent": None, "statement": "cash", "start": lines[0]["start"],
                          "end": lines[0]["end"], "unit": "usd", "balance": None})
        series = self.series(filings)
        row = next(row for row in series["periods"] if row["start"] == "2026-04-01")
        self.assertEqual(row["dps"]["basis"], "derived_cash_dividends_over_basic_shares")
        self.assertEqual(row["dps"]["value"], "0.5000")


def snapshot_and_price(close: str = "120", start_close: str = "100", bar: str = "2026-09-30"):
    snapshot = {"id": "valuation-snapshot-version:x", "content_hash": "a" * 64,
                "price": {"bar_date": bar, "close": close,
                          "version_ref": "market-price-series-version:p"},
                "metrics": [{"metric": "trailing_pe", "status": "available", "value": "20"}]}
    price = {"id": "market-price-series-version:p",
             "bars": [{"date": "2025-09-29", "close": start_close}, {"date": bar, "close": close}]}
    return snapshot, price


class ReturnBridgeTests(unittest.TestCase):

    def series(self):
        return PerShareSeriesTests.series(self)

    def test_realised_decomposition_multiplies_back_to_the_price(self) -> None:
        from dalton_core.return_attribution import build_return_bridge

        snapshot, price = snapshot_and_price()
        filings = calendar_company_filings()
        annual_2024 = [dict(row) for row in filings[0][4] if row["start"] == "2024-01-01"]
        filings.insert(0, ("acc-10k-2024", "10-K", "2025-02-10", "2024-12-31", annual_2024))
        connection = memory_core(filings)
        from dalton_core.return_attribution import compute_company
        series = compute_company(connection, CO)["series"]
        body = build_return_bridge(CO, series=series, series_ref="s", snapshot=snapshot,
                                   price=price, model=None, consensus=None)
        leg = body["legs"][0]
        self.assertEqual(leg["status"], "available", leg)
        product = Decimal(1)
        for name in ("earnings_growth", "share_count", "multiple_change"):
            product *= Decimal(leg["factors"][name])
        self.assertAlmostEqual(float(product), 1.2, places=4)
        total = sum(Decimal(leg["contributions_pp"][name])
                    for name in ("earnings_growth", "share_count", "multiple_change"))
        self.assertAlmostEqual(float(total), 20.0, delta=0.02)
        self.assertEqual(leg["price_return_percent"], "20.00")
        self.assertIsNone(leg["total_return_percent"])
        self.assertIsNone(leg["dividend_yield_percent"])
        self.assertIsNone(leg["expectation_source_kind"])
        self.assertEqual(body["legs"][1]["status"], "unavailable")
        self.assertIn("驱动模型", body["legs"][1]["reason"])

    def test_driver_leg_refuses_the_revenue_times_margin_proxy(self) -> None:
        """2026-10-09 (P0): a revenue-only model gives no expected return at all."""

        from dalton_core.return_attribution import build_return_bridge

        snapshot, price = snapshot_and_price()
        series = self.series()
        # A revenue-only model: the proxy that multiplied by the trailing net
        # margin is gone (GOOGL's 54.75% margin carried ~$99B of securities gains).
        for row in series["periods"]:
            days = (datetime.fromisoformat(row["end"]) - datetime.fromisoformat(row["start"])).days + 1
            row["revenue"] = {"value": str(250 * MILLION * round(days / 91)), "ref": "statement-line:r"}
        from dalton_core.return_attribution import _ttm_points, FiscalCalendar
        series["ttm"] = _ttm_points(series["periods"], FiscalCalendar(["2025-12-31"]))
        cells = [{"ref": f"result:revenue@{end}", "status": "computed", "value": str(1100 * MILLION),
                  "period": {"kind": "quarter", "start": start, "end": end}}
                 for start, end in (("2026-07-01", "2026-09-30"), ("2026-10-01", "2026-12-31"),
                                    ("2027-01-01", "2027-03-31"), ("2027-04-01", "2027-06-30"))]
        model = {"id": "forecast-model-version:m", "content_hash": "b" * 64,
                 "results": [{"ref": "result:revenue", "role": "revenue", "cells": cells},
                             {"ref": "result:net_income", "role": "net_income",
                              "cells": [dict(cell, status="unavailable", value=None) for cell in cells]}]}
        body = build_return_bridge(CO, series=series, series_ref="s", snapshot=snapshot, price=price,
                                   model=model, consensus=None)
        leg = body["legs"][1]
        self.assertEqual(leg["status"], "unavailable", leg)
        self.assertEqual(leg["reason_code"], "model_net_income_unavailable")
        self.assertIn("不再用", leg["reason"])
        self.assertNotIn("earnings_basis", leg)

    def test_consensus_and_sell_side_legs_carry_their_source_kinds(self) -> None:
        from dalton_core.return_attribution import build_return_bridge

        snapshot, price = snapshot_and_price(close="100")
        consensus = {"id": "consensus-estimate-version:c", "content_hash": "c" * 64,
                     "company_ref": CO, "source_ref": "source:yahoo-finance",
                     "fiscal_calendar": {"last_reported_period_end": "2026-06-30"},
                     "eps_estimates": [{"label": "FY2026", "period_kind": "fiscal_year",
                                        "period_end": "2026-12-31", "vendor_period": "0y",
                                        "avg": "5.5", "year_ago": "5.0"}],
                     "price_target": {"mean": "121"},
                     "report_consensus": {"mean": "99", "estimate_refs": ["street-estimate:s1"]}}
        body = build_return_bridge(CO, series=self.series(), series_ref="s", snapshot=snapshot,
                                   price=price, model=None, consensus=consensus)
        consensus_leg, sell_side = body["legs"][2], body["legs"][3]
        self.assertEqual((consensus_leg["expectation_source_kind"], sell_side["expectation_source_kind"]),
                         ("consensus", "sell_side_note"))
        self.assertEqual(consensus_leg["factors"]["earnings_growth"], "1.100000")
        self.assertEqual(consensus_leg["factors"]["multiple_change"], "1.100000")
        self.assertIsNone(consensus_leg["contributions_pp"]["share_count"])
        self.assertEqual(sell_side["inputs"]["target_refs"], ["street-estimate:s1"])


class ExpectationSourceTests(unittest.TestCase):

    def test_classifier_is_closed_and_deterministic(self) -> None:
        from dalton_core.return_attribution import (
            EXPECTATION_SOURCE_KINDS, classify_ref, consensus_tags, conviction_tags,
        )

        self.assertEqual(EXPECTATION_SOURCE_KINDS, ("consensus", "management_guidance",
                                                    "sell_side_note", "owner_view", "market_implied"))
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE claim_versions (claim_version_id TEXT, claim_ref TEXT)")
        connection.executemany("INSERT INTO claim_versions VALUES(?,?)", [
            ("claim-version:t", "claim:transcript:1"), ("claim-version:a", "claim:alphaengine:2"),
            ("claim-version:w", "claim:public_web:3")])
        self.assertEqual(classify_ref(connection, "claim-version:t")[0], "management_guidance")
        self.assertEqual(classify_ref(connection, "claim-version:a")[0], "sell_side_note")
        self.assertIsNone(classify_ref(connection, "claim-version:w")[0])
        self.assertEqual(classify_ref(connection, "consensus-estimate-version:x")[0], "consensus")
        self.assertEqual(classify_ref(connection, "valuation-snapshot-version:x")[0], "market_implied")
        record = {"id": "consensus-estimate-version:x", "content_hash": "d" * 64, "company_ref": CO,
                  "source_ref": "source:yahoo-finance", "eps_estimates": [{}], "price_target": {"mean": "1"},
                  "report_consensus": {"estimate_refs": ["street-estimate:1"]}}
        kinds = {tag["field"]: tag["primary_kind"] for tag in consensus_tags(record)}
        self.assertEqual(kinds, {"eps_estimates": "consensus", "price_target": "consensus",
                                 "report_consensus": "sell_side_note"})
        call = {"id": "conviction-call-proposal:x", "content_hash": "e" * 64, "company_ref": CO,
                "market_view_alignment": {"market_view": {
                    "available": True, "sources": ["sell_side_rating", "crowd_narrative"],
                    "refs": ["claim-version:w"]}},
                "scenarios": [{"source_ref": "company-scenario-version:s"}]}
        tags = {tag["field"]: tag for tag in conviction_tags(connection, call)}
        self.assertEqual(tags["market_view"]["primary_kind"], "sell_side_note")
        self.assertEqual(len(tags["market_view"]["unclassified"]), 2)
        self.assertEqual(tags["scenarios"]["primary_kind"], "owner_view")


class StoreCase(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.store = DaltonStore(Path(self.temp.name) / "core.sqlite")
        self.addCleanup(self.store.close)
        install_filings(self.store.connection, CO, calendar_company_filings())


class AuthorityTests(StoreCase):

    def test_append_only_versioned_on_change_and_scope_checked(self) -> None:
        from dalton_core.return_attribution import (
            ReturnAttributionAuthority, ReturnAttributionConflict, compute_company,
            latest_bridges, refresh_company,
        )

        out = refresh_company(self.store, mission(), CO)
        self.assertEqual(out["bridges_published"], out["bridges"])
        self.assertEqual((out["series_published"], out["return_published"]), (1, 1))
        again = refresh_company(self.store, mission(), CO)
        self.assertEqual((again["bridges_published"], again["series_published"],
                          again["return_published"]), (0, 0, 0))
        # A restated filing moves one bridge to version 2; the rest stay.
        self.store.connection.execute(
            "UPDATE coverage_mission_statement_lines SET value=? WHERE concept=? AND period_start=? "
            "AND ingest_id LIKE ?", (str(77 * MILLION), "us-gaap:IncomeTaxExpenseBenefit",
                                     "2026-04-01", "%acc-q2-2026"))
        self.store.connection.commit()
        moved = refresh_company(self.store, mission(), CO)
        self.assertEqual(moved["bridges_published"], 1)
        q2 = next(b for b in latest_bridges(self.store.connection, CO)
                  if b["period"]["start"] == "2026-04-01")
        self.assertEqual((q2["version"], q2["status"]), (2, "residual"))
        self.assertIsNotNone(q2["prior_version_ref"])
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute("UPDATE income_bridge_versions SET status='complete'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute("DELETE FROM per_share_series_versions")
        authority = ReturnAttributionAuthority(self.store)
        body = compute_company(self.store.connection, CO)["bridges"][0]
        with self.assertRaises(ReturnAttributionConflict):
            authority.publish_income_bridge(body, mission=mission(scopes=("claim",)), actor_ref=AUTOMATION)
        with self.assertRaises(ReturnAttributionConflict):
            authority.publish_income_bridge(body, mission=mission(), actor_ref="automation:someone-else")

    def test_same_inputs_same_bodies(self) -> None:
        from dalton_core.return_attribution import compute_company

        first = compute_company(self.store.connection, CO)
        second = compute_company(self.store.connection, CO)
        self.assertEqual(content_hash(first["bridges"]), content_hash(second["bridges"]))
        self.assertEqual(content_hash(first["series"]), content_hash(second["series"]))


class LaneTests(StoreCase):

    def server(self, current):
        class Missions:
            def mission(self, _ref):
                return current

        self.store.connection.execute("INSERT INTO coverage_mission_pointer VALUES('m', ?)", (current["id"],))
        self.store.connection.commit()
        return SimpleNamespace(store=self.store, coverage_mission=Missions(), lane_state={})

    def test_one_company_per_tick_then_idle_with_chinese_reasons(self) -> None:
        from dalton_core.lane_registry import load_lanes, registered_lanes
        from dalton_core.mission_return_attribution_lane import dispatch

        load_lanes()
        lane = next(spec for spec in registered_lanes() if spec.operation == "dispatch_return_attribution")
        self.assertEqual((lane.order, lane.driver_key, lane.init_kwarg), (98, "return_attribution", None))
        other = "company:ticker:none"
        server = self.server(mission(universe=(CO, other)))
        now = datetime(2026, 10, 7, tzinfo=timezone.utc)
        first = dispatch(server, {}, now=now)
        self.assertEqual((first["status"], first["company_ref"], first["remaining"]),
                         ("published", CO, 1), first)
        self.assertIn("利润桥", first["reason"])
        second = dispatch(server, {}, now=now + timedelta(seconds=5))
        self.assertEqual((second["status"], second["company_ref"]), ("unchanged", other))
        self.assertIn("还没有申报报表行", second["reason"])
        third = dispatch(server, {}, now=now + timedelta(seconds=10))
        self.assertEqual(third["status"], "idle")
        self.assertIn("没有变化", third["reason"])

    def test_ungranted_mission_is_refused_every_tick(self) -> None:
        from dalton_core.mission_return_attribution_lane import dispatch

        server = self.server(mission(scopes=("claim",)))
        out = dispatch(server, {})
        self.assertEqual(out["status"], "ungranted")
        self.assertIn("valuation", out["reason"])

    def test_registered_lane_modules_and_schema_owners(self) -> None:
        from dalton_core.bootstrap import SCHEMA_DATABASES
        from dalton_core.cockpit_plane import REGISTRY_LANE_LABELS
        from dalton_core.lane_registry import LANE_MODULES
        from scripts.rehearse_deploy import known_schema_files

        self.assertIn("dalton_core.mission_return_attribution_lane", LANE_MODULES)
        self.assertIn(("return_attribution_schema.sql", None), SCHEMA_DATABASES)
        self.assertIn("return_attribution_schema.sql", known_schema_files())
        self.assertIn("return_attribution", REGISTRY_LANE_LABELS)


class CockpitAndBriefTests(StoreCase):

    def test_cockpit_panel_is_chinese_and_absent_without_tables(self) -> None:
        from dalton_core.cockpit_return_attribution import card_views
        from dalton_core.return_attribution import refresh_company

        self.assertEqual(card_views(self.store.connection, [CO]), {})
        refresh_company(self.store, mission(), CO)
        view = card_views(self.store.connection, [CO], "industry:test")[CO]
        self.assertIn("个申报期的利润桥", view["headline"])
        self.assertEqual([b["semantics_label"] for b in view["bridges"]], ["完整财年", "单季", "年初至今累计"])
        self.assertEqual(view["bridges"][0]["status_label"], "完整勾稽")
        self.assertIn("FY2025 Q4", view["quarters_not_stated"])
        self.assertEqual([leg["status"] for leg in view["returns"]["legs"]], ["unavailable"] * 4)
        html = (Path(__file__).resolve().parents[1] / "src" / "dalton_core" / "cockpit_control.html").read_text()
        self.assertIn('["回报归因",()=>returnsBlock(c)]', html)
        # 2026-10-07 inspection: amounts in one stated unit with separators,
        # the XBRL concept only under 技术详情.
        bridge = view["bridges"][0]
        self.assertIn(bridge["amount_unit"], {"亿美元", "万美元", "美元"})
        for row in bridge["rows"]:
            self.assertRegex(row["display_value"], r"^-?[\d,]+\.\d{2}$")
            self.assertTrue(row["concept"].startswith("us-gaap:"))
        self.assertNotIn("row.concept])", html)
        self.assertIn("报表行（XBRL 科目 · 原始金额）", html)

    def test_amounts_and_share_counts_read_in_chinese_units(self) -> None:
        from dalton_core.return_attribution import amount_in, amount_scale, share_count_text

        divisor, unit = amount_scale(["69672977000", "10225664000", "-45000000"])
        self.assertEqual(unit, "亿美元")
        self.assertEqual(amount_in("69672977000", divisor), "696.73")
        self.assertEqual(amount_in("-45000000", divisor), "-0.45")
        self.assertEqual(amount_scale(["5000000"])[1], "万美元")
        self.assertEqual(amount_in("123456789000", Decimal("100000000")), "1,234.57")
        self.assertEqual(share_count_text("626824946"), "6.27 亿股")
        self.assertEqual(share_count_text("52267000"), "5,226.70 万股")
        self.assertIsNone(share_count_text(None))

    def test_weekly_brief_lines_on_material_change_and_replay(self) -> None:
        from dalton_core.return_attribution import (
            ReturnAttributionAuthority, ReturnAttributionConflict, brief_entries, brief_lines,
            material_changes, replay_brief_entries,
        )

        authority = ReturnAttributionAuthority(self.store)

        def body(total: str, multiple: str) -> dict:
            return {"company_ref": CO, "bridge_ref": "holding-period-return:test", "as_of": "2026-10-01",
                    "legs": [{"leg": "realised_trailing_year", "status": "available",
                              "total_return_percent": total,
                              "contributions_pp": {"earnings_growth": "5.00", "share_count": "1.00",
                                                   "multiple_change": multiple, "dividends": "2.00"}}]}

        start = datetime.now(timezone.utc) - timedelta(seconds=1)
        authority.publish_return_bridge(body("10.00", "2.00"), mission=mission(), actor_ref=AUTOMATION)
        mid = datetime.now(timezone.utc)
        self.assertEqual(len(brief_entries(self.store.connection, [CO], start, mid)), 1)
        authority.publish_return_bridge(body("11.00", "3.00"), mission=mission(), actor_ref=AUTOMATION)
        later = datetime.now(timezone.utc)
        self.assertEqual(brief_entries(self.store.connection, [CO], mid, later), [])
        authority.publish_return_bridge(body("20.00", "12.00"), mission=mission(), actor_ref=AUTOMATION)
        end = datetime.now(timezone.utc) + timedelta(seconds=1)
        entries = brief_entries(self.store.connection, [CO], later, end)
        self.assertEqual([c["component"] for c in entries[0]["changes"]], ["total", "multiple_change"])
        lines = brief_lines(entries, {CO: "ACME"})
        self.assertTrue(lines[0].startswith("- ACME｜回报分解变化：已实现（过去 12 个月）总回报 11.00 → 20.00"))
        replay_brief_entries(self.store.connection, entries)
        tampered = [dict(entries[0], hash="0" * 64)]
        with self.assertRaises(ReturnAttributionConflict):
            replay_brief_entries(self.store.connection, tampered)
        self.assertIn("没有公司", brief_lines([], {})[0])
        self.assertEqual(material_changes(None, body("1", "1")), [])


if __name__ == "__main__":
    unittest.main()
