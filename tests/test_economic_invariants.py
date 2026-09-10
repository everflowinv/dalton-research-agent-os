"""P17b: the checks that would have caught Linde's zero percent.

Chem's workbook passed forty-two structural checks and still reported a
fifteen-year loss scenario as a 0.0% internal rate of return, because the
bisection that produced it was bounded to ``[0.0, 1.0]`` and returned its own
lower bound. Every test in the first half of this file is a number that is
arithmetically fine and economically impossible; every test in the second half
is a publication that does not happen because of one.

The fixtures are hand-built and tiny on purpose. An invariant tested through a
model is an invariant tested through whatever that model happens to do.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from dalton_core import economic_invariants as ei
from dalton_core.economic_invariants import (
    AVAILABLE,
    BAND,
    DIRECTION,
    DOMAIN,
    FAIL,
    INVARIANTS,
    NOT_APPLICABLE,
    PASS,
    PERIOD_BASIS,
    SEGMENT_SUM,
    SOLVER_BOUNDS,
    UNAVAILABLE,
    EconomicInvariantAuthority,
    EconomicInvariantRefused,
    EconomicInvariantValidationError,
    InvariantReport,
    InvariantResult,
    check,
    evaluate_forecast_model,
)
from dalton_core.model_forecast_driver import (
    ForecastModelAuthority,
    revise_assumptions,
)
from dalton_core.store import DaltonStore


def result_of(results, name):
    return next(item for item in results if item.invariant == name)


def quarter(end, start, value, **extra):
    return {"period_start": start, "period_end": end, "value": value,
            "status": "computed", **extra}


# ---------------------------------------------------------------------------
# the pure invariants
# ---------------------------------------------------------------------------


class DirectionTests(unittest.TestCase):
    """operating_income[k] = revenue[k] * (1 - cost_share - sum(opex_share))."""

    @staticmethod
    def series(pairs, ratios=(("cost", "0.80"),)):
        return [{"period_end": end, "revenue": revenue,
                 "operating_income": operating, "ratios": list(ratios)}
                for end, revenue, operating in pairs]

    def test_revenue_up_with_ratios_held_and_profit_up_is_the_formula(self):
        results = check({"direction_series": self.series([
            ("2026-08-31", "1000", "200"),
            ("2026-11-30", "1100", "220"),
        ])})
        self.assertEqual(result_of(results, DIRECTION).status, PASS)

    def test_revenue_up_with_ratios_held_and_profit_down_is_refused(self):
        results = check({"direction_series": self.series([
            ("2026-08-31", "1000", "200"),
            ("2026-11-30", "1100", "150"),
        ])})
        verdict = result_of(results, DIRECTION)
        self.assertEqual(verdict.status, FAIL)
        self.assertIn("operating income falls", verdict.findings[0])

    def test_a_margin_that_moves_while_every_ratio_is_held_is_refused(self):
        # Not a direction failure -- both numbers rise -- but the chain says
        # they must rise in the same proportion, and 200/1000 is not 230/1100.
        results = check({"direction_series": self.series([
            ("2026-08-31", "1000", "200"),
            ("2026-11-30", "1100", "230"),
        ])})
        verdict = result_of(results, DIRECTION)
        self.assertEqual(verdict.status, FAIL)
        self.assertIn("proportional", verdict.findings[0])

    def test_a_quarter_that_moved_a_cost_ratio_is_not_this_invariants_business(self):
        results = check({"direction_series": [
            {"period_end": "2026-08-31", "revenue": "1000",
             "operating_income": "200", "ratios": [("cost", "0.80")]},
            {"period_end": "2026-11-30", "revenue": "1100",
             "operating_income": "150", "ratios": [("cost", "0.90")]},
        ]})
        self.assertEqual(result_of(results, DIRECTION).status, NOT_APPLICABLE)

    def test_a_loss_making_company_selling_more_loses_more_and_that_is_allowed(self):
        # Margin of minus ten percent, revenue up, operating income down: the
        # formula behaving, not breaking. Refusing this would be the invariant
        # asserting an economic opinion rather than the frozen arithmetic.
        results = check({"direction_series": self.series([
            ("2026-08-31", "1000", "-100"),
            ("2026-11-30", "1100", "-110"),
        ])})
        self.assertEqual(result_of(results, DIRECTION).status, PASS)


class ScenarioDirectionTests(unittest.TestCase):
    @staticmethod
    def entry(role, points, **extra):
        return {"driver_ref": "concept:x", "label": "Cost of revenue",
                "role": role, "measure": "revenue_share",
                "line_ref": "result:operating_income",
                "points": points, **extra}

    def test_a_bigger_cost_share_must_shrink_operating_income(self):
        results = check({"scenarios": [self.entry("cost_of_revenue", [
            {"scenario": "trough", "value": "0.70", "line_total": "300"},
            {"scenario": "ours", "value": "0.80", "line_total": "200"},
            {"scenario": "peak", "value": "0.90", "line_total": "100"},
        ])]})
        self.assertEqual(result_of(results, DIRECTION).status, PASS)

    def test_a_column_that_moves_the_wrong_way_is_refused(self):
        results = check({"scenarios": [self.entry("cost_of_revenue", [
            {"scenario": "ours", "value": "0.80", "line_total": "200"},
            {"scenario": "peak", "value": "0.90", "line_total": "400"},
        ])]})
        verdict = result_of(results, DIRECTION)
        self.assertEqual(verdict.status, FAIL)
        self.assertIn("must make result:operating_income fall", verdict.findings[0])

    def test_the_revenue_drivers_direction_comes_from_the_implied_margin(self):
        # A loss-making chain: faster growth makes the loss bigger, so the
        # column that *rises* with growth is the one to refuse.
        results = check({"scenarios": [self.entry(
            "revenue", [
                {"scenario": "ours", "value": "0.02", "line_total": "-100"},
                {"scenario": "peak", "value": "0.06", "line_total": "-50"},
            ], measure="quarterly_growth", margin="-0.1")]})
        self.assertEqual(result_of(results, DIRECTION).status, FAIL)


class BandTests(unittest.TestCase):
    @staticmethod
    def assumption(value, **extra):
        return {"ref": "assumption:x", "driver_ref": "concept:x",
                "measure": "revenue_share", "value": value,
                "band": {"status": AVAILABLE, "min": "0.70", "max": "0.90",
                         "count": 8, "reason": None},
                "outside_band": False, "reason": None, **extra}

    def test_inside_the_filed_range_passes(self):
        results = check({"assumption_bands": [self.assumption("0.80")]})
        self.assertEqual(result_of(results, BAND).status, PASS)

    def test_outside_the_filed_range_and_silent_is_refused(self):
        results = check({"assumption_bands": [self.assumption("0.95")]})
        verdict = result_of(results, BAND)
        self.assertEqual(verdict.status, FAIL)
        self.assertIn("not marked outside_band", verdict.findings[0])

    def test_outside_the_filed_range_with_a_reason_is_a_view_and_passes(self):
        results = check({"assumption_bands": [self.assumption(
            "0.95", outside_band=True,
            reason="the new plant runs below scale until 2027")]})
        self.assertEqual(result_of(results, BAND).status, PASS)

    def test_the_flag_without_a_sentence_is_refused(self):
        results = check({"assumption_bands": [self.assumption(
            "0.95", outside_band=True, reason="   ")]})
        verdict = result_of(results, BAND)
        self.assertEqual(verdict.status, FAIL)
        self.assertIn("no reason given", verdict.findings[0])

    def test_a_flag_on_a_value_the_history_contains_is_refused_too(self):
        # The other direction, and it matters: a reader who sees
        # ``outside_band`` on half the assumptions stops reading it.
        results = check({"assumption_bands": [self.assumption(
            "0.80", outside_band=True, reason="a sentence that is not needed")]})
        self.assertEqual(result_of(results, BAND).status, FAIL)

    def test_too_little_history_is_not_a_pass(self):
        results = check({"assumption_bands": [self.assumption("9.99", band={
            "status": UNAVAILABLE, "min": None, "max": None, "count": 2,
            "reason": "only 2 filed observations"})]})
        self.assertEqual(result_of(results, BAND).status, NOT_APPLICABLE)


class DomainTests(unittest.TestCase):
    def test_a_margin_may_be_negative(self):
        results = check({"rate_quantities": [
            {"ref": "m", "rate_kind": "margin", "value": "-0.35"}]})
        self.assertEqual(result_of(results, DOMAIN).status, PASS)

    def test_a_margin_above_one_is_refused(self):
        results = check({"rate_quantities": [
            {"ref": "m", "rate_kind": "margin", "value": "1.02"}]})
        verdict = result_of(results, DOMAIN)
        self.assertEqual(verdict.status, FAIL)
        self.assertIn("kept more than it sold", verdict.findings[0])

    def test_growth_of_minus_one_or_worse_is_refused(self):
        results = check({"rate_quantities": [
            {"ref": "g", "rate_kind": "growth", "value": "-1"}]})
        self.assertEqual(result_of(results, DOMAIN).status, FAIL)

    def test_a_tax_rate_outside_zero_to_one_is_refused(self):
        for value in ("-0.05", "1.2"):
            with self.subTest(value=value):
                results = check({"rate_quantities": [
                    {"ref": "t", "rate_kind": "tax_rate", "value": value}]})
                self.assertEqual(result_of(results, DOMAIN).status, FAIL)

    def test_a_tax_rate_inside_it_passes(self):
        results = check({"rate_quantities": [
            {"ref": "t", "rate_kind": "tax_rate", "value": "0.25"}]})
        self.assertEqual(result_of(results, DOMAIN).status, PASS)

    def test_a_rate_kind_nobody_defined_is_refused_rather_than_skipped(self):
        with self.assertRaises(EconomicInvariantValidationError):
            check({"rate_quantities": [
                {"ref": "x", "rate_kind": "vibes", "value": "0.5"}]})


class SegmentSumTests(unittest.TestCase):
    ROWS = [
        {"concept": "us-gaap:Revenues", "period_start": "2026-06-01",
         "period_end": "2026-08-31", "value": "1000", "is_breakdown": False,
         "dimension_axis": None, "dimension_member": None},
        {"concept": "us-gaap:Revenues", "period_start": "2026-06-01",
         "period_end": "2026-08-31", "value": "600", "is_breakdown": True,
         "dimension_axis": "srt:StatementBusinessSegmentsAxis",
         "dimension_member": "Consulting"},
        {"concept": "us-gaap:Revenues", "period_start": "2026-06-01",
         "period_end": "2026-08-31", "value": "400", "is_breakdown": True,
         "dimension_axis": "srt:StatementBusinessSegmentsAxis",
         "dimension_member": "Outsourcing"},
    ]

    def test_the_parts_adding_to_the_whole_passes(self):
        results = check({"segments": ei.segment_groups(self.ROWS)})
        self.assertEqual(result_of(results, SEGMENT_SUM).status, PASS)

    def test_the_parts_missing_the_whole_is_refused(self):
        rows = [dict(row) for row in self.ROWS]
        rows[2]["value"] = "450"
        results = check({"segments": ei.segment_groups(rows)})
        verdict = result_of(results, SEGMENT_SUM)
        self.assertEqual(verdict.status, FAIL)
        self.assertIn("add to 1050", verdict.findings[0])

    def test_rounding_inside_the_tolerance_is_not_a_failure(self):
        rows = [dict(row) for row in self.ROWS]
        rows[2]["value"] = "400.005"
        results = check({"segments": ei.segment_groups(rows)})
        self.assertEqual(result_of(results, SEGMENT_SUM).status, PASS)

    def test_two_axes_are_not_added_together(self):
        rows = list(self.ROWS) + [
            {"concept": "us-gaap:Revenues", "period_start": "2026-06-01",
             "period_end": "2026-08-31", "value": "1000", "is_breakdown": True,
             "dimension_axis": "srt:StatementGeographicalAxis",
             "dimension_member": "US"},
            {"concept": "us-gaap:Revenues", "period_start": "2026-06-01",
             "period_end": "2026-08-31", "value": "0", "is_breakdown": True,
             "dimension_axis": "srt:StatementGeographicalAxis",
             "dimension_member": "RestOfWorld"},
        ]
        groups = ei.segment_groups(rows)
        self.assertEqual(len(groups), 2)
        self.assertEqual(result_of(check({"segments": groups}), SEGMENT_SUM).status,
                         PASS)

    def test_a_breakdown_with_no_consolidated_line_is_nothing_to_check(self):
        results = check({"segments": ei.segment_groups(self.ROWS[1:])})
        self.assertEqual(result_of(results, SEGMENT_SUM).status, NOT_APPLICABLE)


class PeriodBasisTests(unittest.TestCase):
    def test_four_quarters_in_one_line_pass(self):
        results = check({"lines": [{"ref": "revenue", "cells": [
            quarter("2025-11-30", "2025-09-01", "100"),
            quarter("2026-02-28", "2025-12-01", "110"),
            quarter("2026-05-31", "2026-03-01", "120"),
            quarter("2026-08-31", "2026-06-01", "130"),
        ]}]})
        self.assertEqual(result_of(results, PERIOD_BASIS).status, PASS)

    def test_a_year_to_date_figure_among_the_quarters_is_refused(self):
        # The 10-Q shape: the quarter and the half year end on the same day and
        # differ only in period_start. Summed together, the half is booked
        # twice.
        results = check({"lines": [{"ref": "revenue", "cells": [
            quarter("2026-02-28", "2025-12-01", "110"),
            quarter("2026-05-31", "2025-12-01", "230"),
        ]}]})
        verdict = result_of(results, PERIOD_BASIS)
        self.assertEqual(verdict.status, FAIL)
        self.assertIn("mixes period bases", verdict.findings[0])

    def test_a_trailing_year_of_annual_figures_is_refused_by_the_declared_kind(self):
        results = check({"lines": [{
            "ref": "window:revenue", "expected_period_kind": "quarter",
            "cells": [quarter("2026-08-31", "2025-09-01", "1000")]}]})
        verdict = result_of(results, PERIOD_BASIS)
        self.assertEqual(verdict.status, FAIL)
        self.assertIn("summed as quarter figures", verdict.findings[0])

    def test_an_estimate_and_the_actual_that_replaced_it_may_share_a_quarter(self):
        results = check({"lines": [{"ref": "result:revenue", "cells": [
            quarter("2026-08-31", "2026-06-01", "100", kind="estimate",
                    superseded_by="cell:actual"),
            quarter("2026-08-31", "2026-06-01", "104", kind="actual"),
        ]}]})
        self.assertEqual(result_of(results, PERIOD_BASIS).status, PASS)

    def test_a_live_estimate_beside_a_live_actual_is_two_answers_and_is_refused(self):
        results = check({"lines": [{"ref": "result:revenue", "cells": [
            quarter("2026-08-31", "2026-06-01", "100", kind="estimate"),
            quarter("2026-08-31", "2026-06-01", "104", kind="actual"),
        ]}]})
        verdict = result_of(results, PERIOD_BASIS)
        self.assertEqual(verdict.status, FAIL)
        self.assertIn("superseded_by", verdict.findings[0])


class SolverBoundsTests(unittest.TestCase):
    """The Linde case, and the honest ways out of it."""

    def test_the_clamped_irr_is_refused(self):
        results = check({"solver_results": [{
            "ref": "irr:conservative-15y", "label": "conservative 15-year IRR",
            "status": "solved", "value": "0.0",
            "lower_bound": "0.0", "upper_bound": "1.0"}]})
        verdict = result_of(results, SOLVER_BOUNDS)
        self.assertEqual(verdict.status, FAIL)
        self.assertIn("is the bound, not a root", verdict.findings[0])

    def test_a_root_strictly_inside_the_bracket_passes(self):
        results = check({"solver_results": [{
            "ref": "irr:base", "status": "solved", "value": "0.1342",
            "lower_bound": "0.0", "upper_bound": "1.0"}]})
        self.assertEqual(result_of(results, SOLVER_BOUNDS).status, PASS)

    def test_the_honest_answer_to_the_linde_case_is_unbounded_with_no_number(self):
        results = check({"solver_results": [{
            "ref": "irr:conservative-15y", "status": "unbounded", "value": None,
            "lower_bound": "0.0", "upper_bound": "1.0"}]})
        self.assertEqual(result_of(results, SOLVER_BOUNDS).status, PASS)

    def test_unbounded_that_still_carries_a_number_is_refused(self):
        results = check({"solver_results": [{
            "ref": "irr:conservative-15y", "status": "unbounded", "value": "0.0",
            "lower_bound": "0.0", "upper_bound": "1.0"}]})
        self.assertEqual(result_of(results, SOLVER_BOUNDS).status, FAIL)

    def test_a_solved_quantity_that_names_no_bracket_is_refused(self):
        results = check({"solver_results": [{
            "ref": "irr:base", "status": "solved", "value": "0.13",
            "lower_bound": None, "upper_bound": None}]})
        self.assertEqual(result_of(results, SOLVER_BOUNDS).status, FAIL)

    def test_a_value_outside_the_bracket_is_refused(self):
        results = check({"solver_results": [{
            "ref": "irr:base", "status": "solved", "value": "-0.4",
            "lower_bound": "0.0", "upper_bound": "1.0"}]})
        self.assertEqual(result_of(results, SOLVER_BOUNDS).status, FAIL)


class ShapeTests(unittest.TestCase):
    def test_every_invariant_answers_in_the_frozen_order(self):
        results = check({})
        self.assertEqual([item.invariant for item in results], list(INVARIANTS))
        self.assertIsInstance(results, tuple)

    def test_an_empty_subject_is_not_a_pass(self):
        self.assertTrue(all(item.status == NOT_APPLICABLE for item in check({})))

    def test_a_subject_key_nobody_reads_is_refused_rather_than_ignored(self):
        with self.assertRaises(EconomicInvariantValidationError):
            check({"assumptions": []})

    def test_a_failing_result_must_say_what_failed(self):
        with self.assertRaises(EconomicInvariantValidationError):
            InvariantResult(DOMAIN, FAIL)

    def test_a_float_never_reaches_an_invariant(self):
        with self.assertRaises(EconomicInvariantValidationError):
            check({"rate_quantities": [
                {"ref": "m", "rate_kind": "margin", "value": 1.02}]})

    def test_a_report_reads_as_unavailable_with_its_reasons(self):
        report = ei.evaluate(
            output_kind=ei.FORECAST_MODEL, output_ref="forecast-model-version:x",
            company_ref="company:sec-cik:0001467373",
            subject={"solver_results": [{
                "ref": "irr", "status": "solved", "value": "0.0",
                "lower_bound": "0.0", "upper_bound": "1.0"}]})
        self.assertEqual(report.status, UNAVAILABLE)
        self.assertTrue(report.reasons[0].startswith("solver_bounds:"))
        self.assertEqual(report.as_dict()["status"], UNAVAILABLE)

    def test_the_same_subject_gives_the_same_hash(self):
        subject = {"rate_quantities": [
            {"ref": "t", "rate_kind": "tax_rate", "value": "0.25"}]}
        first = ei.evaluate(output_kind=ei.FORECAST_MODEL, output_ref="x",
                            company_ref="c", subject=subject)
        second = ei.evaluate(output_kind=ei.FORECAST_MODEL, output_ref="x",
                             company_ref="c", subject=subject)
        self.assertEqual(first.subject_hash, second.subject_hash)


# ---------------------------------------------------------------------------
# the gate: three publications that do not happen
# ---------------------------------------------------------------------------

from tests.test_model_forecast_driver import (  # noqa: E402
    ACN, COST_CONCEPT, REVENUE_CONCEPT, model as forecast_body,
)


CLAIM_REF = {"kind": "claim", "ref": "claim-version:x", "concept": None,
             "period_end": None, "accession": None}


class ForecastGateTests(unittest.TestCase):
    """A forecast version that fails an invariant is never stored."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.authority = ForecastModelAuthority(self.store)
        self.verdicts = EconomicInvariantAuthority(self.store)

    def versions(self):
        return self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM forecast_model_versions").fetchone()["n"]

    def test_a_model_built_from_the_filings_passes_and_records_nothing(self):
        stored = self.authority.publish(forecast_body())
        self.assertEqual(stored["status"], "fresh")
        self.assertIsNone(self.verdicts.latest(ACN, ei.FORECAST_MODEL))

    def test_an_out_of_band_assumption_with_no_reason_refuses_the_whole_version(self):
        record = self.authority.publish(forecast_body())
        before = self.versions()
        with self.assertRaises(EconomicInvariantRefused) as caught:
            self.authority.publish(revise_assumptions(
                record,
                [{"driver": f"concept:{COST_CONCEPT}", "period": "2026-08-31",
                  "value": "0.20", "because": "A view was taken.",
                  "refs": [CLAIM_REF]}],
                change_reason="assumption_review", evidence_refs=[CLAIM_REF],
                actor_ref="human:analyst"))
        report = caught.exception.report
        self.assertEqual(report.status, UNAVAILABLE)
        self.assertTrue(any(reason.startswith("assumption_band:")
                            for reason in report.reasons))
        # Refuse-whole: not one repaired cell, not a partial version.
        self.assertEqual(self.versions(), before)

    def test_the_refusal_is_on_the_record_with_its_reasons(self):
        record = self.authority.publish(forecast_body())
        with self.assertRaises(EconomicInvariantRefused):
            self.authority.publish(revise_assumptions(
                record,
                [{"driver": f"concept:{COST_CONCEPT}", "period": "2026-08-31",
                  "value": "0.20", "because": "A view was taken.",
                  "refs": [CLAIM_REF]}],
                change_reason="assumption_review", evidence_refs=[CLAIM_REF],
                actor_ref="human:analyst"))
        verdict = self.verdicts.latest(ACN, ei.FORECAST_MODEL)
        self.assertEqual(verdict["status"], UNAVAILABLE)
        self.assertEqual(verdict["version"], 1)
        self.assertTrue(verdict["reasons"])
        failed = [item for item in verdict["results"] if item["status"] == FAIL]
        self.assertEqual([item["invariant"] for item in failed], [BAND])

    def test_the_same_refusal_twice_is_one_row(self):
        record = self.authority.publish(forecast_body())
        change = [{"driver": f"concept:{COST_CONCEPT}", "period": "2026-08-31",
                   "value": "0.20", "because": "A view was taken.",
                   "refs": [CLAIM_REF]}]
        for _ in range(2):
            with self.assertRaises(EconomicInvariantRefused):
                self.authority.publish(revise_assumptions(
                    record, change, change_reason="assumption_review",
                    evidence_refs=[CLAIM_REF], actor_ref="human:analyst"))
        self.assertEqual(len(self.verdicts.verdicts(ACN, ei.FORECAST_MODEL)), 1)

    def test_the_same_value_with_a_reason_publishes_and_clears_the_refusal(self):
        record = self.authority.publish(forecast_body())
        with self.assertRaises(EconomicInvariantRefused):
            self.authority.publish(revise_assumptions(
                record,
                [{"driver": f"concept:{COST_CONCEPT}", "period": "2026-08-31",
                  "value": "0.20", "because": "A view was taken.",
                  "refs": [CLAIM_REF]}],
                change_reason="assumption_review", evidence_refs=[CLAIM_REF],
                actor_ref="human:analyst"))
        stored = self.authority.publish(revise_assumptions(
            record,
            [{"driver": f"concept:{COST_CONCEPT}", "period": "2026-08-31",
              "value": "0.20", "because": "A view was taken.",
              "outside_band": {
                  "reason": "the renegotiated delivery contract lands in this "
                            "quarter and no filed quarter contains it"},
              "refs": [CLAIM_REF]}],
            change_reason="assumption_review", evidence_refs=[CLAIM_REF],
            actor_ref="human:analyst"))
        self.assertEqual(stored["status"], "fresh")
        chain = self.verdicts.verdicts(ACN, ei.FORECAST_MODEL)
        self.assertEqual([item["status"] for item in chain],
                         [UNAVAILABLE, AVAILABLE])
        self.assertEqual(self.verdicts.open_refusals(), [])

    def test_a_flag_with_no_sentence_behind_it_is_refused_at_the_shape(self):
        record = self.authority.publish(forecast_body())
        with self.assertRaises(Exception) as caught:
            self.authority.publish(revise_assumptions(
                record,
                [{"driver": f"concept:{COST_CONCEPT}", "period": "2026-08-31",
                  "value": "0.20", "because": "A view was taken.",
                  "outside_band": {"reason": "because"}, "refs": [CLAIM_REF]}],
                change_reason="assumption_review", evidence_refs=[CLAIM_REF],
                actor_ref="human:analyst"))
        self.assertIn("must be a sentence", str(caught.exception))

    def test_a_model_from_before_the_flag_existed_still_reads_back(self):
        stored = self.authority.publish(forecast_body())
        stored.pop("status")
        assumption = dict(stored["assumptions"][0])
        self.assertIsNone(assumption["outside_band"])
        del assumption["outside_band"]
        from dalton_core.model_forecast_driver import validate_forecast_model

        wire = validate_forecast_model({
            **stored, "assumptions": [assumption] + list(stored["assumptions"][1:])})
        self.assertIsNone(wire["assumptions"][0]["outside_band"])

    def test_a_verdict_row_cannot_be_updated_or_deleted(self):
        record = self.authority.publish(forecast_body())
        with self.assertRaises(EconomicInvariantRefused):
            self.authority.publish(revise_assumptions(
                record,
                [{"driver": f"concept:{COST_CONCEPT}", "period": "2026-08-31",
                  "value": "0.20", "because": "A view was taken.",
                  "refs": [CLAIM_REF]}],
                change_reason="assumption_review", evidence_refs=[CLAIM_REF],
                actor_ref="human:analyst"))
        for sql in ("UPDATE economic_invariant_verdicts SET status='available'",
                    "DELETE FROM economic_invariant_verdicts"):
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.store.connection.execute(sql)

    def test_an_unauthorised_insert_is_refused(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "INSERT INTO economic_invariant_verdicts"
                "(verdict_id,verdict_ref,version_number,prior_verdict_id,company_ref,"
                "output_kind,output_ref,status,rule_ref,subject_hash,failure_count,"
                "record_json,content_hash,actor_ref,created_at)"
                " VALUES('a','b',1,NULL,'c','forecast_model_version','d',"
                "'unavailable','r','h',1,'{}','x','y','z')")

    def test_a_clamped_solver_result_refuses_the_version_it_was_published_with(self):
        # The Linde case reaching the gate through the publisher rather than
        # through ``check``: the caller hands over the derived quantity beside
        # the model, and a number that is its own search bound stops the
        # version the way an impossible margin does.
        with self.assertRaises(EconomicInvariantRefused) as caught:
            self.authority.publish(forecast_body(), solver_results=[{
                "ref": "irr:conservative-15y",
                "label": "conservative 15-year IRR",
                "status": "solved", "value": "0.0",
                "lower_bound": "0.0", "upper_bound": "1.0"}])
        self.assertTrue(any(reason.startswith("solver_bounds:")
                            for reason in caught.exception.report.reasons))
        self.assertEqual(self.versions(), 0)

    def test_a_segment_table_that_does_not_add_up_stops_the_version(self):
        rows = [
            {"concept": REVENUE_CONCEPT, "period_start": "2026-03-01",
             "period_end": "2026-05-31", "value": "1331000000",
             "is_breakdown": False, "dimension_axis": None,
             "dimension_member": None},
            {"concept": REVENUE_CONCEPT, "period_start": "2026-03-01",
             "period_end": "2026-05-31", "value": "800000000",
             "is_breakdown": True, "dimension_axis": "srt:SegmentAxis",
             "dimension_member": "Consulting"},
            {"concept": REVENUE_CONCEPT, "period_start": "2026-03-01",
             "period_end": "2026-05-31", "value": "400000000",
             "is_breakdown": True, "dimension_axis": "srt:SegmentAxis",
             "dimension_member": "Outsourcing"},
        ]
        with self.assertRaises(EconomicInvariantRefused) as caught:
            self.authority.publish(forecast_body(), statement_rows=rows)
        self.assertTrue(any(reason.startswith("segment_sum:")
                            for reason in caught.exception.report.reasons))


