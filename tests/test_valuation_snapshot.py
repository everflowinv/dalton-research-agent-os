"""P11c: valuation as arithmetic on refs, and the gate that used to be shut.

Every number in a snapshot has a version ref, a bar date, or a concept and an
accession behind it. The formulas are frozen, written into every record, and
checked here against numbers computed by hand:

    market cap = 100 x 1,000,000                    = 100,000,000
    trailing P/E = 100,000,000 / 10,000,000         = 10
    price / sales = 100,000,000 / 50,000,000        = 2
    EV = 100,000,000 + 20,000,000 - 5,000,000       = 115,000,000
    EBITDA = 12,000,000 + 2,000,000                 = 14,000,000
    EV / EBITDA = 115,000,000 / 14,000,000          = 8.2143 (4 dp)
    FCF = 14,000,000 - 4,000,000                    = 10,000,000
    FCF yield = 10,000,000 / 100,000,000            = 0.1

The last class is the point of the whole slice: ``VALUATION_AUTHORITY_ROLES``
demanded five authorities of which none existed, so no valuation could ever be
published. With a price and a share count in hand, that door opens -- and only
that far.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from dalton_core.store import DaltonStore, content_hash
from dalton_core.valuation_snapshot import (
    FORMULA_VERSION,
    METRICS,
    MIN_PERCENTILE_SAMPLE,
    PERCENTILE_METHOD,
    ValuationSnapshotAuthority,
    ValuationSnapshotConflict,
    ValuationSnapshotValidationError,
)

ACN = "company:sec-cik:0001467373"
ACCESSION = "0001467373-26-000001"
PRICE_VERSION = "market-price-series-version:" + "a" * 32
PRICED_ON = "2026-09-08"
QUARTER_ENDS = ("2025-11-30", "2026-02-28", "2026-05-31", "2026-08-31")
QUARTER_STARTS = ("2025-09-01", "2025-12-01", "2026-03-01", "2026-06-01")


def flow(value, *, source_ref="source:sec-edgar", concept="Concept"):
    return {
        "concept": concept, "statement": "income", "unit": "USD",
        "source_ref": source_ref,
        "components": [
            {"period_start": start, "period_end": end,
             "value": value, "accession": ACCESSION}
            for start, end in zip(QUARTER_STARTS, QUARTER_ENDS)
        ],
    }


def instant(value, *, source_ref="source:sec-edgar", concept="Concept"):
    return {
        "concept": concept, "statement": "balance", "unit": "USD",
        "source_ref": source_ref,
        "components": [{"period_start": None, "period_end": "2026-08-31",
                        "value": value, "accession": ACCESSION}],
    }


def roles(**overrides):
    base = {
        "net_income": flow("2500000", concept="NetIncomeLoss"),
        "revenue": flow("12500000", concept="Revenues"),
        "operating_income": flow("3000000", concept="OperatingIncomeLoss"),
        "depreciation_amortisation": flow(
            "500000", concept="DepreciationDepletionAndAmortization"),
        "total_debt": instant("20000000", concept="DebtLongtermAndShorttermCombinedAmount"),
        "cash_and_equivalents": instant(
            "5000000", concept="CashAndCashEquivalentsAtCarryingValue"),
        "operating_cash_flow": flow(
            "3500000", concept="NetCashProvidedByUsedInOperatingActivities"),
        "capital_expenditure": flow(
            "1000000", concept="PaymentsToAcquirePropertyPlantAndEquipment"),
    }
    for key, value in overrides.items():
        if value is None:
            base.pop(key, None)
        else:
            base[key] = value
    return base


def history(bars=40, last=PRICED_ON, first_close=60):
    start = date.fromisoformat(last) - timedelta(days=bars - 1)
    return [
        {"date": (start + timedelta(days=index)).isoformat(),
         "close": str(first_close + index)}
        for index in range(bars)
    ]


class ValuationTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.authority = ValuationSnapshotAuthority(self.store)

    def price(self, close="100", bar_date=PRICED_ON):
        return {
            "version_ref": PRICE_VERSION, "version_hash": "b" * 64,
            "bar_date": bar_date, "close": close, "currency": "USD",
            "invocation_ref": "connector-invocation:yfinance:" + "c" * 32,
            "artifact_hash": "d" * 64,
        }

    def shares(self, count="1000000", as_of=PRICED_ON):
        return {
            "version_ref": PRICE_VERSION, "version_hash": "b" * 64,
            "as_of": as_of, "shares_outstanding": count,
            "invocation_ref": "connector-invocation:yfinance:" + "c" * 32,
            "artifact_hash": "d" * 64,
        }

    def publish(self, *, windows=None, price_history=None, **overrides):
        request = {
            "company_ref": ACN,
            "price": self.price(),
            "shares": self.shares(),
            "fundamental_windows": windows if windows is not None else [
                {"as_of": "2025-12-31", "roles": roles()}],
            "price_history": history() if price_history is None else price_history,
        }
        request.update(overrides)
        return self.authority.publish_snapshot(**request)

    def metrics(self, published):
        return {item["metric"]: item for item in published["metrics"]}


class FormulaTests(ValuationTestCase):
    def setUp(self):
        super().setUp()
        self.published = self.publish()
        self.by_metric = self.metrics(self.published)

    def test_the_market_cap_is_the_close_times_the_share_count(self):
        self.assertEqual(self.published["basis"]["market_cap"], "100000000")
        self.assertEqual(
            self.published["basis"]["market_cap_formula"],
            "close * shares_outstanding")

    def test_every_metric_matches_the_hand_computed_number(self):
        self.assertEqual(self.by_metric["trailing_pe"]["value"], "10")
        self.assertEqual(self.by_metric["price_to_sales"]["value"], "2")
        self.assertEqual(self.by_metric["ev_to_ebitda"]["value"], "8.2143")
        self.assertEqual(self.by_metric["fcf_yield"]["value"], "0.1")
        for item in self.by_metric.values():
            self.assertEqual(item["status"], "available")

    def test_the_formula_is_written_into_every_record(self):
        self.assertEqual(self.published["formula_version"], FORMULA_VERSION)
        self.assertEqual(
            self.by_metric["ev_to_ebitda"]["formula"],
            "(market_cap + total_debt - cash_and_equivalents) / "
            "(operating_income_ttm + depreciation_amortisation_ttm)")

    def test_it_is_derived_deterministic_and_says_so(self):
        self.assertEqual(self.published["kind"], "derived_deterministic")

    def test_all_four_metrics_are_produced(self):
        self.assertEqual(set(self.by_metric), set(METRICS))

    def test_every_input_is_a_ref(self):
        self.assertEqual(self.published["price"]["version_ref"], PRICE_VERSION)
        self.assertEqual(self.published["price"]["bar_date"], PRICED_ON)
        self.assertEqual(self.published["shares"]["as_of"], PRICED_ON)
        window = self.published["fundamental_windows"][0]
        for role in window["roles"].values():
            self.assertEqual(role["source_ref"], "source:sec-edgar")
            for component in role["components"]:
                self.assertEqual(component["accession"], ACCESSION)

    def test_a_price_that_doubles_doubles_the_multiple(self):
        # The arithmetic is replayable rather than remembered.
        published = self.authority.publish_snapshot(
            company_ref="company:sec-cik:0000000001",
            price=self.price(close="200"), shares=self.shares(),
            fundamental_windows=[{"as_of": "2025-12-31", "roles": roles()}],
            price_history=history(),
        )
        self.assertEqual(self.metrics(published)["trailing_pe"]["value"], "20")


class PercentileTests(ValuationTestCase):
    def test_a_price_above_its_whole_history_is_the_hundredth_percentile(self):
        published = self.publish()
        by_metric = self.metrics(published)
        percentile = by_metric["trailing_pe"]["percentile"]
        self.assertEqual(percentile["value"], "100")
        self.assertEqual(percentile["method"], PERCENTILE_METHOD)
        self.assertEqual(percentile["sample_size"], 40)
        # And the yield, which moves the other way, is at the bottom.
        self.assertEqual(by_metric["fcf_yield"]["percentile"]["value"], "0")

    def test_the_percentile_is_the_share_of_history_at_or_below(self):
        # Thirty closes from 1 to 30 and a priced bar at 15: fifteen of the
        # thirty are at or below it.
        bars = [{"date": (date.fromisoformat(PRICED_ON)
                          - timedelta(days=29 - index)).isoformat(),
                 "close": str(index + 1)} for index in range(30)]
        published = self.publish(price=self.price(close="15"), price_history=bars)
        self.assertEqual(
            self.metrics(published)["trailing_pe"]["percentile"]["value"], "50")

    def test_a_thin_history_gets_no_percentile_and_says_how_thin(self):
        published = self.publish(price_history=history(bars=10, first_close=90))
        percentile = self.metrics(published)["trailing_pe"]["percentile"]
        self.assertIsNone(percentile["value"])
        self.assertEqual(percentile["sample_size"], 10)
        self.assertIn(str(MIN_PERCENTILE_SAMPLE), percentile["reason"])

    def test_one_fundamental_window_is_declared_as_price_only(self):
        # An honest label rather than a hidden limitation: with the
        # fundamentals fixed, the multiple's percentile is the price's
        # percentile in multiple clothing.
        published = self.publish()
        self.assertEqual(
            self.metrics(published)["trailing_pe"]["percentile"]["basis"],
            "price_only")

    def test_several_dated_windows_make_the_history_real(self):
        published = self.publish(windows=[
            {"as_of": "2025-01-31", "roles": roles(
                net_income=flow("2000000", concept="NetIncomeLoss"))},
            {"as_of": "2025-12-31", "roles": roles()},
        ])
        self.assertEqual(
            self.metrics(published)["trailing_pe"]["percentile"]["basis"],
            "price_and_filed_fundamentals")
        self.assertEqual(published["basis"]["fundamental_window_count"], 2)

    def test_a_bar_older_than_every_filing_is_left_out_not_back_filled(self):
        # Valuing a 2024 price against a 2026 filing is a percentile that knows
        # the future.
        bars = history(bars=60, first_close=40)
        published = self.publish(
            windows=[{"as_of": bars[20]["date"], "roles": roles()}],
            price_history=bars,
        )
        self.assertEqual(
            self.metrics(published)["trailing_pe"]["percentile"]["sample_size"], 40)


class UnavailableTests(ValuationTestCase):
    def test_a_missing_concept_makes_one_metric_unavailable_not_the_snapshot(self):
        published = self.publish(windows=[
            {"as_of": "2025-12-31", "roles": roles(operating_income=None)}])
        by_metric = self.metrics(published)
        self.assertEqual(by_metric["ev_to_ebitda"]["status"], "unavailable")
        self.assertIsNone(by_metric["ev_to_ebitda"]["value"])
        self.assertIn("operating income", by_metric["ev_to_ebitda"]["reason"])
        # The others still compute: one absent concept is not four.
        self.assertEqual(by_metric["trailing_pe"]["value"], "10")

    def test_a_loss_making_company_gets_no_pe_and_a_sentence(self):
        published = self.publish(windows=[{"as_of": "2025-12-31", "roles": roles(
            net_income=flow("-2500000", concept="NetIncomeLoss"))}])
        pe = self.metrics(published)["trailing_pe"]
        self.assertEqual(pe["status"], "unavailable")
        # Not "-40x", which invites a reader to compare it with 40x.
        self.assertIn("not positive", pe["reason"])

    def test_a_negative_free_cash_flow_is_a_number_not_an_error(self):
        published = self.publish(windows=[{"as_of": "2025-12-31", "roles": roles(
            capital_expenditure=flow(
                "5000000", concept="PaymentsToAcquirePropertyPlantAndEquipment"))}])
        yield_ = self.metrics(published)["fcf_yield"]
        self.assertEqual(yield_["status"], "available")
        self.assertEqual(Decimal(yield_["value"]), Decimal("-0.06"))

    def test_a_price_older_than_every_filing_gets_no_metric_and_says_why(self):
        published = self.publish(
            windows=[{"as_of": "2026-09-30", "roles": roles()}])
        for item in published["metrics"]:
            self.assertEqual(item["status"], "unavailable")
            self.assertIn("no filing was on hand", item["reason"])


class RefusalTests(ValuationTestCase):
    def test_a_fundamental_from_yahoo_is_refused_by_source(self):
        # The refusal this whole module is built around. A P/E on a scraped
        # earnings number looks exactly like a P/E on a filed one.
        with self.assertRaises(ValuationSnapshotConflict) as caught:
            self.publish(windows=[{"as_of": "2025-12-31", "roles": roles(
                net_income=flow("2500000", source_ref="source:yahoo-finance"))}])
        self.assertIn("primary source", str(caught.exception))

    def test_a_three_quarter_trailing_window_is_refused(self):
        # Three quarters read as a year understates every multiple built on it.
        short = flow("2500000")
        short["components"] = short["components"][:3]
        with self.assertRaises(ValuationSnapshotConflict) as caught:
            self.publish(windows=[
                {"as_of": "2025-12-31", "roles": roles(net_income=short)}])
        self.assertIn("exactly 4", str(caught.exception))

    def test_a_balance_read_as_a_flow_is_refused(self):
        with self.assertRaises(ValuationSnapshotConflict):
            self.publish(windows=[{"as_of": "2025-12-31", "roles": roles(
                total_debt=flow("20000000"))}])

    def test_the_same_quarter_counted_twice_is_refused(self):
        doubled = flow("2500000")
        doubled["components"][1] = copy.deepcopy(doubled["components"][0])
        with self.assertRaises(ValuationSnapshotConflict):
            self.publish(windows=[
                {"as_of": "2025-12-31", "roles": roles(net_income=doubled)}])

    def test_a_component_without_an_accession_is_refused(self):
        orphan = flow("2500000")
        orphan["components"][0]["accession"] = "somewhere"
        with self.assertRaises(ValuationSnapshotValidationError):
            self.publish(windows=[
                {"as_of": "2025-12-31", "roles": roles(net_income=orphan)}])

    def test_capital_expenditure_handed_over_pre_negated_is_refused(self):
        # Subtracting an already-negative capex would double the free cash flow.
        with self.assertRaises(ValuationSnapshotConflict) as caught:
            self.publish(windows=[{"as_of": "2025-12-31", "roles": roles(
                capital_expenditure=flow("-1000000"))}])
        self.assertIn("sign convention", str(caught.exception))

    def test_a_role_no_metric_uses_is_refused(self):
        with self.assertRaises(ValuationSnapshotValidationError):
            self.publish(windows=[
                {"as_of": "2025-12-31",
                 "roles": {**roles(), "vibes": flow("1")}}])

    def test_a_priced_bar_newer_than_its_own_history_is_refused(self):
        with self.assertRaises(ValuationSnapshotConflict):
            self.publish(price_history=history(last="2026-09-01"))


class VersionChainTests(ValuationTestCase):
    def test_the_same_inputs_twice_is_a_duplicate(self):
        first = self.publish()
        again = self.publish()
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["version"], 1)

    def test_a_new_price_publishes_a_new_version(self):
        first = self.publish()
        second = self.publish(price=self.price(close="110"))
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["prior_version_ref"], first["id"])
        self.assertEqual(self.metrics(second)["trailing_pe"]["value"], "11")
        # And version one still says 10.
        self.assertEqual(
            self.metrics(self.authority.version(first["id"]))["trailing_pe"]["value"],
            "10")

    def test_a_restated_filing_publishes_a_new_version(self):
        self.publish()
        second = self.publish(windows=[{"as_of": "2025-12-31", "roles": roles(
            net_income=flow("2600000", concept="NetIncomeLoss"))}])
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(second["version"], 2)

    def test_a_stored_snapshot_reads_back_by_hash(self):
        published = self.publish()
        stored = self.authority.version(published["id"])
        self.assertEqual(stored["content_hash"], published["content_hash"])
        self.assertEqual(self.authority.latest_version(ACN)["id"], published["id"])


class ValuationGateTests(unittest.TestCase):
    """The gate P11c was asked to unfreeze, and how far it now opens.

    ``VALUATION_AUTHORITY_ROLES`` demanded price, shares, FX, rates *and*
    consensus, and this system had none of them -- so "a valuation needs all
    five" meant "no valuation may ever be published". P11a supplies the two a
    multiple is arithmetically impossible without; the other three stay in the
    vocabulary, stay checked when claimed, and stop being required.
    """

    PERIOD = {
        "start": "2026-01-01", "end": "2026-09-08",
        "calendar": "company:fiscal", "kind": "quarter",
    }

    def setUp(self):
        from dalton_core.model_input import ModelInputLedger

        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.ledger = ModelInputLedger(self.store)
        self.evidence = self.store.register_evidence({
            "evidence_ref": "evidence:acn:market-price",
            "source_type": "connector",
            "source_ref": "source:yahoo-finance",
            "retrieved_at": "2026-09-09T12:00:00+00:00",
            "source_lineage": [PRICE_VERSION],
            "independence_group": "source:yahoo-finance",
            "actor_ref": "core:market-price-worker",
        })
        self.scenario = self.admit("scenario", "scenario:base", {
            "schema_version": "0.1", "scenario_ref": "scenario:base",
            "label": "Base", "description": "the market layer is wired",
            "base_scenario_version_ref": None,
            "base_scenario_version_hash": None, "owner_ref": "human:pm",
        }, suffix="scenario")
        self.price_input = self.admit(
            "actual", "input:acn:price", self.market_payload(
                metric_ref="metric:price", value="100"), suffix="price")
        self.shares_input = self.admit(
            "actual", "input:acn:shares", self.market_payload(
                metric_ref="metric:shares-outstanding", value="1000000"),
            suffix="shares")

    def market_payload(self, *, metric_ref, value):
        return {
            "schema_version": "0.1", "metric_ref": metric_ref,
            "subject_ref": "company:acn", "business_line_ref": None,
            "period": copy.deepcopy(self.PERIOD),
            "unit": "unit", "currency": "USD", "value": value,
            "source_authorities": [{
                "authority_kind": "evidence_version",
                "version_ref": self.evidence["evidence_version_id"],
                "content_hash": self.evidence["content_hash"],
            }],
        }

    def admit(self, kind, ref, payload, *, suffix):
        candidate = self.ledger.propose_input(
            candidate_id=f"candidate:{suffix}", input_kind=kind,
            model_input_ref=ref, prior_version_ref=None, payload=payload,
            proposed_by="automation:coverage-mission",
            idempotency_key=f"propose:{suffix}",
        )["candidate"]
        return self.ledger.decide_input(
            decision_id=f"decision:{suffix}", candidate_id=candidate["id"],
            candidate_hash=candidate["content_hash"], verdict="admit",
            rationale="the market layer published it", findings=[],
            reviewer_ref="human:pm", version_id=f"input-version:{suffix}",
            idempotency_key=f"decide:{suffix}",
        )["version"]

    def binding(self, ref, role, version):
        return {
            "binding_ref": ref, "role": role, "version_ref": version["id"],
            "version_hash": version["content_hash"],
        }

    def record(self, bindings, authorities):
        return self.ledger.record_model_run(
            version_id="run:valuation:1", model_run_ref="run:valuation",
            prior_version_ref=None,
            scenario_version_ref=self.scenario["id"],
            scenario_version_hash=self.scenario["content_hash"],
            input_bindings=bindings,
            formula_version_ref=FORMULA_VERSION,
            formula_version_hash=content_hash({"formula": FORMULA_VERSION}),
            status="completed", outputs=[{
                "output_ref": "valuation:acn", "output_kind": "valuation",
                "metric_ref": "metric:trailing-pe",
                "period": copy.deepcopy(self.PERIOD),
                "unit": "ratio", "currency": "USD", "value": "10",
                "authority_bindings": authorities,
            }], errors=[], started_at="2026-09-09T12:00:00+00:00",
            completed_at="2026-09-09T12:00:01+00:00",
            actor_ref="core:valuation-snapshot-worker",
            idempotency_key="run:valuation",
        )

    def test_price_alone_is_still_not_enough(self):
        from dalton_core.model_input import ModelInputConflict

        with self.assertRaises(ModelInputConflict) as caught:
            self.record(
                [self.binding("market:price", "price", self.price_input)],
                [{"role": "price", "binding_ref": "market:price"}],
            )
        self.assertIn("shares", str(caught.exception))

    def test_price_and_shares_together_open_the_gate(self):
        # The whole point of P11a: before the market layer existed neither of
        # these could be produced, so this path was closed by construction.
        run = self.record(
            [
                self.binding("market:price", "price", self.price_input),
                self.binding("market:shares", "shares", self.shares_input),
            ],
            [
                {"role": "price", "binding_ref": "market:price"},
                {"role": "shares", "binding_ref": "market:shares"},
            ],
        )["model_run"]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["outputs"][0]["output_kind"], "valuation")
        self.assertEqual(run["outputs"][0]["value"], "10")
        self.assertEqual(
            {item["role"] for item in run["outputs"][0]["authority_bindings"]},
            {"price", "shares"})

    def test_a_role_outside_the_vocabulary_is_still_refused(self):
        from dalton_core.model_input import ModelInputValidationError

        vibes = self.admit("actual", "input:acn:vibes", self.market_payload(
            metric_ref="metric:vibes", value="7"), suffix="vibes")
        with self.assertRaises(ModelInputValidationError):
            self.record(
                [
                    self.binding("market:price", "price", self.price_input),
                    self.binding("market:shares", "shares", self.shares_input),
                    self.binding("market:vibes", "vibes", vibes),
                ],
                [
                    {"role": "price", "binding_ref": "market:price"},
                    {"role": "shares", "binding_ref": "market:shares"},
                    {"role": "vibes", "binding_ref": "market:vibes"},
                ],
            )

    def test_an_optional_role_is_still_checked_when_it_is_claimed(self):
        from dalton_core.model_input import ModelInputConflict

        # FX is no longer required. It is still not free: claiming it means
        # binding a frozen actual input of that exact role.
        with self.assertRaises(ModelInputConflict):
            self.record(
                [
                    self.binding("market:price", "price", self.price_input),
                    self.binding("market:shares", "shares", self.shares_input),
                ],
                [
                    {"role": "price", "binding_ref": "market:price"},
                    {"role": "shares", "binding_ref": "market:shares"},
                    {"role": "fx", "binding_ref": "market:fx"},
                ],
            )

    def test_the_required_set_is_a_named_constant_not_a_literal(self):
        from dalton_core.model_input import (
            REQUIRED_VALUATION_AUTHORITY_ROLES,
            VALUATION_AUTHORITY_ROLES,
        )

        # Widening it again is a decision about what a valuation means, and it
        # should show up in a diff with a reason.
        self.assertEqual(REQUIRED_VALUATION_AUTHORITY_ROLES, {"price", "shares"})
        self.assertTrue(
            REQUIRED_VALUATION_AUTHORITY_ROLES < VALUATION_AUTHORITY_ROLES)


if __name__ == "__main__":
    unittest.main()
