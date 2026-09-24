"""P11c-E: the lane that finally puts a number in ``valuation_snapshot_versions``.

Every figure below is hand-computable from the fixture, which is the point:

    market cap   = 50 x 100                              = 5,000
    TTM earnings = 100 + 200 + 300 + (1,000 - 600)       = 1,000
    trailing P/E = 5,000 / 1,000                         = 5
    TTM revenue  = 1,000 + 2,000 + 3,000 + (10,000-6,000)= 10,000
    price/sales  = 5,000 / 10,000                        = 0.5
    EV           = 5,000 + 2,000 - 500                   = 6,500
    EBITDA       = 1,250 + 200                           = 1,450
    EV/EBITDA    = 6,500 / 1,450                         = 4.4828 (4 dp)
    FCF          = 1,000 - 200                           = 800
    FCF yield    = 800 / 5,000                           = 0.16

The fourth quarter in each of those trailing years is one no filer states: it
is the fiscal year minus the nine months inside it, and the tests below check
that it arrives labelled, carrying both accessions, and that the authority
refuses it when the subtraction does not hold.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from dalton_core.market_price import MarketPriceSeriesAuthority
from dalton_core.valuation_snapshot import (
    COMPONENT_DERIVED,
    COMPONENT_REPORTED,
    ValuationSnapshotAuthority,
    ValuationSnapshotConflict,
)
from dalton_core.valuation_snapshot_cli import (
    MAX_PRICE_TRADING_DAY_LAG,
    build_inputs,
    held_fingerprint,
    run_valuation_snapshot,
    scan,
)
from dalton_core.valuation_snapshot_lane import (
    ValuationSnapshotLaneCoordinator,
    ledger_signature,
    may_write_valuation,
)
from dalton_core.coverage_mission import CoverageMissionAuthority
from tests.p14a_fixtures import ACN, CTSH, ARTIFACT, GOVERNANCE, GOVERNANCE_HASH, \
    INVOCATION, P14aHarness, bar

CAPTURED_AT = "2026-05-30T23:30:00+00:00"
FIRST_BAR = "2026-02-20"
BARS = 40

# quarter -> (period_start, period_end)
Q1 = ("2025-01-01", "2025-03-31")
Q2 = ("2025-04-01", "2025-06-30")
Q3 = ("2025-07-01", "2025-09-30")
H1 = ("2025-01-01", "2025-06-30")
M9 = ("2025-01-01", "2025-09-30")
FY = ("2025-01-01", "2025-12-31")

# role -> (statement, concept, Q1, Q2, Q3, H1, 9M, FY)
FLOWS = {
    "net_income": ("income", "us-gaap:NetIncomeLoss",
                   "100", "200", "300", "300", "600", "1000"),
    "revenue": ("income", "us-gaap:Revenues",
                "1000", "2000", "3000", "3000", "6000", "10000"),
    "operating_income": ("income", "us-gaap:OperatingIncomeLoss",
                         "150", "250", "350", "400", "750", "1250"),
    "depreciation_amortisation": (
        "cash", "us-gaap:DepreciationDepletionAndAmortization",
        "50", "50", "50", "100", "150", "200"),
    "operating_cash_flow": (
        "cash", "us-gaap:NetCashProvidedByUsedInOperatingActivities",
        "200", "200", "200", "400", "600", "1000"),
    "capital_expenditure": (
        "cash", "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
        "50", "50", "50", "100", "150", "200"),
}
INSTANTS = {
    "total_debt": ("balance", "us-gaap:DebtLongtermAndShorttermCombinedAmount", "2000"),
    "cash_and_equivalents": (
        "balance", "us-gaap:CashAndCashEquivalentsAtCarryingValue", "500"),
}
FILINGS = (
    # accession, form, filed, report_date, which periods the filing states
    ("0001467373-25-000001", "10-Q", "2025-05-05", "2025-03-31", (Q1,)),
    ("0001467373-25-000002", "10-Q", "2025-08-05", "2025-06-30", (Q2, H1)),
    ("0001467373-25-000003", "10-Q", "2025-11-05", "2025-09-30", (Q3, M9)),
    ("0001467373-26-000001", "10-K", "2026-02-20", "2025-12-31", (FY,)),
)
PERIOD_VALUE = {Q1: 2, Q2: 3, Q3: 4, H1: 5, M9: 6, FY: 7}


def _bars(count: int = BARS, first: str = FIRST_BAR, first_close: int = 11):
    start = date.fromisoformat(first)
    return [((start + timedelta(days=index)).isoformat(), str(first_close + index))
            for index in range(count)]


class ValuationLaneHarness(P14aHarness):
    grants = ("market_price", "valuation")
    flows = FLOWS

    def setUp(self) -> None:
        super().setUp()
        self.prices = MarketPriceSeriesAuthority(self.store)
        self.snapshots = ValuationSnapshotAuthority(self.store)
        self._attempt = 0

    # -- the priced side ---------------------------------------------------

    def publish_prices(self, company_ref=ACN, ticker="ACN", closes=None,
                       shares="100", captured_at=CAPTURED_AT):
        closes = _bars() if closes is None else closes
        return self.prices.publish_series(
            company_ref=company_ref, ticker=ticker, currency="USD",
            bars=[bar(day, close) for day, close in closes],
            observations=[{"observation": "shares_outstanding",
                           "as_of": closes[-1][0], "value": shares,
                           "unit": "shares"}],
            invocation_ref=INVOCATION, artifact_hash=ARTIFACT,
            governance_ref=GOVERNANCE, governance_hash=GOVERNANCE_HASH,
            requested_start=closes[0][0], requested_end=closes[-1][0],
            captured_at=captured_at,
        )

    # -- the filed side ----------------------------------------------------

    def line(self, statement, concept, value, period, *, ordinal):
        start, end = period
        return {
            "statement": statement, "concept": concept,
            "label": concept.split(":")[-1], "level": 1,
            "parent_concept": None, "is_breakdown": False,
            "dimension_axis": None, "dimension_member": None,
            "period_start": start, "period_end": end,
            "value": value, "unit": "usd", "balance": "credit",
        }

    def filing_lines(self, periods, report_date, *, drop=()):
        lines = []
        for role, (statement, concept, *values) in self.flows.items():
            if role in drop:
                continue
            for period in periods:
                lines.append(self.line(statement, concept,
                                       values[PERIOD_VALUE[period] - 2], period,
                                       ordinal=len(lines)))
        for role, (statement, concept, value) in INSTANTS.items():
            if role in drop:
                continue
            lines.append(self.line(statement, concept, value,
                                   (None, report_date), ordinal=len(lines)))
        return lines

    def ingest_filings(self, company_ref=ACN, cik="0001467373", drop=(),
                       filings=FILINGS):
        authorization = self.missions.authorize_sec_lane(
            company_ref=company_ref, ticker="ACN",
            actor_ref="automation:coverage-mission",
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"],
        )
        attempts: dict[str, int] = {}
        for accession, form, filed, report_date, periods in filings:
            attempt = attempts.get(form, 0)
            attempts[form] = attempt + 1
            self._attempt += 1
            dispatch = self.missions.queue_statement_dispatch(
                authorization=authorization, form=form, attempt=attempt)
            self.missions.mark_statement_dispatch_launched(
                dispatch["dispatch_id"], f"sec-financials-run:{self._attempt:024d}")
            self.missions.record_statement_observation(
                dispatch_id=dispatch["dispatch_id"],
                observation={
                    "schema_version": "0.1", "cik": cik,
                    "entity_name": "Accenture plc",
                    "filings": [{
                        "accession": accession, "form": form, "filed": filed,
                        "report_date": report_date,
                        "lines": self.filing_lines(periods, report_date, drop=drop),
                    }],
                    "source_record_refs": ["raw-sink:" + "c" * 64],
                    "next_cursor": None, "provider_status": 200,
                },
                governance_ref="g", governance_hash="b" * 64)
            self.missions.settle_statement_dispatch(
                dispatch["dispatch_id"], outcome="succeeded")

    # -- running -----------------------------------------------------------

    def run_child(self, **kwargs):
        # The child opens its own store against the same file; this one must
        # not be holding a write transaction across it.
        return run_valuation_snapshot(
            state_dir=self.state_dir, summary_dir=self.state_dir, **kwargs)

    def metrics(self, snapshot):
        return {item["metric"]: item for item in snapshot["metrics"]}

    def coverage(self, snapshot):
        return {item["input"]: item for item in snapshot["input_coverage"]}


class ArithmeticTests(ValuationLaneHarness):
    def setUp(self) -> None:
        super().setUp()
        self.publish_prices()
        self.ingest_filings()
        self.summary = self.run_child()
        self.snapshot = self.snapshots.latest_version(ACN)
        self.by_metric = self.metrics(self.snapshot)

    def test_the_run_publishes_one_snapshot(self):
        self.assertEqual(self.summary["status"], "succeeded")
        self.assertEqual(self.summary["snapshot_status"], "published")
        self.assertEqual(self.summary["company_ref"], ACN)
        self.assertEqual(self.summary["available_metric_count"], 4)
        self.assertEqual(self.summary["formal_authority_writes"], 0)
        self.assertEqual(self.summary["cost_micros"], 0)

    def test_every_metric_matches_the_hand_computed_number(self):
        self.assertEqual(self.by_metric["trailing_pe"]["value"], "5")
        self.assertEqual(self.by_metric["price_to_sales"]["value"], "0.5")
        self.assertEqual(self.by_metric["ev_to_ebitda"]["value"], "4.4828")
        self.assertEqual(self.by_metric["fcf_yield"]["value"], "0.16")
        for item in self.by_metric.values():
            self.assertEqual(item["status"], "available", item)

    def test_the_market_cap_and_the_enterprise_value_are_both_stated(self):
        self.assertEqual(self.snapshot["basis"]["market_cap"], "5000")
        self.assertEqual(self.snapshot["basis"]["enterprise_value"], "6500")
        self.assertEqual(self.snapshot["basis"]["enterprise_value_formula"],
                         "market_cap + total_debt - cash_and_equivalents")
        self.assertIsNone(self.snapshot["basis"]["enterprise_value_reason"])

    def test_the_priced_bar_is_the_newest_settled_one(self):
        self.assertEqual(self.snapshot["as_of"], "2026-03-31")
        self.assertEqual(self.snapshot["price"]["close"], "50")
        self.assertEqual(self.snapshot["basis"]["price_history_bars"], BARS)

    def test_every_number_names_the_version_it_came_from(self):
        series = self.prices.series(ACN)
        self.assertEqual(self.snapshot["price"]["version_ref"], series["version_ref"])
        self.assertEqual(self.snapshot["price"]["version_hash"], series["version_hash"])
        self.assertEqual(self.snapshot["shares"]["version_ref"], series["version_ref"])
        self.assertEqual(self.snapshot["shares"]["shares_outstanding"], "100")
        self.assertEqual(self.snapshot["price"]["invocation_ref"], INVOCATION)
        self.assertEqual(self.snapshot["price"]["artifact_hash"], ARTIFACT)
        accessions = {accession for accession, *_rest in FILINGS}
        window = self.snapshot["fundamental_windows"][-1]
        for role in window["roles"].values():
            self.assertEqual(role["source_ref"], "source:sec-edgar")
            self.assertEqual(role["unit"], "USD")
            for component in role["components"]:
                self.assertIn(component["accession"], accessions)

    def test_the_fourth_quarter_is_derived_and_names_both_filings(self):
        components = self.snapshot["fundamental_windows"][-1][
            "roles"]["net_income"]["components"]
        self.assertEqual(len(components), 4)
        bases = [item["basis"] for item in components]
        self.assertEqual(bases[:3], [COMPONENT_REPORTED] * 3)
        fourth = components[-1]
        self.assertEqual(fourth["basis"], COMPONENT_DERIVED)
        self.assertEqual(fourth["period_start"], "2025-10-01")
        self.assertEqual(fourth["period_end"], "2025-12-31")
        self.assertEqual(fourth["value"], "400")
        self.assertEqual(
            [(part["period_end"], part["value"], part["accession"])
             for part in fourth["derived_from"]],
            [("2025-09-30", "600", "0001467373-25-000003"),
             ("2025-12-31", "1000", "0001467373-26-000001")])

    def test_the_authority_redoes_the_subtraction_rather_than_trusting_it(self):
        window = json.loads(json.dumps(self.snapshot["fundamental_windows"][-1]))
        roles = {
            role: {key: detail[key] for key in
                   ("concept", "statement", "unit", "source_ref", "components")}
            for role, detail in window["roles"].items()
        }
        roles["net_income"]["components"][-1]["value"] = "401"
        with self.assertRaises(ValuationSnapshotConflict) as caught:
            self.snapshots.publish_snapshot(
                company_ref="company:sec-cik:0000000009",
                price=self.snapshot["price"], shares=self.snapshot["shares"],
                fundamental_windows=[{"as_of": window["as_of"], "roles": roles}],
            )
        self.assertIn("difference", str(caught.exception))

    def test_the_percentile_says_what_it_rests_on(self):
        percentile = self.by_metric["trailing_pe"]["percentile"]
        self.assertEqual(percentile["sample_size"], BARS)
        self.assertEqual(percentile["value"], "100")
        self.assertEqual(percentile["basis"], "price_only")
        self.assertEqual(percentile["shares_basis"],
                         "current_shares_applied_to_history")


class IdempotenceTests(ValuationLaneHarness):
    def setUp(self) -> None:
        super().setUp()
        self.publish_prices()
        self.ingest_filings()
        self.run_child()

    def rows(self) -> int:
        return self.store.connection.execute(
            "SELECT COUNT(*) n FROM valuation_snapshot_versions").fetchone()["n"]

    def test_a_second_run_over_unchanged_inputs_publishes_nothing(self):
        self.assertEqual(self.rows(), 1)
        second = self.run_child()
        self.assertEqual(second["status"], "idle")
        self.assertEqual(second["snapshot_status"], "nothing_to_price")
        self.assertEqual(self.rows(), 1)

    def test_the_held_fingerprint_is_the_one_the_producer_computed(self):
        snapshot = self.snapshots.latest_version(ACN)
        inputs = build_inputs(
            missions=self.missions, prices=self.prices, company_ref=ACN,
            calendar=[day for day, _close in _bars()])
        self.assertEqual(held_fingerprint(snapshot), inputs["fingerprint"])

    def test_a_new_bar_is_a_new_snapshot(self):
        closes = _bars(count=BARS + 1)
        self.publish_prices(closes=closes)
        summary = self.run_child()
        self.assertEqual(summary["snapshot_status"], "published")
        self.assertEqual(self.rows(), 2)
        self.assertEqual(self.snapshots.latest_version(ACN)["version"], 2)


class MissingInputTests(ValuationLaneHarness):
    def test_a_company_with_no_total_debt_still_gets_a_snapshot(self):
        self.publish_prices()
        self.ingest_filings(drop=("total_debt",))
        summary = self.run_child()
        self.assertEqual(summary["snapshot_status"], "published")
        self.assertEqual(summary["missing_inputs"], ["total_debt"])
        snapshot = self.snapshots.latest_version(ACN)
        by_metric = self.metrics(snapshot)
        self.assertEqual(by_metric["trailing_pe"]["value"], "5")
        self.assertEqual(by_metric["ev_to_ebitda"]["status"], "unavailable")
        self.assertIsNone(snapshot["basis"]["enterprise_value"])
        self.assertIn("total debt", snapshot["basis"]["enterprise_value_reason"])
        coverage = self.coverage(snapshot)
        self.assertEqual(coverage["total_debt"]["status"], "missing")
        self.assertIn("数据源缺失", coverage["total_debt"]["detail"])
        self.assertIn("us-gaap:DebtLongtermAndShorttermCombinedAmount",
                      coverage["total_debt"]["concept"])
        self.assertEqual(coverage["net_income"]["status"], "held")

    def test_a_company_with_no_trailing_year_still_gets_its_market_cap(self):
        # Only the three 10-Qs: the fiscal year is never filed, so there is no
        # cumulative figure to take the fourth quarter out of and no trailing
        # year at all. The balance sheet is still filed, so the market cap and
        # the enterprise value are still facts -- and holding the snapshot back
        # would take those away too and say nothing about why.
        self.publish_prices()
        self.ingest_filings(filings=FILINGS[:3])
        summary = self.run_child()
        self.assertEqual(summary["snapshot_status"], "published")
        self.assertEqual(summary["available_metric_count"], 0)
        snapshot = self.snapshots.latest_version(ACN)
        self.assertEqual(snapshot["basis"]["market_cap"], "5000")
        self.assertEqual(snapshot["basis"]["enterprise_value"], "6500")
        coverage = self.coverage(snapshot)
        self.assertEqual(coverage["net_income"]["status"], "missing")
        self.assertIn("凑不满 4 个", coverage["net_income"]["detail"])
        self.assertEqual(coverage["total_debt"]["status"], "held")
        for item in self.metrics(snapshot).values():
            self.assertEqual(item["status"], "unavailable")


class FreshnessTests(ValuationLaneHarness):
    def setUp(self) -> None:
        super().setUp()
        self.ingest_filings()

    def calendar_and_scan(self):
        mission = self.missions.mission(self.mission["id"])
        return {row["company_ref"]: row
                for row in scan(self.store, self.missions, self.prices, mission)}

    def test_a_price_three_trading_days_behind_is_still_priced(self):
        self.publish_prices()
        self.publish_prices(company_ref=CTSH, ticker="CTSH",
                            closes=_bars(count=BARS + MAX_PRICE_TRADING_DAY_LAG))
        rows = self.calendar_and_scan()
        self.assertEqual(rows[ACN]["action"], "publish")

    def test_a_price_four_trading_days_behind_is_refused(self):
        self.publish_prices()
        self.publish_prices(company_ref=CTSH, ticker="CTSH",
                            closes=_bars(count=BARS + MAX_PRICE_TRADING_DAY_LAG + 1))
        rows = self.calendar_and_scan()
        self.assertEqual(rows[ACN]["action"], "unavailable")
        self.assertIn("落后 4 个交易日", rows[ACN]["reason"])
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) n FROM valuation_snapshot_versions"
        ).fetchone()["n"], 0)

    def test_a_company_with_no_price_series_says_so(self):
        self.publish_prices()
        rows = self.calendar_and_scan()
        self.assertEqual(rows[CTSH]["action"], "unavailable")
        self.assertIn("价格序列", rows[CTSH]["reason"])


class GrantTests(ValuationLaneHarness):
    grants = ("market_price",)

    def test_a_mission_without_the_grant_publishes_nothing(self):
        self.publish_prices()
        self.ingest_filings()
        self.assertFalse(may_write_valuation(self.mission))
        summary = self.run_child()
        self.assertEqual(summary["status"], "succeeded")
        self.assertTrue(str(summary["snapshot_status"]).startswith("refused:"))
        self.assertIn("valuation", summary["snapshot_status"])
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) n FROM valuation_snapshot_versions"
        ).fetchone()["n"], 0)


class StubLauncher:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.ticket: dict[str, Any] | None = None

    def start(self, *, company_ref: str, fingerprint: str) -> dict[str, Any]:
        self.started.append({"company_ref": company_ref, "fingerprint": fingerprint})
        self.ticket = {
            "id": f"valuation-snapshot-run:{len(self.started):024d}",
            "company_ref": company_ref, "fingerprint": fingerprint,
            "status": "running", "summary": {},
        }
        return self.ticket

    def status(self, ticket_ref: str) -> dict[str, Any]:
        return dict(self.ticket or {})

    def settle(self, **summary: Any) -> None:
        self.ticket = {**(self.ticket or {}), "status": "succeeded",
                       "summary": summary}


class LaneTests(ValuationLaneHarness):
    def coordinator(self, launcher):
        return ValuationSnapshotLaneCoordinator(
            store=self.store, missions=self.missions, prices=self.prices,
            launcher=launcher, mission=lambda: self.mission)

    def test_the_lane_launches_one_company_and_settles_it(self):
        self.publish_prices()
        self.ingest_filings()
        launcher = StubLauncher()
        lane = self.coordinator(launcher)
        first = lane.dispatch_once()
        self.assertEqual(first["status"], "launched")
        self.assertEqual(first["company_ref"], ACN)
        self.assertEqual(first["missing_inputs"], [])
        busy = lane.dispatch_once()
        self.assertEqual(busy["status"], "busy")
        launcher.settle(snapshot_status="published", available_metric_count=4)
        settled = lane.dispatch_once()
        self.assertEqual(settled["settled"]["snapshot_status"], "published")

    def test_a_lane_with_nothing_to_do_reads_one_signature(self):
        self.publish_prices()
        self.ingest_filings()
        self.run_child()
        lane = self.coordinator(StubLauncher())
        first = lane.dispatch_once()
        self.assertEqual(first["status"], "idle")
        self.assertIn("输入一致", first["reason"])
        # Second tick inside the scan floor: answered from the signature alone.
        second = lane.dispatch_once()
        self.assertEqual(second["status"], "idle")
        self.assertIn("没有新版本", second["reason"])

    def test_a_mission_without_the_grant_is_ungranted_not_broken(self):
        self.publish_prices()
        launcher = StubLauncher()
        lane = ValuationSnapshotLaneCoordinator(
            store=self.store, missions=self.missions, prices=self.prices,
            launcher=launcher, mission=lambda: {"autonomy": {"may_write": []}})
        result = lane.dispatch_once()
        self.assertEqual(result["status"], "ungranted")
        self.assertIn("valuation", result["reason"])
        self.assertEqual(launcher.started, [])

    def test_the_signature_moves_when_an_input_authority_does(self):
        before = ledger_signature(self.store.connection)
        self.publish_prices()
        self.assertNotEqual(before, ledger_signature(self.store.connection))

    def test_the_lane_is_registered_in_tick_order(self):
        from dalton_core.lane_registry import tick_lanes

        spec = next(item for item in tick_lanes()
                    if item.operation == "dispatch_valuation_snapshot")
        self.assertEqual(spec.order, 97)
        self.assertEqual(spec.driver_key, "valuation_snapshot")
        self.assertEqual(spec.init_kwarg, "valuation_snapshot_launcher")

class AmazonConceptTests(ValuationLaneHarness):
    """Net income only on the cash-flow statement, capex as productive assets."""

    flows = {
        **FLOWS,
        "net_income": ("cash", "us-gaap:NetIncomeLoss", *FLOWS["net_income"][2:]),
        "capital_expenditure": ("cash", "us-gaap:PaymentsToAcquireProductiveAssets",
                                *FLOWS["capital_expenditure"][2:]),
    }

    def test_the_same_facts_price_the_same_multiples(self):
        self.publish_prices()
        self.ingest_filings()
        summary = self.run_child()
        self.assertEqual(summary["snapshot_status"], "published")
        snapshot = self.snapshots.latest_version(ACN)
        by_metric = self.metrics(snapshot)
        self.assertEqual(by_metric["trailing_pe"]["value"], "5")
        self.assertEqual(by_metric["fcf_yield"]["value"], "0.16")
        coverage = self.coverage(snapshot)
        self.assertEqual(coverage["net_income"]["status"], "held")
        self.assertEqual(coverage["capital_expenditure"]["status"], "held")
        used = json.dumps(snapshot, sort_keys=True)
        self.assertIn('"statement": "cash"', used)
        self.assertIn("us-gaap:PaymentsToAcquireProductiveAssets", used)