class ForecastSubjectTests(unittest.TestCase):
    """What the builder reads off a stored model, before any store is opened."""

    def setUp(self):
        self.record = forecast_body()

    def test_the_band_comes_from_the_filed_series_not_from_a_round_number(self):
        band = ei.measure_band(self.record, f"concept:{COST_CONCEPT}", "revenue_share")
        self.assertEqual(band["status"], AVAILABLE)
        self.assertEqual(Decimal(band["min"]), Decimal("0.8"))
        self.assertEqual(Decimal(band["max"]), Decimal("0.8"))

    def test_the_cost_ratios_are_what_the_direction_check_holds(self):
        subject = ei.forecast_subject(self.record)
        held = {row["period_end"]: row["ratios"] for row in subject["direction_series"]}
        self.assertTrue(all(held[end] for end in held))
        self.assertEqual(len(set(map(str, held.values()))), 1)

    def test_every_forecast_quarter_carries_a_margin_to_check(self):
        subject = ei.forecast_subject(self.record)
        margins = [item for item in subject["rate_quantities"]
                   if item["rate_kind"] == "margin"]
        self.assertTrue(margins)
        self.assertTrue(all(Decimal(item["value"]) <= 1 for item in margins))

    def test_the_tax_assumption_is_read_as_a_rate_and_not_as_a_margin(self):
        subject = ei.forecast_subject(self.record)
        kinds = {item["rate_kind"] for item in subject["rate_quantities"]}
        self.assertIn("tax_rate", kinds)
        self.assertIn("growth", kinds)


