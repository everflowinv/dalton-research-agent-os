"""Synthetic acceptance of executable economic and capital cash mechanisms."""
import copy
import unittest
from decimal import Decimal
from dalton_core.forecast_financial_execution import (driver_execution, financial_mechanisms,
    execute_financial_model, validate_execution_contract, validate_assumption_sources, additional_drivers)
from dalton_core.forecast_company_fcf import append_company_fcf, validate_fcf_definition
from dalton_core.model_forecast_driver import (_result, _computed_cell, _finish, forecast_periods,
    ForecastModelValidationError, build_structured_forecast_model, revise_assumptions, ForecastModelAuthority)
from dalton_core.company_financial_statement_structure import validate_financial_statement_structure, forecast_structure_binding
from dalton_core.store import DaltonStore, content_hash
from tests.test_company_financial_statement_structure import input_line
from tests.test_financial_structure_forecast_consumer import financial_inputs, forecastable_proposal


def economic_model(operator="product", *, company_ref=None):
    table = financial_inputs()
    if company_ref is not None:
        table["company_ref"] = company_ref
    values = (100, 110, 120, 130)
    table["filed_lines"] += [input_line("units", values, unit="count"), input_line("price", (10,)*4, unit="usd_per_unit")]
    drivers = [{"ref": "volume", "kind": "volume", "basis_concept": "units", "label": "units"},
               {"ref": "price", "kind": "price", "basis_concept": "price", "label": "price"}]
    if operator == "contract_conversion":
        table["filed_lines"].append(input_line("book", (2000,2200,2400,2600)))
        drivers = [{"ref":"book", "kind":"contract_book", "basis_concept":"book", "label":"book"}]
    if operator == "sum":
        table["filed_lines"] += [input_line("segmentA", (400,440,480,520)), input_line("segmentB", (600,660,720,780))]
        drivers = [{"ref":"a", "kind":"segment", "basis_concept":"segmentA", "label":"A"},
                   {"ref":"b", "kind":"segment", "basis_concept":"segmentB", "label":"B"}]
    spec = {"spec_id": table["spec_ref"], "company_ref": table["company_ref"], "content_hash":"b"*64,
        "revenue_anchor_concept":"revenue", "expense_lines":[], "revenue_drivers":drivers,
        "horizon":{"forecast_quarters":8}, "driver_formulas":[{"output_ref":"revenue", "operator":operator,
         "driver_refs":[d["ref"] for d in drivers], "because":"Synthetic company reports exact economic quantities"}]}
    structure,replay = validate_financial_statement_structure(forecastable_proposal(table), spec, table)
    body = build_structured_forecast_model(spec, table, structure=structure, replay=replay,
        binding=forecast_structure_binding(structure,replay,table))
    return body,spec


def line(ref, role, values, periods, *, driver_ref=None):
    result = _result(ref,role,ref,"usd","synthetic",driver_ref=driver_ref)
    result["cells"] = [_computed_cell(ref,p,Decimal(str(v))) for p,v in zip(periods,values)]
    return _finish(result)


def capital_body():
    body,spec = economic_model()
    periods=body["forecast_periods"]
    for role,concept,value,statement in [("depreciable_assets","us-gaap:PropertyPlantAndEquipmentNet",1000,"balance"),
        ("depreciation_amortization","us-gaap:DepreciationDepletionAndAmortization",50,"cash"),
        ("debt_current","us-gaap:LongTermDebtCurrent",200,"balance")]:
        history=copy.deepcopy(body["drivers"][0]["history"])
        for c in history: c.update(concept=concept,value=str(value),period_start=None if statement=="balance" else c["period_start"])
        body["drivers"].append({"ref":role,"role":role,"concept":concept,"unit":"usd","statement":statement,
            "history":history,"structure_line_ref":role,"forecast_method":"roll_forward" if role=="depreciation_amortization" else "hold_last"})
        body["financial_statement_structure"]["lines"].append({"ref":role,"role":role,"kind":"filed","concept":concept})
    body["results"] += [line("result:capital_expenditure","capital_expenditure",[100]*8,periods),
        line("result:debt_current","debt_current",[200]*8,periods)]
    return body


