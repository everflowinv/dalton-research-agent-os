"""Currency spelling never changes filed arithmetic, evidence or old results."""
import copy
import unittest
from unittest.mock import patch

from dalton_core.forecast_financial_execution import additional_drivers
from dalton_core.model_forecast_driver import (
    ForecastModelValidationError, STRUCTURED_CASH_SCHEMA_VERSION, _normalize_driver,
)
from dalton_core.company_model_forecast import model_digest
from dalton_core.store import content_hash
from tests import test_mission_model_forecast_lane as lane_fixture
from tests import test_company_financial_statement_structure as structure_fixture
from tests import test_financial_structure_forecast_consumer as consumer_fixture


def inputs(*, company="company:example:a", unit="USD", operand_units=("USD", "USD")):
    source = {
        "concept": "example:SegmentRevenue", "statement": "income", "status": "filed",
        "cells": {"2025-12-31": {
            "period_start": "2025-10-01", "value": "250", "unit": unit,
            "basis": "derived_from_cumulative", "source_accessions": ["filing:q3", "filing:annual"],
            "source_forms": ["10-Q", "10-K"], "derived_from": [
                {"period_start": "2025-01-01", "period_end": "2025-09-30", "value": "100",
                 "unit": operand_units[0], "accession": "filing:q3", "form": "10-Q"},
                {"period_start": "2025-01-01", "period_end": "2025-12-31", "value": "350",
                 "unit": operand_units[1], "accession": "filing:annual", "form": "10-K"},
            ],
        }},
    }
    spec = {"company_ref": company, "revenue_drivers": [
        {"ref": "segment", "basis_concept": source["concept"], "label": "Segment revenue"}],
        "driver_formulas": [], "expense_lines": []}
    table = {"company_ref": company, "filed_lines": [source]}
    return spec, table


class DerivedHistoryUnitTests(unittest.TestCase):
    def driver(self, spec, table):
        result = additional_drivers(spec, table, [])
        self.assertEqual(len(result), 1)
        return result[0]

    def validate(self, driver):
        return _normalize_driver(driver, "drivers[0]", schema_version=STRUCTURED_CASH_SCHEMA_VERSION)

    def test_cross_company_case_variants_keep_exact_arithmetic_and_source_bytes(self):
        for company, units in [("company:example:a", ("USD", "USD")),
                               ("company:example:b", ("Usd", "uSD"))]:
            with self.subTest(company=company):
                spec, table = inputs(company=company, operand_units=units)
                original = copy.deepcopy(table)
                driver = self.driver(spec, table)
                self.assertEqual(driver["unit"], "usd")
                self.assertEqual([p["unit"] for p in driver["history"][0]["derived_from"]], ["usd", "usd"])
                self.validate(driver)
                self.assertEqual(table, original)
                expected = copy.deepcopy(original["filed_lines"][0]["cells"]["2025-12-31"]["derived_from"])
                for p in expected:
                    p["unit"] = "usd"
                self.assertEqual(driver["history"][0]["derived_from"], expected)
                self.assertEqual(driver["history"][0]["value"], "250")

    def test_already_canonical_operand_content_hash_does_not_change(self):
        spec, table = inputs(unit="usd", operand_units=("usd", "usd"))
        source = table["filed_lines"][0]["cells"]["2025-12-31"]["derived_from"]
        driver = self.driver(spec, table)
        self.assertEqual(content_hash(driver["history"][0]["derived_from"]), content_hash(source))
        self.validate(driver)

    def test_different_currency_is_not_converted_to_make_a_tie(self):
        spec, table = inputs(operand_units=("EUR", "USD"))
        driver = self.driver(spec, table)
        self.assertEqual(driver["history"][0]["derived_from"][0]["unit"], "EUR")
        with self.assertRaisesRegex(ForecastModelValidationError, "derived arithmetic does not replay"):
            self.validate(driver)

    def test_numeric_mismatch_still_fails_exact_decimal_replay(self):
        spec, table = inputs()
        table["filed_lines"][0]["cells"]["2025-12-31"]["value"] = "250.000000001"
        with self.assertRaisesRegex(ForecastModelValidationError, "derived arithmetic does not replay"):
            self.validate(self.driver(spec, table))

    def test_wrong_source_and_period_remain_refused(self):
        for field, value, error in [("accession", "filing:foreign", "do not match its sources"),
                                    ("period_end", "2025-09-29", "derived periods do not replay")]:
            with self.subTest(field=field):
                spec, table = inputs()
                table["filed_lines"][0]["cells"]["2025-12-31"]["derived_from"][0][field] = value
                with self.assertRaisesRegex(ForecastModelValidationError, error):
                    self.validate(self.driver(spec, table))

    def test_validator_has_not_been_relaxed_for_an_inconsistent_old_driver(self):
        spec, table = inputs()
        driver = self.driver(spec, table)
        driver["history"][0]["derived_from"][0]["unit"] = "USD"
        with self.assertRaisesRegex(ForecastModelValidationError, "derived arithmetic does not replay"):
            self.validate(driver)

    def test_consistent_historical_uppercase_model_is_still_readable_and_unchanged(self):
        spec, table = inputs()
        driver = self.driver(spec, table)
        driver["unit"] = "USD"
        for operand in driver["history"][0]["derived_from"]:
            operand["unit"] = "USD"
        before = copy.deepcopy(driver)
        self.validate(driver)
        self.assertEqual(driver, before)

    def test_structured_rule_change_naturally_launches_once_without_changing_spec_or_old_failure(self):
        fixture = lane_fixture.LaneTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        table = consumer_fixture.financial_inputs()
        table["company_ref"] = lane_fixture.ACN
        spec = structure_fixture.company_spec()
        spec["decided_by"] = "automation:test"
        spec["company_ref"] = lane_fixture.ACN
        spec["financial_statement_structure"] = consumer_fixture.forecastable_proposal(table)
        original = copy.deepcopy(spec)
        old_hash = content_hash

        def legacy_hash(body):
            if isinstance(body, dict) and "derived_history_unit_rule" in body:
                body = {k: v for k, v in body.items() if k != "derived_history_unit_rule"}
            return old_hash(body)

        with patch("dalton_core.mission_model_forecast_lane.pending_companies",
                   return_value=[(lane_fixture.ACN, spec, table)]), \
             patch("dalton_core.mission_model_forecast_lane.forecast_reporting_boundary", return_value=None):
            with patch("dalton_core.company_model_forecast.content_hash", side_effect=legacy_hash):
                old_digest = model_digest(spec, table)
                first = fixture.lane.dispatch_once()
            self.assertEqual(first["model_digest"], old_digest)
            fixture.launcher.finish(first["ticket_ref"], status="failed", summary={
                "failure_reason": "ForecastModelValidationError: drivers[0].history[0] derived arithmetic does not replay"})
            old_ticket = copy.deepcopy(fixture.launcher.tickets[first["ticket_ref"]])
            new = fixture.lane.dispatch_once()
            self.assertEqual(new["status"], "launched")
            self.assertNotEqual(new["model_digest"], old_digest)
            self.assertEqual(new["model_digest"], model_digest(spec, table))
            self.assertEqual(fixture.launcher.tickets[first["ticket_ref"]], old_ticket)
            fixture.launcher.finish(new["ticket_ref"], status="failed", summary={
                "forecast_status": "unavailable:economic_invariants", "failure_reason": "assumption_band: genuine current refusal"})
            self.assertEqual(fixture.lane.dispatch_once()["status"], "held")
            self.assertEqual(len(fixture.launcher.started), 2)
        self.assertEqual(spec, original)


if __name__ == "__main__":
    unittest.main()