from dalton_core.forecast_sensitivity import (  # noqa: E402
    SensitivityProjectionAuthority, build_projection,
)
from tests.test_forecast_sensitivity import (  # noqa: E402
    COST_DRIVER, model as sensitivity_model, store as memory_store,
)


class SensitivityGateTests(unittest.TestCase):
    """A what-if column that moves the wrong way is not a table worth reading."""

    def setUp(self):
        self.store = memory_store()
        self.addCleanup(self.store.close)
        self.authority = SensitivityProjectionAuthority(self.store)
        self.verdicts = EconomicInvariantAuthority(self.store)
        self.body = build_projection(sensitivity_model())

    def projections(self):
        return self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM sensitivity_projections").fetchone()["n"]

    def test_the_projection_the_engine_computes_passes(self):
        stored = self.authority.publish(self.body)
        self.assertEqual(stored["status"], "fresh")
        self.assertIsNone(self.verdicts.latest(ACN, ei.SENSITIVITY_PROJECTION))

    def test_the_scenarios_are_monotone_in_the_direction_the_formula_fixes(self):
        subject = ei.projection_subject(self.body)
        cost = next(item for item in subject["scenarios"]
                    if item["driver_ref"] == COST_DRIVER)
        self.assertEqual(cost["line_ref"], "result:operating_income")
        self.assertEqual(result_of(check(subject), DIRECTION).status, PASS)

    def test_a_column_that_rises_with_the_cost_share_refuses_the_whole_table(self):
        body = ei.json.loads(ei.canonical_json(self.body))
        driver = next(item for item in body["drivers"]
                      if item["driver_ref"] == COST_DRIVER)
        rows = {item["scenario"]: item for item in driver["what_if"]
                if item["status"] == "computed"}

        def operating(scenario):
            return next(item for item in rows[scenario]["lines"]
                        if item["ref"] == "result:operating_income")

        # The lowest cost share this company ever filed and the highest, with
        # their operating incomes exchanged: every number is one the chain
        # itself produced, and together they say a bigger cost share earns
        # more. Nothing structural can see it.
        low, high = operating("trough"), operating("peak")
        low["total"], high["total"] = high["total"], low["total"]
        before = self.projections()
        with self.assertRaises(EconomicInvariantRefused) as caught:
            self.authority.publish(body)
        self.assertTrue(any(reason.startswith("direction_consistency:")
                            for reason in caught.exception.report.reasons))
        self.assertEqual(self.projections(), before)
        self.assertEqual(
            self.verdicts.latest(ACN, ei.SENSITIVITY_PROJECTION)["status"],
            UNAVAILABLE)

    def test_a_scenario_whose_margin_exceeds_one_refuses_the_table(self):
        body = ei.json.loads(ei.canonical_json(self.body))
        driver = next(item for item in body["drivers"]
                      if item["driver_ref"] == COST_DRIVER)
        for row in driver["what_if"]:
            if row["status"] != "computed":
                continue
            for line in row["lines"]:
                if line["ref"] == "result:operating_income":
                    line["total"] = str(Decimal(line["total"]) * 100)
        with self.assertRaises(EconomicInvariantRefused) as caught:
            self.authority.publish(body)
        self.assertTrue(any(reason.startswith("rate_domain:")
                            for reason in caught.exception.report.reasons))


