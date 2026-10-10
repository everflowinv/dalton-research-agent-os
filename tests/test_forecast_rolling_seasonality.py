"""Synthetic company acceptance tests for the rolling/seasonal forecast rules."""
import copy
import unittest
from decimal import Decimal

from dalton_core.company_model_forecast import pending_action, model_digest
from dalton_core.forecast_coverage_assumptions import coverage_structure_assumptions
from dalton_core.forecast_seasonality import seasonal_profile
from dalton_core.model_forecast import quarter_after
from dalton_core.model_forecast_driver import (
    ForecastModelValidationError, build_forecast_model, forecast_periods,
    compute_structure_results, validate_new_forecast_rules,
)
from tests.test_model_forecast_driver import model


def synthetic_history(growing=False):
    period = {"end": "2023-12-31", "calendar": "company:fiscal"}
    out = []
    for i in range(12):
        period = quarter_after(period)
        value = Decimal((100, 120, 100, 280)[i % 4]) * (Decimal("1.1") ** (i // 4) if growing else 1)
        out.append({"concept": "synthetic:SeasonalRevenue", "period_start": period["start"],
                    "period_end": period["end"], "value": str(value), "unit": "usd",
                    "accessions": [f"synthetic-filing-{i}"], "source": "filed"})
    return out


def synthetic_driver():
    return {"ref": "concept:synthetic:SeasonalRevenue", "role": "revenue", "status": "filed",
            "kind": "statement_line", "concept": "synthetic:SeasonalRevenue", "unit": "usd",
            "statement": "income", "structure_line_ref": "sales", "history": synthetic_history()}


def synthetic_structure(method):
    return {"schema_version": "0.6", "lines": [{"ref": "sales", "kind": "filed", "role": "revenue",
            "label": "Synthetic seasonal sales", "unit": "usd", "concept": "synthetic:SeasonalRevenue",
            "forecast_method": method, "forecast_base_ref": None}], "formulas": []}


class RollingSeasonalityTests(unittest.TestCase):
    def test_short_forecast_is_rejected_but_old_spec_generates_eight(self):
        body = model()
        self.assertEqual(len(body["forecast_periods"]), 8)
        with self.assertRaises(ForecastModelValidationError):
            forecast_periods(body["drivers"][0], 4)
        old = copy.deepcopy(body)
        old["forecast_periods"] = old["forecast_periods"][:4]
        with self.assertRaises(ForecastModelValidationError):
            validate_new_forecast_rules(old)

    def test_preliminary_actual_moves_start_without_becoming_forecast(self):
        body = model()
        driver = next(d for d in body["drivers"] if d["role"] == "revenue")
        policy = {"latest_actual_end": "2026-08-31", "fiscal_year_end": "2025-08-31"}
        periods = forecast_periods(driver, 8, policy=policy)
        self.assertEqual(periods[0]["start"], "2026-09-01")
        self.assertEqual(periods[0]["end"], "2026-11-30")
        self.assertGreaterEqual(periods[-1]["end"], "2027-08-31")

    def test_reported_quarter_cannot_be_selected_by_forged_policy(self):
        body = model()
        body["forecast_policy"]["latest_actual_end"] = "2025-05-31"
        with self.assertRaisesRegex(ForecastModelValidationError, "reported history"):
            validate_new_forecast_rules(body)

    def test_future_reporting_boundary_is_rejected(self):
        body = model()
        body["forecast_policy"]["latest_actual_end"] = "2030-05-31"
        with self.assertRaisesRegex(ForecastModelValidationError, "latest known"):
            validate_new_forecast_rules(body)

    def test_year_end_coverage_can_extend_a_requested_horizon(self):
        driver = synthetic_driver()
        periods = forecast_periods(driver, 8, policy={"latest_actual_end": "2026-01-01",
                                                   "fiscal_year_end": "2025-12-31"})
        self.assertEqual(len(periods), 8)
        self.assertGreaterEqual(periods[-1]["end"], "2027-12-31")

    def test_stable_seasonality_is_detected_after_removing_growth(self):
        self.assertTrue(seasonal_profile(synthetic_history(True))["significant"])
        trend = synthetic_history()
        for i, c in enumerate(trend):
            c["value"] = str(Decimal(100) * Decimal("1.1") ** i)
        self.assertFalse(seasonal_profile(trend)["significant"])

    def test_seasonal_company_rejects_mechanical_average_quarterly_growth(self):
        driver = synthetic_driver()
        periods = forecast_periods(driver, 8)
        with self.assertRaisesRegex(ValueError, "cannot use average quarterly growth"):
            coverage_structure_assumptions([driver], periods, synthetic_structure("quarterly_growth"),
                                          decided_by="automation:test")

    def test_yoy_and_factor_methods_execute_the_seasonal_pattern(self):
        driver = synthetic_driver()
        periods = forecast_periods(driver, 8)
        for method in ("year_over_year_growth", "seasonal_factor"):
            structure = synthetic_structure(method)
            assumptions = coverage_structure_assumptions([driver], periods, structure, decided_by="automation:test")
            results = compute_structure_results([driver], assumptions, periods, structure)
            values = [Decimal(c["value"]) for c in results[0]["cells"]]
            self.assertEqual(values, [Decimal(v) for v in (100, 120, 100, 280) * 2])
            self.assertTrue(all(c["assumption_refs"] for c in results[0]["cells"]))

    def test_completion_connects_seasonal_method_to_generated_results(self):
        from dalton_core.forecast_coverage import complete_structure_definition
        driver = synthetic_driver()
        structure = synthetic_structure("quarterly_growth")
        line = structure["lines"][0]
        line.update(statement="income", period_kind="duration", annual_semantics="sum_quarters",
                    annual_forecast_method=None)
        source = {"concept": driver["concept"], "statement": "income", "status": "filed",
                  "period_basis": "duration", "cells": {c["period_end"]: c for c in driver["history"]}}
        completed = complete_structure_definition(structure, company_spec={}, financial_inputs={"filed_lines": [source]})
        self.assertEqual(completed["lines"][0]["forecast_method"], "year_over_year_growth")
        self.assertIn("季节性", completed["coverage"][0]["reason"])
        periods = forecast_periods(driver, 8)
        assumptions = coverage_structure_assumptions([driver], periods, completed, decided_by="automation:test")
        results = compute_structure_results([driver], assumptions, periods, completed)
        self.assertEqual(Decimal(results[0]["cells"][3]["value"]), Decimal(280))

    def test_legacy_short_published_record_reads_but_new_write_is_refused(self):
        from dalton_core.model_forecast_driver import ForecastModelAuthority, body_hash, validate_forecast_model
        from dalton_core.store import DaltonStore, content_hash
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        record = ForecastModelAuthority(store).publish(model())
        record.pop("status", None)
        record.pop("forecast_policy")
        record["forecast_periods"] = record["forecast_periods"][:4]
        record["body_hash"] = body_hash(record)
        record["content_hash"] = content_hash({k:v for k,v in record.items() if k != "content_hash"})
        self.assertEqual(len(validate_forecast_model(record)["forecast_periods"]), 4)
        with self.assertRaisesRegex(ForecastModelValidationError, "at least 8"):
            validate_new_forecast_rules(record)

    def test_factor_phase_tracks_dates_when_start_skips_preliminary_quarter(self):
        driver = synthetic_driver()
        periods = forecast_periods(driver, 8, policy={"latest_actual_end": "2027-03-31"})
        assumptions = coverage_structure_assumptions([driver], periods, synthetic_structure("seasonal_factor"), decided_by="automation:test")
        self.assertEqual(Decimal(assumptions[0]["value"]), Decimal(120))

    def test_reporting_boundary_changes_digest_and_triggers_once(self):
        from tests.test_model_forecast_driver import ledger, spec
        from dalton_core.company_model_inputs import build_model_inputs
        specification = spec()
        table = build_model_inputs(ledger(), specification)
        body = build_forecast_model(specification, table)
        boundary = {"latest_actual_end": "2026-05-31", "fiscal_year_end": None, "preliminary": None}
        self.assertIsNone(pending_action(body, specification, table, reporting_boundary=boundary))
        next_boundary = {**boundary, "latest_actual_end": "2026-08-31"}
        self.assertEqual(pending_action(body, specification, table, reporting_boundary=next_boundary), "rules_upgrade")
        self.assertNotEqual(model_digest(specification, table, reporting_boundary=boundary),
                            model_digest(specification, table, reporting_boundary=next_boundary))


if __name__ == "__main__":
    unittest.main()