class FinancialExecutionTests(unittest.TestCase):
    def test_generated_volume_price_segments_and_contract_conversion_execute(self):
        for op in ("product","sum","contract_conversion"):
            body,spec = economic_model(op)
            self.assertEqual(body["execution_contract"]["drivers"]["status"],"executed")
            self.assertEqual(len(body["forecast_periods"]),8)
            validate_execution_contract(body,publishing=True)
            revenue=next(r for r in body["results"] if r["role"]=="revenue")
            self.assertGreater(Decimal(revenue["cells"][0]["value"]),Decimal(1300))
            self.assertTrue(any(a["measure"]=="economic_driver_level" for a in body["assumptions"]))

    def test_revised_volume_recalculates_revenue_and_operating_income(self):
        body,spec=economic_model()
        store=DaltonStore(":memory:"); self.addCleanup(store.close)
        record=ForecastModelAuthority(store).publish(body)
        end=record["forecast_periods"][0]["end"]
        evidence={"kind":"doc_passage","ref":"doc-passage:synthetic-volume-guidance:0-100:aaaaaaaaaaaa","concept":None,"period_end":end,"accession":None}
        revised=revise_assumptions(record,[{"driver":"economic:volume","period":end,"value":"200","because":"Held company volume guidance","outside_band":{"reason":"The held volume guidance explicitly predicts a higher quantity than the previous filed range."}}],
            change_reason="human_revision",evidence_refs=[evidence],actor_ref="human:analyst")
        revenue=next(r for r in revised["results"] if r["role"]=="revenue")
        self.assertEqual(Decimal(revenue["cells"][0]["value"]),Decimal(2000))
        old=next(r for r in record["results"] if r["role"]=="operating_income")["cells"][0]["value"]
        new=next(r for r in revised["results"] if r["role"]=="operating_income")["cells"][0]["value"]
        self.assertNotEqual(old,new)
        ForecastModelAuthority(store).publish(revised)

    def test_missing_and_fake_execution_cannot_publish(self):
        body,spec=economic_model()
        declared=copy.deepcopy(spec); declared["driver_formulas"]=[]
        report,_=driver_execution(body,declared)
        self.assertEqual(report["status"],"unexecuted")
        body["execution_contract"]["specification"]= {k:declared.get(k,[]) for k in ("revenue_drivers","expense_lines","driver_formulas")}
        contract=body["execution_contract"]; contract.pop("content_hash"); contract["content_hash"]=content_hash(contract)
        with self.assertRaisesRegex(ForecastModelValidationError,"does not replay"):
            validate_execution_contract(body,publishing=True)

    def test_assets_roll_and_interest_use_total_debt(self):
        body=capital_body(); report,assumptions=financial_mechanisms(body)
        da=next(r for r in report["lines"] if r["method"]=="roll_forward")
        self.assertEqual(Decimal(da["periods"][0]["depreciation_amortization"]),Decimal("52.5"))
        self.assertEqual(Decimal(da["periods"][0]["closing"]),Decimal("1047.5"))
        interest=next(r for r in report["lines"] if r["method"]=="rate_times_balance")
        self.assertEqual(Decimal(interest["periods"][0]["debt_balance"]),Decimal(1200))
        self.assertEqual(Decimal(interest["periods"][0]["interest"]),Decimal(20))

    def test_missing_capital_bases_is_explicit(self):
        body=capital_body(); body["drivers"]=[d for d in body["drivers"] if d.get("role") not in {"depreciable_assets","debt_current","debt_noncurrent"}]
        report,overrides=financial_mechanisms(body)
        self.assertEqual(overrides,[])
        self.assertTrue(all(r["status"]=="unavailable" and "缺少" in r["reason"] for r in report["lines"]))

    def test_preliminary_actual_cannot_skip_an_asset_roll_quarter(self):
        body=capital_body()
        body["forecast_policy"]["latest_actual_end"]="2026-03-31"
        report,overrides=financial_mechanisms(body)
        da=next(r for r in report["lines"] if r["method"]=="roll_forward")
        self.assertEqual(da["status"],"unavailable")
        self.assertIn("禁止跳过",da["reason"])
        self.assertFalse(any(a["driver_ref"]=="depreciation_amortization" for a in overrides))

    def test_human_claim_is_rejected_but_document_passage_is_accepted(self):
        body,spec=economic_model(); a=next(a for a in body["assumptions"] if a["measure"]=="economic_driver_level")
        a["kind"]="human"; a["provenance"]["rule_ref"]=None
        a["refs"]=[{"kind":"claim","ref":"claim:unsupported"}]
        with self.assertRaisesRegex(ForecastModelValidationError,"doc-passage"):
            validate_assumption_sources(body)
        a["refs"]=[{"kind":"doc_passage","ref":"doc-passage:held:0-100:aaaaaaaaaaaa"}]
        validate_assumption_sources(body)

    def test_unpredictable_noncore_gain_does_not_block_core_operating_income(self):
        from dalton_core.model_forecast_driver import compute_structure_results
        body,spec=economic_model()
        structure=copy.deepcopy(body["financial_statement_structure"])
        structure["lines"].append({"ref":"equity-gain","role":"other_nonoperating_income_expense",
            "label":"Unrealised investment gain","kind":"filed","concept":"synthetic:InvestmentGain",
            "unit":"usd","forecast_method":"unavailable","forecast_base_ref":None})
        formula=next(f for f in structure["formulas"] if f["output_ref"]=="pretax")
        formula["terms"].append({"line_ref":"equity-gain","coefficient":"1"})
        results=compute_structure_results(body["drivers"],body["assumptions"],body["forecast_periods"],structure)
        self.assertEqual(next(r for r in results if r["role"]=="operating_income")["status"],"computed")
        self.assertEqual(next(r for r in results if r["role"]=="pretax_income")["status"],"unavailable")

    def test_company_fcf_deducts_lease_software_contracts_and_dates_one_off(self):
        body=capital_body(); periods=body["forecast_periods"]
        body["results"] += [line("result:operating_cash_flow","operating_cash_flow",[500]*8,periods),
            line("result:free_cash_flow","free_cash_flow",[400]*8,periods)]
        components=[{"role":"operating_cash_flow","concept":"cfo","coefficient":"1"},
            {"role":"capital_expenditure","concept":"capex","coefficient":"-1"}]
        for role,concept,amount in [("operating_cash_flow","cfo",500),("capital_expenditure","capex",100),
            ("finance_lease_principal","leases",20),("capitalized_software","software",30),("capitalized_contract_costs","contracts",10)]:
            history=copy.deepcopy(body["drivers"][0]["history"])
            for c in history: c.update(concept=concept,value=str(amount))
            body["drivers"].append({"ref":concept,"role":role,"concept":concept,"unit":"usd","history":history})
            if role not in {"operating_cash_flow","capital_expenditure"}: components.append({"role":role,"concept":concept,"coefficient":"-1"})
        definition={"source_ref":"filing:synthetic","components":components,"one_off_items":[{"period_end":periods[0]["end"],"value":"50","direction":"outflow","source_ref":"doc-passage:cash-charge:0-100:aaaaaaaaaaaa","because":"One cash restructuring payment"}]}
        append_company_fcf(body,definition)
        byref={r["ref"]:r for r in body["results"]}
        self.assertEqual(Decimal(byref["result:free_cash_flow_company"]["cells"][0]["value"]),340)
        self.assertEqual(Decimal(byref["result:capital_expenditure_company"]["cells"][0]["value"]),160)
        adjusted=byref["result:free_cash_flow_company_ex_one_offs"]["cells"]
        self.assertEqual([Decimal(c["value"]) for c in adjusted[:2]],[390,340])
        next(c for c in definition["components"] if c["role"]=="capitalized_software")["concept"]="missing"
        append_company_fcf(body,definition)
        self.assertEqual(next(r for r in body["results"] if r["ref"]=="result:free_cash_flow_company")["status"],"unavailable")

if __name__=="__main__": unittest.main()