from tests.test_valuation_snapshot import (  # noqa: E402
    ACCESSION, PRICED_ON, QUARTER_ENDS, QUARTER_STARTS,
    history as price_history, roles,
)
from dalton_core.valuation_snapshot import ValuationSnapshotAuthority  # noqa: E402

VALUATION_ACN = "company:sec-cik:0001467373"
PRICE_BINDING = {
    "version_ref": "market-price-series-version:" + "a" * 32,
    "version_hash": "b" * 64,
    "invocation_ref": "connector-invocation:yfinance:" + "c" * 32,
    "artifact_hash": "d" * 64,
}


class ValuationGateTests(unittest.TestCase):
    """A trailing year that is not four quarters is not a trailing year."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.authority = ValuationSnapshotAuthority(self.store)
        self.verdicts = EconomicInvariantAuthority(self.store)

    def publish(self, **overrides):
        return self.authority.publish_snapshot(
            company_ref=VALUATION_ACN,
            price={**PRICE_BINDING, "bar_date": PRICED_ON, "close": "100",
                   "currency": "USD"},
            shares={**PRICE_BINDING, "as_of": PRICED_ON,
                    "shares_outstanding": "1000000"},
            fundamental_windows=[{"as_of": "2026-09-01",
                                  "roles": roles(**overrides)}],
            price_history=price_history(),
        )

    def snapshots(self):
        return self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM valuation_snapshot_versions").fetchone()["n"]

    def test_four_quarters_publish(self):
        self.assertEqual(self.publish()["status"], "fresh")
        self.assertIsNone(self.verdicts.latest(VALUATION_ACN, ei.VALUATION_SNAPSHOT))

    def test_a_year_to_date_figure_inside_the_trailing_year_refuses_the_snapshot(self):
        # ``_role_input`` counts four components and refuses a repeat; it does
        # not look at how long each period is. Swapping the last quarter for
        # the nine months that contains it passes every check the module makes
        # and produces a price/sales wrong by the overlap.
        revenue = {
            "concept": "Revenues", "statement": "income", "unit": "USD",
            "source_ref": "source:sec-edgar",
            "components": [
                {"period_start": start, "period_end": end,
                 "value": "12500000", "accession": ACCESSION}
                for start, end in zip(QUARTER_STARTS[:3], QUARTER_ENDS[:3])
            ] + [{"period_start": QUARTER_STARTS[1], "period_end": QUARTER_ENDS[3],
                  "value": "37500000", "accession": ACCESSION}],
        }
        with self.assertRaises(EconomicInvariantRefused) as caught:
            self.publish(revenue=revenue)
        self.assertTrue(any(reason.startswith("period_basis:")
                            for reason in caught.exception.report.reasons))
        self.assertEqual(self.snapshots(), 0)
        self.assertEqual(
            self.verdicts.latest(VALUATION_ACN, ei.VALUATION_SNAPSHOT)["status"],
            UNAVAILABLE)

    def test_a_clamped_derived_figure_beside_a_snapshot_refuses_it(self):
        with self.assertRaises(EconomicInvariantRefused):
            self.authority.publish_snapshot(
                company_ref=VALUATION_ACN,
                price={**PRICE_BINDING, "bar_date": PRICED_ON, "close": "100",
                       "currency": "USD"},
                shares={**PRICE_BINDING, "as_of": PRICED_ON,
                        "shares_outstanding": "1000000"},
                fundamental_windows=[{"as_of": "2026-09-01", "roles": roles()}],
                price_history=price_history(),
                solver_results=[{"ref": "irr:dcf", "status": "solved",
                                 "value": "1.0", "lower_bound": "0.0",
                                 "upper_bound": "1.0"}])
        self.assertEqual(self.snapshots(), 0)


class CockpitVisibilityTests(unittest.TestCase):
    """A number that is not there has to say why on the page it is missing from.

    The failure mode this closes is the quiet one: the gate refuses, the model
    card keeps showing the last version that did publish, and nobody learns
    that a newer one was stopped. Chem's forty-two OK checks were readable; the
    thing that was not readable was the refusal that never happened.
    """

    def setUp(self):
        from tests.test_cockpit_plane import CockpitHarness

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.harness = CockpitHarness(Path(self._dir.name))
        self.addCleanup(self.harness.close)
        self.store = self.harness.h.h.core
        self.plane = self.harness.plane
        self.authority = ForecastModelAuthority(self.store)

    def refuse(self):
        record = self.authority.publish(forecast_body())
        with self.assertRaises(EconomicInvariantRefused):
            self.authority.publish(revise_assumptions(
                record,
                [{"driver": f"concept:{COST_CONCEPT}", "period": "2026-08-31",
                  "value": "0.20", "because": "A view was taken.",
                  "refs": [CLAIM_REF]}],
                change_reason="assumption_review", evidence_refs=[CLAIM_REF],
                actor_ref="human:analyst"))

    def test_the_company_card_carries_the_refusal_and_its_reasons(self):
        self.refuse()
        view = self.plane.overview()
        card = next(item for item in view["companies"]
                    if item["company_ref"] == ACN)
        refusal = card["invariants"][ei.FORECAST_MODEL]
        self.assertEqual(refusal["status"], UNAVAILABLE)
        self.assertEqual(refusal["output_kind_label"], "预测模型版本")
        self.assertTrue(refusal["reasons"])
        self.assertEqual([item["invariant"] for item in refusal["failed"]], [BAND])
        self.assertIn("outside_band", refusal["failed"][0]["label"])

    def test_a_company_with_nothing_refused_reads_as_an_empty_dict(self):
        self.authority.publish(forecast_body())
        card = next(item for item in self.plane.overview()["companies"]
                    if item["company_ref"] == ACN)
        self.assertEqual(card["invariants"], {})

    def test_the_forecast_page_says_a_newer_version_was_stopped(self):
        self.refuse()
        page = self.plane.company_model(ACN)
        self.assertEqual(page["version"], 1)
        self.assertEqual(page["invariants"][ei.FORECAST_MODEL]["status"],
                         UNAVAILABLE)
        self.assertIn("没通过经济不变量检查",
                      page["invariants"][ei.FORECAST_MODEL]["note"])

    def test_a_core_from_before_this_lane_degrades_to_no_refusals(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.assertEqual(self.plane._invariants(connection), {})
