import json
import unittest
from pathlib import Path

from dalton_core.company_model_inputs import build_model_inputs
from dalton_core.company_model_spec import CompanyModelSpecError, build_prompt, spec_from_response
from dalton_core.driver_template import (
    COST_DRIVER_TEMPLATES, COST_REGISTRY_HASH, cost_prompt_block,
    cost_slot_ids, cost_template_gaps,
)
from dalton_core.model_forecast_driver import build_drivers
from dalton_core.store import canonical_json
from tests.test_company_model_inputs import FakeMissions, _line
from tests.test_company_model_spec import DECIDED_BY, STATE, _spec

ROOT = Path(__file__).resolve().parents[1]


class CostRegistryTests(unittest.TestCase):
    def test_requested_classification_slots_are_frozen(self):
        self.assertEqual(cost_slot_ids("commodity_cycle"),
                         ("raw_material_spread", "energy_intensity", "utilisation_cost"))
        self.assertEqual(cost_slot_ids("capital_cycle"),
                         ("depreciation_curve", "maintenance_capex"))
        self.assertEqual(cost_slot_ids("contract_compounder"),
                         ("delivery_cost", "revenue_per_employee"))
        self.assertEqual(cost_slot_ids("structural_growth"), ("unit_cost_curve",))
        self.assertEqual(cost_slot_ids("turnaround"), ("fixed_cost_removal",))

    def test_published_v1_is_canonical_and_hash_bound(self):
        published = json.loads((ROOT / "deploy/phase9/w5-cost-driver-template-v1.json").read_text())
        self.assertEqual(published["content_hash"], COST_REGISTRY_HASH)
        self.assertEqual(canonical_json({k: v for k, v in published.items()
                                         if k != "content_hash"}),
                         canonical_json(dict(COST_DRIVER_TEMPLATES)))

    def test_supply_gap_uses_cost_slots(self):
        gaps = cost_template_gaps("turnaround", ["revenue demand"],
                                  subject="档案 supply_and_cost")
        self.assertIn("fixed_cost_removal", gaps[0])


class CostSpecToForecastTests(unittest.TestCase):
    def state(self, classification="contract_compounder"):
        return {**STATE, "industry_classification": classification}

    def test_prompt_selects_cost_template_after_classification(self):
        prompt = build_prompt(self.state())
        self.assertIn("COST DRIVER TEMPLATE (contract_compounder)", prompt)
        self.assertIn("delivery_cost", prompt)
        self.assertNotIn("raw_material_spread\t", prompt)

    def test_cross_class_cost_slot_is_refused(self):
        body = _spec()
        body["expense_lines"][0]["cost_driver_slot"] = "raw_material_spread"
        with self.assertRaisesRegex(CompanyModelSpecError, "not in this classification"):
            spec_from_response(self.state(), body, decided_by=DECIDED_BY)

    def test_unbound_cost_slot_requires_explicit_reason(self):
        body = _spec()
        body["expense_lines"][0]["cost_driver_slot"] = None
        with self.assertRaisesRegex(CompanyModelSpecError, "unbound_reason"):
            spec_from_response(self.state(), body, decided_by=DECIDED_BY)
        body["expense_lines"][0]["cost_driver_unbound_reason"] = (
            "This company reports a project-specific cost outside the template.")
        spec = spec_from_response(self.state(), body, decided_by=DECIDED_BY)
        self.assertIsNone(spec["expense_lines"][0]["cost_driver_slot"])

    def test_legacy_spec_bytes_do_not_gain_optional_cost_keys(self):
        spec = spec_from_response(self.state(), _spec(), decided_by=DECIDED_BY)
        self.assertNotIn("cost_driver_slot", spec["expense_lines"][0])

    def test_cost_slot_reaches_forecast_driver_without_becoming_actual(self):
        body = _spec()
        body["expense_lines"][0]["cost_driver_slot"] = "delivery_cost"
        decided = spec_from_response(self.state(), body, decided_by=DECIDED_BY)
        missions = FakeMissions([
            _line("us-gaap:Revenues", "2026-03-01", "2026-05-31", "100"),
            _line("us-gaap:CostOfRevenue", "2026-03-01", "2026-05-31", "70"),
            _line("us-gaap:SellingGeneralAndAdministrativeExpense",
                  "2026-03-01", "2026-05-31", "10"),
        ])
        table = build_model_inputs(missions, decided)
        row = next(item for item in table["rows"] if item["ref"] == "cost-of-services")
        self.assertEqual(row["cost_driver_slot"], "delivery_cost")
        driver = next(item for item in build_drivers(table)
                      if item.get("concept") == "us-gaap:CostOfRevenue")
        self.assertEqual(driver["cost_driver_slots"], ["delivery_cost"])
        self.assertEqual(driver["history"][0]["value"], "70")


if __name__ == "__main__":
    unittest.main()
