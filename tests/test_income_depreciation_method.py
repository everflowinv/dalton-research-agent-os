from __future__ import annotations
import copy
from decimal import Decimal
import unittest

from dalton_core.forecast_coverage import income_depreciation_policy, complete_structure_definition
from dalton_core.forecast_financial_execution import financial_mechanisms
from dalton_core.company_financial_statement_structure import materialize_financial_statement_structure,forecast_structure_binding,FinancialStatementStructureError
from dalton_core.model_forecast_driver import build_structured_forecast_model,ForecastModelValidationError
from dalton_core.filed_period_composition import derive
from dalton_core.store import content_hash

DA="us-gaap:DepreciationAndAmortization"

def fixture():
    from tests.test_filed_period_composition import fixture as income_fixture
    spec,inputs,sources=income_fixture()
    for line in spec["financial_statement_structure"]["lines"]:
        if line.get("concept")=="opex":line["concept"]=DA
    for line in inputs["filed_lines"]:
        if line["concept"]=="opex":line["concept"]=DA
    for source in sources:
        for row in source["rows"]:
            if row["concept"]=="opex":row["concept"]=DA
        source["rows_hash"]=content_hash(source["rows"])
    for expense in spec["expense_lines"]:
        if expense["basis_concept"]=="opex":expense["basis_concept"]=DA
    debt=copy.deepcopy(inputs["filed_lines"][0])
    debt.update(concept="us-gaap:LongTermDebtNoncurrent",statement="balance",period_basis="instant")
    for cell in debt["cells"].values():cell.update(period_start=None,value="1000")
    inputs["filed_lines"].append(debt)
    inputs["historical_income_composition"]=derive(spec,inputs,sources)
    return spec,inputs

def build(spec,inputs):
    structure,replay=materialize_financial_statement_structure(spec,inputs)
    binding=forecast_structure_binding(structure,replay,inputs)
    model=build_structured_forecast_model(spec,inputs,structure=structure,replay=replay,binding=binding)
    return structure,replay,model

class IncomeDepreciationTests(unittest.TestCase):
    def test_income_method_and_all_eight_quarter_profit_eps_remain_executable(self):
        spec,inputs=fixture();s,r,f=build(spec,inputs)
        self.assertEqual(next(l for l in s["lines"] if l["ref"]=="opex")["forecast_method"],"share_of_line")
        mechanisms,_=financial_mechanisms(f)
        self.assertEqual(mechanisms["lines"][0]["method"],"share_of_line")
        self.assertEqual(mechanisms["lines"][0]["status"],"computed")
        for role in ("operating_income","pretax_income","net_income","diluted_eps"):
            result=next(x for x in f["results"] if x.get("role")==role)
            self.assertEqual(result["status"],"computed")
            self.assertEqual(len(result["cells"]),8)
        self.assertTrue(all(len(x["tested_periods"])==12 for x in r["formulas"]))

    def test_carry_forward_mean_is_an_explicit_income_assumption(self):
        spec,inputs=fixture();line=next(x for x in spec["financial_statement_structure"]["lines"] if x["ref"]=="opex")
        line.update(forecast_method="carry_forward_mean",forecast_base_ref=None)
        s,r,f=build(spec,inputs);result=next(x for x in f["results"] if x.get("driver_ref")=="concept:"+DA)
        source=next(l for l in inputs["filed_lines"] if l["concept"]==DA)
        mean=sum((Decimal(source["cells"][end]["value"]) for end in sorted(source["cells"])[-4:]),Decimal(0))/4
        self.assertEqual({Decimal(c["value"]) for c in result["cells"]},{mean})

    def test_cash_unknown_method_short_history_and_foreign_base_not_relaxed(self):
        for modify in (lambda l:l.update(statement="cash"),lambda l:l.update(forecast_method="unavailable",forecast_base_ref=None),lambda l:l.update(forecast_base_ref="missing")):
            spec,inputs=fixture();modify(next(x for x in spec["financial_statement_structure"]["lines"] if x["ref"]=="opex"))
            self.assertIsNone(income_depreciation_policy(spec,inputs))
        spec,inputs=fixture();source=next(x for x in inputs["filed_lines"] if x["concept"]==DA);source["cells"]={k:v for k,v in list(source["cells"].items())[:7]}
        self.assertIsNone(income_depreciation_policy(spec,inputs))

    def test_complete_asset_roll_with_capex_assumption_keeps_existing_route(self):
        spec,inputs=fixture();source=next(x for x in inputs["filed_lines"] if x["concept"]==DA)
        asset=copy.deepcopy(source);asset.update(concept="us-gaap:PropertyPlantAndEquipmentNet",statement="balance",period_basis="instant")
        for cell in asset["cells"].values():cell.update(period_start=None,value="1000")
        inputs["filed_lines"].append(asset)
        inputs["cash_flow_inputs"]=[{"role":"capital_expenditure","status":"filed","series":{"quarters":[{} for _ in range(4)]}}]
        spec["cash_flow_companion"]={"lines":[{"role":"capital_expenditure","forecast_method":"share_of_line"}]}
        self.assertIsNone(income_depreciation_policy(spec,inputs))
        completed=complete_structure_definition(spec["financial_statement_structure"],company_spec=spec,financial_inputs=inputs)
        self.assertEqual(next(l for l in completed["lines"] if l["ref"]=="opex")["forecast_method"],"roll_forward")
        self.assertNotIn("income_depreciation_forecast_policy",completed)

    def test_marker_hash_and_exact_method_source_history_drift_reject(self):
        spec,inputs=fixture();s,r,f=build(spec,inputs)
        for mutate in (lambda p:p.update(content_hash="0"*64),lambda p:p["lines"][0].update(method="quarterly_growth"),lambda p:p["lines"][0].update(source_history_hash="0"*64)):
            bad=copy.deepcopy(f);policy=bad["financial_statement_structure"]["income_depreciation_forecast_policy"];mutate(policy)
            if policy["content_hash"]!="0"*64:policy["content_hash"]=content_hash({k:v for k,v in policy.items() if k!="content_hash"})
            with self.assertRaises(ForecastModelValidationError):financial_mechanisms(bad)

    def test_old_without_marker_keeps_roll_mechanism_and_no_bypass(self):
        spec,inputs=fixture();s,r,f=build(spec,inputs)
        f["financial_statement_structure"].pop("income_depreciation_forecast_policy")
        d=next(d for d in f["drivers"] if d["concept"]==DA);d.update(forecast_method="roll_forward",forecast_base_ref="missing")
        mechanisms,overrides=financial_mechanisms(f)
        self.assertEqual(mechanisms["lines"][0]["method"],"roll_forward")
        self.assertEqual(mechanisms["lines"][0]["status"],"unavailable")
        self.assertEqual([a for a in overrides if a["driver_ref"] == "concept:"+DA],[])
