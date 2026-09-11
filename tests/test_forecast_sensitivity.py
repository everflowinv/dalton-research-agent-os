"""P13-M3: bands, ranking, what-ifs, and the things this layer refuses to say.

The fixture is eight quarters of arithmetic anyone can do on paper. Revenue
alternates +20%, -10%, +10%; the cost share alternates 80%, 82%, 78%; SG&A is
exactly a tenth of revenue every quarter and tax exactly a quarter of operating
income every quarter. That last pair is deliberate: two drivers whose
historical band has no width at all, so the ranking has to break a tie, and it
has to break it the same way on every machine.

The expected band numbers below were worked out by hand and are written out in
full, so a failure here means the rule changed rather than that a fixture
drifted.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from decimal import Decimal

from dalton_core import forecast_sensitivity as fs
from dalton_core.company_model_inputs import build_model_inputs
from dalton_core.company_model_report import render_sensitivity
from dalton_core.forecast_sensitivity import (
    MAX_DRIVERS,
    MIN_BAND_POINTS,
    SCENARIOS,
    SELECTION_RULE,
    SELECTION_RULE_HASH,
    SELECTION_RULE_REF,
    UNIT_MOVE,
    WHAT_IF_LINES,
    SensitivityConflict,
    SensitivityProjectionAuthority,
    SensitivityUnavailable,
    body_hash,
    build_projection,
    candidate_drivers,
    driver_impact,
    driver_swing,
    historical_band,
    horizon_total,
    impact_metric,
    measure_series,
    open_periods,
    projection_readiness,
    recompute,
    select_drivers,
    validate_projection,
)
from dalton_core.model_forecast_driver import (
    ForecastModelAuthority,
    ForecastModelValidationError,
    build_forecast_model,
    revise_assumptions,
)
from dalton_core.store import DaltonStore, canonical_json, content_hash
from tests.test_company_model_inputs import ACN, FakeMissions, _line

REVENUE = "us-gaap:Revenues"
COST = "us-gaap:CostOfGoodsAndServicesSold"
SGA = "us-gaap:SellingGeneralAndAdministrativeExpense"
TAX = "us-gaap:IncomeTaxExpenseBenefit"
RND = "us-gaap:ResearchAndDevelopmentExpense"
DA = "us-gaap:DepreciationAndAmortization"

REVENUE_DRIVER = f"concept:{REVENUE}"
COST_DRIVER = f"concept:{COST}"
SGA_DRIVER = f"concept:{SGA}"
TAX_DRIVER = f"concept:{TAX}"

#: Eight consecutive Accenture fiscal quarters. Every adjacent pair is 90 to 92
#: days apart, so the growth series has seven points and no hole.
QUARTERS = (
    ("2024-06-01", "2024-08-31"),
    ("2024-09-01", "2024-11-30"),
    ("2024-12-01", "2025-02-28"),
    ("2025-03-01", "2025-05-31"),
    ("2025-06-01", "2025-08-31"),
    ("2025-09-01", "2025-11-30"),
    ("2025-12-01", "2026-02-28"),
    ("2026-03-01", "2026-05-31"),
)
# +20%, -10%, +10%, repeating. Mean = 0.6 / 7 = 0.085714285714...
REVENUES = ("1000000000", "1200000000", "1080000000", "1188000000",
            "1425600000", "1283040000", "1411344000", "1693612800")
# 80%, 82%, 78%, 80%, 82%, 78%, 80%, 82%. Mean = 6.42 / 8 = 0.8025.
COSTS = ("800000000", "984000000", "842400000", "950400000",
         "1168992000", "1000771200", "1129075200", "1388762496")
# Exactly a tenth of revenue, every quarter: a band with no width.
SGAS = ("100000000", "120000000", "108000000", "118800000",
        "142560000", "128304000", "141134400", "169361280")
# Exactly a quarter of operating income, every quarter: another one.
TAXES = ("25000000", "24000000", "32400000", "29700000",
         "28512000", "38491200", "35283600", "33872256")

SERIES = {REVENUE: REVENUES, COST: COSTS, SGA: SGAS, TAX: TAXES}


def ledger(series=None):
    series = SERIES if series is None else series
    lines = []
    for concept, values in series.items():
        for (start, end), value in zip(QUARTERS, values):
            lines.append(_line(concept, start, end, value))
    return FakeMissions(lines)


def spec(*, quarters=4, cash="not_material"):
    return {
        "spec_id": "company-model-spec:test", "company_ref": ACN,
        "state_hash": "a" * 64, "content_hash": "b" * 64,
        "revenue_drivers": [{
            "ref": "top-line", "label": "Client work", "kind": "mix",
            "basis_concept": REVENUE, "unit": "USD",
            "because": "The blend moves the total.",
        }],
        "expense_lines": [
            {"ref": "delivery", "label": "Cost of services", "basis_concept": COST,
             "behaviour": "variable_with_revenue", "driver_ref": None,
             "because": "Delivery cost follows revenue."},
            {"ref": "sga", "label": "Selling, general and administrative",
             "basis_concept": SGA, "behaviour": "semi_variable", "driver_ref": None,
             "because": "Sales cost scales with the book."},
            {"ref": "tax", "label": "Income tax", "basis_concept": TAX,
             "behaviour": "variable_with_revenue", "driver_ref": None,
             "because": "The rate is stable."},
        ],
        "operating_metrics": [],
        "forecast_statements": [
            {"statement": "income", "importance": "required", "because": "The question."},
            {"statement": "balance", "importance": "supporting", "because": "Light."},
            {"statement": "cash", "importance": cash, "because": "Judgement."},
        ],
        "horizon": {"historical_quarters": 12, "forecast_quarters": quarters,
                    "because": "The cycle."},
    }


def model(missions=None, specification=None, **kwargs):
    missions = ledger() if missions is None else missions
    specification = spec() if specification is None else specification
    record = build_forecast_model(
        specification, build_model_inputs(missions, specification), **kwargs)
    # Two fields the authority would have supplied; a projection is bound to
    # both, so the fixture has to carry them.
    record["id"] = "forecast-model-version:fixture:1"
    record["content_hash"] = "c" * 64
    return record


def store():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    return DaltonStore(":memory:", connection=connection)


class BandTests(unittest.TestCase):
    def test_the_growth_series_is_every_adjacent_pair_with_its_two_filings(self):
        series = measure_series(model(), REVENUE_DRIVER, "quarterly_growth")
        self.assertEqual(series["status"], "available")
        self.assertEqual(
            [item["value"] for item in series["points"]],
            ["0.200000000000", "-0.100000000000", "0.100000000000",
             "0.200000000000", "-0.100000000000", "0.100000000000",
             "0.200000000000"])
        # Seven points from eight quarters: a rate is measured across a pair.
        self.assertEqual(len(series["points"]), len(QUARTERS) - 1)
        first = series["points"][0]
        self.assertEqual(first["period_end"], "2024-11-30")
        self.assertEqual([ref["period_end"] for ref in first["refs"]],
                         ["2024-08-31", "2024-11-30"])
        self.assertTrue(all(ref["accession"] for ref in first["refs"]))

    def test_the_band_is_the_peak_the_trough_the_mean_and_the_latest(self):
        band = historical_band(
            measure_series(model(), REVENUE_DRIVER, "quarterly_growth"))
        self.assertEqual(band["status"], "available")
        self.assertEqual(band["count"], 7)
        self.assertEqual(band["peak"]["value"], "0.200000000000")
        self.assertEqual(band["trough"]["value"], "-0.100000000000")
        # 0.6 / 7, quantised to twelve places.
        self.assertEqual(band["mean"]["value"], "0.085714285714")
        self.assertEqual(band["latest"]["value"], "0.200000000000")
        self.assertEqual(band["latest"]["period_end"], "2026-05-31")
        self.assertEqual(band["first_period"], "2024-11-30")
        self.assertEqual(band["last_period"], "2026-05-31")

    def test_a_tied_extreme_is_attributed_to_the_first_quarter_that_reached_it(self):
        band = historical_band(
            measure_series(model(), REVENUE_DRIVER, "quarterly_growth"))
        # +20% happens three times; the earliest is the one named, on every
        # machine, because two runs disagreeing about which quarter was the
        # peak is a difference nobody could explain.
        self.assertEqual(band["peak"]["period_end"], "2024-11-30")
        self.assertEqual(band["trough"]["period_end"], "2025-02-28")

    def test_a_share_band_is_measured_against_the_line_it_is_a_share_of(self):
        band = historical_band(measure_series(model(), COST_DRIVER, "revenue_share"))
        self.assertEqual(band["count"], 8)
        self.assertEqual(band["peak"]["value"], "0.820000000000")
        self.assertEqual(band["trough"]["value"], "0.780000000000")
        self.assertEqual(band["mean"]["value"], "0.802500000000")
        self.assertEqual(band["latest"]["value"], "0.820000000000")

    def test_each_extreme_carries_the_next_observation_in(self):
        # Cognizant's tax share peaks at 70.79% in one quarter and the next
        # highest is far below it: that peak is an event, not a rate. Deciding
        # which extremes are outliers would need an exclusion rule nobody has
        # agreed; showing the second one lets a person see the gap.
        band = historical_band(
            measure_series(model(), COST_DRIVER, "revenue_share"))
        self.assertEqual(band["peak"]["value"], "0.820000000000")
        self.assertEqual(band["peak"]["runner_up"]["value"], "0.800000000000")
        self.assertEqual(band["trough"]["value"], "0.780000000000")
        self.assertEqual(band["trough"]["runner_up"]["value"], "0.800000000000")
        # The runner-up is the first quarter at the next *distinct* level, so
        # two quarters tied at the peak do not report each other and hide a gap.
        self.assertEqual(band["peak"]["runner_up"]["period_end"], "2024-08-31")

    def test_a_band_with_no_width_has_no_runner_up(self):
        band = historical_band(measure_series(model(), SGA_DRIVER, "revenue_share"))
        self.assertEqual(band["status"], "available")
        self.assertIsNone(band["peak"]["runner_up"])
        self.assertIsNone(band["trough"]["runner_up"])

    def test_a_band_with_no_width_is_still_a_band(self):
        band = historical_band(measure_series(model(), SGA_DRIVER, "revenue_share"))
        self.assertEqual(band["status"], "available")
        for edge in ("peak", "trough", "mean", "latest"):
            self.assertEqual(band[edge]["value"], "0.100000000000")

    def test_too_little_history_is_unavailable_with_the_count_in_the_reason(self):
        short = {concept: values[:3] for concept, values in SERIES.items()}
        band = historical_band(
            measure_series(model(ledger(short)), COST_DRIVER, "revenue_share"))
        self.assertEqual(band["status"], "unavailable")
        self.assertEqual(band["count"], 3)
        self.assertIn(str(MIN_BAND_POINTS), band["reason"])
        self.assertIsNone(band["peak"])

    def test_a_base_that_changes_sign_gets_no_band_rather_than_a_meaningless_one(self):
        # The guard the model's own generator makes, repeated here. A company
        # that lost money and then made money has a "tax rate" of minus a lot
        # and plus a lot; the higher of the two describes the loss.
        losing = dict(SERIES)
        losing[COST] = ("1200000000", "984000000", "842400000", "950400000",
                        "1168992000", "1000771200", "1129075200", "1388762496")
        series = measure_series(model(ledger(losing)), TAX_DRIVER,
                                "operating_income_share")
        self.assertEqual(series["status"], "unavailable")
        self.assertIn("changes sign", series["reason"])
        self.assertEqual(historical_band(series)["status"], "unavailable")

    def test_a_driver_this_model_does_not_hold_is_a_reason_not_a_crash(self):
        series = measure_series(model(), "concept:us-gaap:Nonsense", "revenue_share")
        self.assertEqual(series["status"], "unavailable")
        self.assertIn("no driver", series["reason"])

    def test_an_unknown_measure_is_refused_rather_than_guessed(self):
        series = measure_series(model(), COST_DRIVER, "log_of_the_moon")
        self.assertEqual(series["status"], "unavailable")
        self.assertIn("log_of_the_moon", series["reason"])


class RankingTests(unittest.TestCase):
    def test_the_metric_is_the_bottom_most_line_that_computes(self):
        metric = impact_metric(model())
        self.assertEqual(metric["status"], "available")
        # Free cash flow first in the precedence, but this specification marks
        # the cash flow statement not_material, so it is not computed.
        self.assertEqual(metric["result_ref"], "result:net_income")
        self.assertEqual(metric["label"], "net income")

    def test_when_net_income_is_unavailable_the_metric_falls_to_operating_income(self):
        # No tax line at all: net income cannot be computed, and the ranking
        # falls one line up rather than refusing the whole projection.
        without_tax = {key: value for key, value in SERIES.items() if key != TAX}
        specification = spec()
        specification["expense_lines"] = [
            row for row in specification["expense_lines"] if row["ref"] != "tax"]
        metric = impact_metric(model(ledger(without_tax), specification))
        self.assertEqual(metric["result_ref"], "result:operating_income")

    def test_every_share_driver_has_the_same_unit_elasticity(self):
        # The finding this whole ranking rule exists because of. One point on
        # cost of revenue and one point on SG&A are both one point of the same
        # forecast revenue, so they move operating income by the same number.
        record = model()
        metric = impact_metric(record)
        cost = driver_impact(record, COST_DRIVER, metric)
        sga = driver_impact(record, SGA_DRIVER, metric)
        self.assertEqual(cost["status"], "computed")
        self.assertEqual(cost["delta"], sga["delta"])

    def test_the_swing_is_what_separates_them(self):
        record = model()
        metric = impact_metric(record)
        cost_band = historical_band(measure_series(record, COST_DRIVER, "revenue_share"))
        sga_band = historical_band(measure_series(record, SGA_DRIVER, "revenue_share"))
        cost = driver_swing(record, COST_DRIVER, cost_band, metric)
        sga = driver_swing(record, SGA_DRIVER, sga_band, metric)
        self.assertEqual(cost["status"], "computed")
        self.assertEqual(sga["swing"], "0.00000000")
        self.assertGreater(Decimal(cost["swing"]), Decimal(sga["swing"]))

    def test_the_ranking_puts_the_widest_band_first_and_breaks_ties_by_ref(self):
        picked = select_drivers(model())
        self.assertEqual(picked["selection"]["status"], "available")
        order = [item["driver"]["ref"] for item in picked["ranked"]]
        self.assertEqual(order[:2], [REVENUE_DRIVER, COST_DRIVER])
        # SG&A and tax both swing exactly nothing; the tie is broken by ref, so
        # income tax sorts before selling, general and administrative.
        self.assertEqual(order[2:], [TAX_DRIVER, SGA_DRIVER])

    def test_the_ranking_is_stable_across_runs(self):
        record = model()
        first = [item["driver"]["ref"] for item in select_drivers(record)["ranked"]]
        second = [item["driver"]["ref"] for item in select_drivers(record)["ranked"]]
        self.assertEqual(first, second)

    def test_a_filed_tax_benefit_is_not_carried_forward_as_a_rate(self):
        # IBM's held history has a profitable quarter with a tax benefit.  The
        # observation is real, but holding that negative effective rate flat
        # makes the economic-invariant gate refuse the entire sensitivity
        # table.  With five other filed candidates, the selector must use
        # those rather than clamp the benefit or lose the table.
        history = dict(SERIES)
        history[TAX] = (*TAXES[:-1], "-10000000")
        history[RND] = tuple(str(Decimal(value) * Decimal("0.05"))
                             for value in REVENUES)
        history[DA] = tuple(str(Decimal(value) * Decimal("0.07"))
                            for value in REVENUES)
        specification = spec()
        specification["expense_lines"] = [
            *specification["expense_lines"],
            {"ref": "research", "label": "Research", "basis_concept": RND,
             "behaviour": "semi_variable", "driver_ref": None,
             "because": "Research supports the service."},
            {"ref": "depreciation", "label": "Depreciation", "basis_concept": DA,
             "behaviour": "fixed", "driver_ref": None,
             "because": "Assets are consumed over time."},
        ]
        record = model(ledger(history), specification)
        picked = select_drivers(record)
        refs = [item["driver"]["ref"] for item in picked["ranked"]]

        self.assertEqual(picked["selection"]["status"], "available")
        self.assertEqual(len(refs), MAX_DRIVERS)
        self.assertNotIn(TAX_DRIVER, refs)
        projection = build_projection(record)
        self.assertEqual(len(projection["drivers"]), MAX_DRIVERS)
        self.assertNotIn(TAX_DRIVER,
                         [item["driver_ref"] for item in projection["drivers"]])

    def test_no_more_than_five_drivers_are_named(self):
        self.assertLessEqual(len(select_drivers(model())["ranked"]), MAX_DRIVERS)

    def test_the_rule_is_frozen_and_its_hash_is_the_hash_of_its_words(self):
        # The ref says what the rule ranks by. It is not "elasticity-rank":
        # a projection computed under a rule that ranked by elasticity must
        # never be mistakable for one computed under this rule.
        self.assertEqual(SELECTION_RULE_REF, "rule:swing-rank:1")
        self.assertEqual(SELECTION_RULE["ref"], SELECTION_RULE_REF)
        self.assertEqual(SELECTION_RULE_HASH, content_hash(SELECTION_RULE))
        self.assertEqual(SELECTION_RULE["unit_move"], "0.01")
        self.assertEqual(SELECTION_RULE["metric_precedence"][0],
                         "result:free_cash_flow")
        self.assertEqual(SELECTION_RULE["scenarios"], list(SCENARIOS))
        # The rule states that every column is a flat hold, not a path, and
        # that a driver without a band is demoted rather than dropped.
        self.assertIn("held", SELECTION_RULE["measure"])
        self.assertIn("none of them is a path", SELECTION_RULE["held_flat"])
        self.assertIn("demoted, not excluded", SELECTION_RULE["rank_fallback"])


class WhatIfTests(unittest.TestCase):
    def test_our_own_column_reproduces_the_stored_model(self):
        # The column that has to be right or nothing else means anything: if
        # recomputing our own estimate did not reproduce the model, every other
        # column would be measured against a baseline that is not the model.
        record = model()
        results, replaced = recompute(record, None, None)
        self.assertEqual(replaced, [])
        stored = {cell["period"]["end"]: cell["value"]
                  for line in record["results"] if line["ref"] == "result:net_income"
                  for cell in line["cells"] if cell["kind"] == "estimate"}
        recomputed = {cell["period"]["end"]: cell["value"]
                      for line in results if line["ref"] == "result:net_income"
                      for cell in line["cells"] if cell["kind"] == "estimate"}
        self.assertEqual(stored, recomputed)

    def test_a_what_if_is_bit_identical_when_recomputed(self):
        record = model()
        first = build_projection(record)
        second = build_projection(record)
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(body_hash(first), body_hash(second))

    def test_every_scenario_names_the_assumptions_it_replaced_and_the_cells_it_came_from(self):
        record = model()
        projection = build_projection(record)
        cost = next(item for item in projection["drivers"]
                    if item["driver_ref"] == COST_DRIVER)
        rows = {row["scenario"]: row for row in cost["what_if"]}
        self.assertEqual(sorted(rows), sorted(SCENARIOS))
        for scenario in ("trough", "mean", "peak"):
            row = rows[scenario]
            self.assertEqual(row["status"], "computed")
            # One replaced assumption per forecast quarter.
            self.assertEqual(len(row["replaced_assumption_refs"]),
                             len(record["forecast_periods"]))
            self.assertTrue(all(ref.startswith(f"assumption:{COST_DRIVER}@")
                                for ref in row["replaced_assumption_refs"]))
            self.assertTrue(row["input_refs"])
            self.assertTrue(all(ref["accession"] for ref in row["input_refs"]))
        # Ours replaces nothing: it *is* the model.
        self.assertEqual(rows["ours"]["replaced_assumption_refs"], [])

    def test_the_extreme_columns_carry_the_quarter_the_extreme_happened_in(self):
        projection = build_projection(model())
        cost = next(item for item in projection["drivers"]
                    if item["driver_ref"] == COST_DRIVER)
        rows = {row["scenario"]: row for row in cost["what_if"]}
        self.assertEqual(rows["peak"]["from_period"],
                         cost["band"]["peak"]["period_end"])
        self.assertEqual(rows["trough"]["from_period"],
                         cost["band"]["trough"]["period_end"])
        self.assertIsNone(rows["mean"]["from_period"])

    def test_a_higher_cost_share_produces_a_lower_bottom_line(self):
        projection = build_projection(model())
        cost = next(item for item in projection["drivers"]
                    if item["driver_ref"] == COST_DRIVER)
        totals = {}
        for row in cost["what_if"]:
            line = next(item for item in row["lines"]
                        if item["ref"] == "result:net_income")
            totals[row["scenario"]] = Decimal(line["total"])
        self.assertGreater(totals["trough"], totals["mean"])
        self.assertGreater(totals["mean"], totals["peak"])

    def test_revenue_is_unmoved_by_an_expense_scenario(self):
        projection = build_projection(model())
        cost = next(item for item in projection["drivers"]
                    if item["driver_ref"] == COST_DRIVER)
        revenues = {
            next(item for item in row["lines"] if item["ref"] == "result:revenue")["total"]
            for row in cost["what_if"]}
        self.assertEqual(len(revenues), 1)

    def test_an_unavailable_line_keeps_its_reason_rather_than_printing_a_zero(self):
        projection = build_projection(model())
        for driver in projection["drivers"]:
            for row in driver["what_if"]:
                line = next(item for item in row["lines"]
                            if item["ref"] == "result:free_cash_flow")
                self.assertEqual(line["status"], "unavailable")
                self.assertIsNone(line["total"])
                self.assertIn("not_material", line["reason"])

    def test_a_line_is_not_summed_over_the_quarters_that_did_compute(self):
        record = model()
        results, _ = recompute(record, None, None)
        ends = [str(item["end"]) for item in open_periods(record)]
        total = horizon_total(results, "result:free_cash_flow", ends)
        self.assertEqual(total["status"], "unavailable")
        self.assertIsNone(total["value"])

    def test_every_computed_column_says_it_holds_its_level_flat(self):
        # "The trough" is not the trough quarter happening once; it is the
        # whole horizon spent there. A reader who does not know that reads a
        # far milder table than the one in front of them.
        record = model()
        projection = build_projection(record)
        quarters = len(open_periods(record))
        for driver in projection["drivers"]:
            for row in driver["what_if"]:
                if row["status"] != "computed":
                    self.assertFalse(row["held_flat"])
                    self.assertEqual(row["quarters_held"], 0)
                    continue
                self.assertTrue(row["held_flat"])
                self.assertEqual(row["quarters_held"], quarters)
                for line in row["lines"]:
                    self.assertEqual(len(line["cells"]), quarters)

    def test_the_four_lines_are_the_four_the_rule_names(self):
        projection = build_projection(model())
        for driver in projection["drivers"]:
            for row in driver["what_if"]:
                self.assertEqual([line["ref"] for line in row["lines"]],
                                 list(WHAT_IF_LINES))


class HorizonTests(unittest.TestCase):
    def test_a_quarter_the_filings_have_answered_is_not_a_scenario(self):
        # Shaped the way ``actualize_model`` leaves a record: the settled
        # quarter moves *out* of forecast_periods and into realised_periods.
        # ``validate_forecast_model`` refuses a record holding a quarter in
        # both, so that is the only realised state a stored model can be in.
        record = model()
        settled = dict(record["forecast_periods"][0])
        record["realised_periods"] = [settled]
        record["forecast_periods"] = [dict(item)
                                      for item in record["forecast_periods"][1:]]
        # And the overlapping shape the filter used to guard against is one
        # the authority refuses outright, which is why filtering for it here
        # would only ever fire on a record that cannot be stored.
        authority = ForecastModelAuthority(store())
        overlapping = {key: value for key, value in record.items()
                       if key != "id" and key != "content_hash"}
        overlapping["forecast_periods"] = (
            [settled] + list(overlapping["forecast_periods"]))
        with self.assertRaises(ForecastModelValidationError) as refused:
            authority.publish(overlapping)
        self.assertIn("both realised and forecast", str(refused.exception))
        ends = [item["end"] for item in open_periods(record)]
        self.assertNotIn(settled["end"], ends)
        projection = build_projection(record)
        self.assertEqual(len(projection["horizon"]), len(ends))
        for driver in projection["drivers"]:
            for row in driver["what_if"]:
                for line in row["lines"]:
                    self.assertEqual([cell["period_end"] for cell in line["cells"]],
                                     ends)

    def test_a_model_with_nothing_still_ahead_is_refused_whole(self):
        # Every quarter settled: forecast_periods is empty and realised holds
        # them, which is a record the authority accepts and this slice cannot
        # be sensitive about.
        record = model()
        record["realised_periods"] = [dict(item)
                                      for item in record["forecast_periods"]]
        record["forecast_periods"] = []
        self.assertEqual(open_periods(record), [])
        with self.assertRaises(SensitivityUnavailable) as caught:
            build_projection(record)
        self.assertIn("already been filed", str(caught.exception))

    def test_a_revised_assumption_moves_our_column_and_nothing_else(self):
        record = model()
        end = record["forecast_periods"][0]["end"]
        revised = revise_assumptions(
            record,
            [{"driver": COST_DRIVER, "period": end, "value": "0.900000000000",
              "because": "A person decided delivery cost is about to jump."}],
            change_reason="human_revision",
            evidence_refs=[{"kind": "human_decision", "ref": "human:pm",
                            "concept": None, "period_end": None, "accession": None}],
            actor_ref="human:pm")
        revised["id"] = record["id"]
        revised["content_hash"] = record["content_hash"]
        projection = build_projection(revised)
        cost = next(item for item in projection["drivers"]
                    if item["driver_ref"] == COST_DRIVER)
        # The assumption is no longer one number across the horizon, and the
        # table says so rather than picking one of the two.
        self.assertIsNone(cost["ours"]["value"])
        self.assertTrue(cost["ours"]["varies"])
        ours = next(row for row in cost["what_if"] if row["scenario"] == "ours")
        self.assertEqual(ours["status"], "unavailable")
        self.assertIn("not one number", ours["reason"])
        # The band is unaffected: it is filed history, not our view of it.
        self.assertEqual(cost["band"]["peak"]["value"], "0.820000000000")

    def revised(self):
        """The fixture with cost of revenue moved for one quarter only."""

        record = model()
        end = record["forecast_periods"][0]["end"]
        out = revise_assumptions(
            record,
            [{"driver": COST_DRIVER, "period": end, "value": "0.900000000000",
              "because": "A person decided delivery cost is about to jump."}],
            change_reason="human_revision",
            evidence_refs=[{"kind": "human_decision", "ref": "human:pm",
                            "concept": None, "period_end": None, "accession": None}],
            actor_ref="human:pm")
        out["id"] = record["id"]
        out["content_hash"] = record["content_hash"]
        return out

    def test_a_driver_with_no_single_level_gets_no_elasticity(self):
        # An elasticity is "the answer moves this much when this assumption
        # moves one point". A driver revised for one quarter has no single
        # level to add a point to, and taking the first quarter's would flatten
        # the other quarters onto it and report the flattening as sensitivity.
        record = self.revised()
        impact = driver_impact(record, COST_DRIVER, impact_metric(record))
        self.assertEqual(impact["status"], "unavailable")
        self.assertIn("not one number across the horizon", impact["reason"])
        self.assertIsNone(impact["delta"])
        self.assertIsNone(impact["delta_per_unit"])

    def test_a_driver_with_no_elasticity_is_still_ranked_on_its_swing(self):
        # Demoted, not excluded -- and here not even demoted, because its band
        # is intact and the swing is what ranks. A driver dropped for want of an
        # elasticity would be a driver the reader is told nothing about, which
        # reads as a driver that does not matter.
        record = self.revised()
        picked = select_drivers(record)
        refs = [item["driver"]["ref"] for item in picked["ranked"]]
        self.assertIn(COST_DRIVER, refs)
        self.assertEqual(picked["selection"]["status"], "available")
        entry = next(item for item in picked["ranked"]
                     if item["driver"]["ref"] == COST_DRIVER)
        self.assertEqual(entry["impact"]["status"], "unavailable")
        self.assertEqual(entry["swing"]["status"], "computed")

    def test_a_driver_with_neither_number_is_the_only_one_dropped(self):
        # Too little history for a band, and no single level for an elasticity.
        short = {concept: values[:3] for concept, values in SERIES.items()}
        record = model(ledger(short))
        picked = select_drivers(record)
        for entry in picked["ranked"]:
            self.assertEqual(entry["band"]["status"], "unavailable")
            self.assertEqual(entry["swing"]["status"], "unavailable")
            # Kept because the elasticity places it, and ranked below any
            # driver that had a band -- here there are none.
            self.assertEqual(entry["impact"]["status"], "computed")


class ProjectionTests(unittest.TestCase):
    def test_a_projection_is_bound_to_the_model_version_and_its_inputs(self):
        record = model()
        projection = build_projection(record)
        self.assertEqual(projection["model_version_ref"], record["id"])
        self.assertEqual(projection["model_version_hash"], record["content_hash"])
        self.assertEqual(projection["inputs_hash"], record["inputs_hash"])
        self.assertEqual(projection["formula_hash"], record["formula_hash"])
        self.assertEqual(projection["value_kind"], "derived_deterministic")

    def test_the_fingerprint_moves_when_the_model_does_and_when_the_street_does(self):
        record = model()
        base = fs.fingerprint(record, None)
        self.assertEqual(base, fs.fingerprint(record, None))
        self.assertNotEqual(base, fs.fingerprint(record, "d" * 64))
        moved = dict(record, content_hash="e" * 64)
        self.assertNotEqual(base, fs.fingerprint(moved, None))

    def test_readiness_counts_rather_than_scores(self):
        readiness = projection_readiness(build_projection(model()))
        self.assertEqual(readiness["drivers_selected"], 4)
        self.assertEqual(readiness["drivers_with_bands"], 4)
        self.assertEqual(readiness["drivers_without_bands"], [])
        self.assertEqual(readiness["impact_metric"], "result:net_income")
        self.assertEqual(readiness["horizon_quarters"], 4)
        self.assertEqual(readiness["bridge_status"], "unavailable")
        self.assertNotIn("score", readiness)

    def test_the_default_bridge_is_unavailable_rather_than_absent(self):
        projection = build_projection(model())
        self.assertEqual(projection["consensus_bridge"]["status"], "unavailable")
        self.assertTrue(projection["consensus_bridge"]["reason"])
        self.assertEqual(projection["consensus_bridge"]["metrics"], [])

    def test_a_projection_that_cannot_name_its_model_is_refused(self):
        projection = build_projection(model())
        projection.update({"id": "sensitivity-projection:x:1", "version": 1,
                           "created_at": "now", "prior_projection_ref": None,
                           "body_hash": "f" * 64, "content_hash": "0" * 64})
        validate_projection(dict(projection))
        with self.assertRaises(Exception):
            validate_projection(dict(projection, model_version_ref="claim:whatever"))

    def test_an_unknown_field_is_refused_rather_than_carried(self):
        projection = build_projection(model())
        projection.update({"id": "sensitivity-projection:x:1", "version": 1,
                           "created_at": "now", "prior_projection_ref": None,
                           "body_hash": "f" * 64, "content_hash": "0" * 64,
                           "confidence": 0.9})
        with self.assertRaises(Exception):
            validate_projection(projection)


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.store = store()
        self.authority = SensitivityProjectionAuthority(self.store)
        self.record = model()

    def tearDown(self):
        self.store.close()

    def body(self, record=None):
        return build_projection(record or self.record, actor_ref="automation:test")

    def test_a_first_projection_is_version_one_and_reads_back_as_written(self):
        stored = self.authority.publish(self.body())
        self.assertEqual(stored["status"], "fresh")
        self.assertEqual(stored["version"], 1)
        self.assertIsNone(stored["prior_projection_ref"])
        again = self.authority.projection(stored["id"])
        self.assertEqual(again["content_hash"], stored["content_hash"])
        self.assertEqual(self.authority.latest(ACN)["id"], stored["id"])
        self.assertEqual(self.authority.companies(), [ACN])

    def test_an_unchanged_model_recomputes_to_a_duplicate(self):
        first = self.authority.publish(self.body())
        second = self.authority.publish(self.body())
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.authority.versions(ACN)), 1)

    def test_a_revised_model_produces_a_second_version_on_the_chain(self):
        first = self.authority.publish(self.body())
        end = self.record["forecast_periods"][0]["end"]
        revised = revise_assumptions(
            self.record,
            [{"driver": COST_DRIVER, "period": end, "value": "0.900000000000",
              "because": "A person decided delivery cost is about to jump."}],
            change_reason="human_revision",
            evidence_refs=[{"kind": "human_decision", "ref": "human:pm",
                            "concept": None, "period_end": None, "accession": None}],
            actor_ref="human:pm")
        revised["id"] = "forecast-model-version:fixture:2"
        revised["content_hash"] = "d" * 64
        second = self.authority.publish(self.body(revised))
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["prior_projection_ref"], first["id"])
        self.assertEqual([item["version"] for item in self.authority.versions(ACN)],
                         [1, 2])

    def test_the_stored_hash_is_the_hash_of_the_stored_bytes(self):
        # The validator normalises -- every gap row goes through P15d's _text,
        # which strips -- so hashing the draft and storing the wire would put a
        # number in the row that is not the hash of the JSON beside it, and the
        # read-back check would be comparing that number with itself.
        stored = self.authority.publish(self.body())
        row = self.store.connection.execute(
            "SELECT record_json, content_hash FROM sensitivity_projections "
            "WHERE projection_id=?", (stored["id"],)).fetchone()
        written = json.loads(row["record_json"])
        self.assertEqual(
            row["content_hash"],
            content_hash({key: value for key, value in written.items()
                          if key != "content_hash"}))
        self.assertEqual(written["content_hash"], row["content_hash"])

    def test_a_projection_cannot_be_updated_or_deleted(self):
        stored = self.authority.publish(self.body())
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute(
                "UPDATE sensitivity_projections SET company_ref='x' WHERE projection_id=?",
                (stored["id"],))
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute(
                "DELETE FROM sensitivity_projections WHERE projection_id=?",
                (stored["id"],))

    def test_an_insert_outside_the_authority_is_refused(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute(
                "INSERT INTO sensitivity_projections(projection_id,projection_ref,"
                "version_number,prior_projection_id,company_ref,model_version_ref,"
                "model_version_hash,inputs_hash,fingerprint,body_hash,record_json,"
                "content_hash,actor_ref,created_at) "
                "VALUES('x','y',1,NULL,'z','m','h','i','f','b','{}','c','a','t')")

    def test_two_companies_whose_refs_end_alike_keep_separate_chains(self):
        # Live holds ``company:sec-cik:001688568`` beside
        # ``company:sec-cik:0001467373``; a slug taken from the last segment
        # would be a way for one company to write into another's chain.
        other = dict(self.record, company_ref="company:other:0001467373")
        first = self.authority.publish(self.body())
        second = self.authority.publish(self.body(other))
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["version"], 1)


class RenderTests(unittest.TestCase):
    def test_the_view_prints_the_band_the_quarter_and_every_scenario(self):
        projection = build_projection(model())
        projection.update({"id": "sensitivity-projection:x:1", "version": 1,
                           "created_at": "now"})
        text = render_sensitivity(projection, entity_name="Accenture plc")
        self.assertIn("SENSITIVITY  Accenture plc", text)
        self.assertIn("CONSENSUS BRIDGE", text)
        self.assertIn("unavailable:", text)
        for scenario in SCENARIOS:
            self.assertIn(scenario, text)
        # The quarter each extreme happened in, beside the number.
        self.assertIn("(2024-11-30)", text)
        # An unavailable line prints its reason where its number would be.
        self.assertIn("not_material", text)
        self.assertIn("this is what ranks it", text)
        # The reader is told the columns are flat holds, not paths.
        self.assertIn("HOLDS ITS LEVEL FLAT", text)
        self.assertIn("next peak in:", text)

    def test_the_view_says_where_our_estimate_sits_in_the_band(self):
        projection = build_projection(model())
        projection.update({"id": "sensitivity-projection:x:1", "version": 1,
                           "created_at": "now"})
        text = render_sensitivity(projection)
        self.assertIn("of the way from trough to peak", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
