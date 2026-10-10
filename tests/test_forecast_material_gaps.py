import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from dalton_core.forecast_material_gaps import gap_plans,open_gap_requests
from dalton_core import material_requests
from dalton_core.material_acquisition import TargetedSourceRung
from dalton_core.mission_model_forecast_lane import MissionModelForecastLaneCoordinator


class ForecastGapTests(unittest.TestCase):
    def spec(self,company="company:synthetic"):
        return {"company_ref":company,"spec_id":"spec:synthetic","revenue_drivers":[{"kind":"segment","ref":"segment","basis_identity":None}]}

    def test_driver_contract_gap_has_governed_acquisition_routes(self):
        plan=gap_plans(self.spec(),{"periods":["2026-06-30"]})[0]
        self.assertEqual(plan["key"],"economic_driver_history")
        for source in ("sec_edgar","alphaengine","web"):
            self.assertTrue(TargetedSourceRung(source).applies(plan))
        self.assertIn("full segment identity",plan["need"]["question"])

    def test_cash_addback_is_requested_as_missing_income_authority(self):
        spec=self.spec();spec["revenue_drivers"]=[]
        spec["financial_statement_structure"]={"lines":[{"kind":"filed","concept":"synthetic:Amortization","statement":"income","label":"Amortization"}]}
        plan=gap_plans(spec,{"filed_lines":[{"concept":"synthetic:Amortization","status":"filed","statement":"cash"}]})[0]
        self.assertEqual(plan["key"],"statement_identity:synthetic:Amortization")
        self.assertIn("cannot replace an income expense",plan["need"]["question"])

    def test_tax_and_shares_requests_do_not_treat_material_as_numeric_admission(self):
        record={"results":[{"status":"unavailable","role":"income_tax_expense","ref":"tax","reason":"Discrete tax lacks normalization authority"},
                           {"status":"unavailable","role":"diluted_weighted_average_shares","ref":"shares","reason":"Missing quarter share source"}]}
        spec=self.spec();spec["revenue_drivers"]=[]
        plans=gap_plans(spec,{},record=record)
        self.assertEqual({p["key"] for p in plans},{"normalised_tax_basis","diluted_share_basis"})
        self.assertTrue(all("source admission must pass" in p["acceptance"]["model_input_admission"] for p in plans))

    def test_requests_are_idempotent_and_do_not_modify_a_core_model(self):
        with tempfile.TemporaryDirectory() as state:
            first=open_gap_requests(state,self.spec(),{"periods":["2026-06-30"]})
            second=open_gap_requests(state,self.spec(),{"periods":["2026-06-30"]})
            self.assertEqual(first[0]["id"],second[0]["id"])
            self.assertEqual(second[0]["write_status"],"duplicate")
            self.assertEqual(len(material_requests.open_requests(state)),1)
            from pathlib import Path
            self.assertFalse((Path(state)/"core.sqlite").exists())

    def test_current_partial_model_requests_material_without_launching_or_revising(self):
        with tempfile.TemporaryDirectory() as state:
            spec=self.spec()
            models=SimpleNamespace(latest=lambda _:{"forecast_policy":{"latest_actual_end":"2026-06-30"},"results":[]})
            missions=SimpleNamespace(latest_company_model_spec=lambda _:spec)
            launcher=SimpleNamespace(state_dir=state)
            lane=MissionModelForecastLaneCoordinator(missions=missions,models=models,launcher=launcher,
                mission=lambda:{"universe":[{"company_ref":spec["company_ref"]}]},failure_ledger_dir=state)
            with patch("dalton_core.mission_model_forecast_lane.pending_companies",return_value=[]):
                self.assertEqual(lane.dispatch_once()["status"],"idle")
                self.assertEqual(lane.dispatch_once()["status"],"idle")
            self.assertEqual(len(material_requests.open_requests(state)),1)

    def test_complete_independent_inputs_open_no_gap(self):
        spec=self.spec();spec["revenue_drivers"]=[{"kind":"volume","basis_concept":"synthetic:Units"}]
        self.assertEqual(gap_plans(spec,{"filed_lines":[]},record={"results":[],"execution_contract":{"drivers":{"status":"executed"}}}),[])

    def test_entire_batch_input_failure_still_opens_each_companys_own_source_request(self):
        with tempfile.TemporaryDirectory() as state:
            spec=self.spec();spec["revenue_drivers"]=[{"kind":"volume","basis_concept":"synthetic:Units"}]
            models=SimpleNamespace(latest=lambda _:None)
            missions=SimpleNamespace(latest_company_model_spec=lambda _:spec)
            lane=MissionModelForecastLaneCoordinator(missions=missions,models=models,
                launcher=SimpleNamespace(state_dir=state), mission=lambda:{"universe":[{"company_ref":spec["company_ref"]}]},
                failure_ledger_dir=state)
            with patch("dalton_core.mission_model_forecast_lane.pending_companies",side_effect=ValueError("batch selector failure")), \
                 patch("dalton_core.company_model_inputs.build_model_inputs",side_effect=ValueError("this company lacks exact share context")):
                self.assertEqual(lane.dispatch_once()["status"],"unavailable")
                self.assertEqual(lane.dispatch_once()["status"],"unavailable")
            requests=material_requests.open_requests(state)
            self.assertEqual(len(requests),1)
            self.assertIn("this company lacks exact share context",requests[0]["need"]["question"])
            self.assertNotIn("batch selector failure",requests[0]["need"]["question"])
