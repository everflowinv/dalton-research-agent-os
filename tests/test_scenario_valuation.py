"""Synthetic twelve-month valuation, source-bound to the frozen driver model."""
import copy
import unittest
from decimal import Decimal
from dalton_core.scenario_valuation import build_valuation,replay,forward_eps,ScenarioValuationAuthority,ScenarioValuationError,LEGACY_RULE_REF
from dalton_core.forecast_sensitivity import recompute_linked,SensitivityUnavailable
from dalton_core.model_forecast_driver import ForecastModelAuthority
from dalton_core.store import DaltonStore
from tests.test_forecast_financial_execution import economic_model


def inputs():
    body,spec=economic_model();store=DaltonStore(":memory:")
    try:model=ForecastModelAuthority(store).publish(body)
    finally:store.close()
    binding={"model_version_ref":model["id"],"model_version_hash":model["content_hash"]}
    scenarios=[]
    for key,probability,delta in [("bear",".25","-.1"),("base",".5","0"),("bull",".25",".1")]:
        scenarios.append({"id":"scenario:"+key,"content_hash":key[0]*64,"key":key,"name":key,"probability":probability,
            "probability_basis":{"kind":"template_prior","because":"Explicit template prior awaiting evidence","source_refs":["doc-passage:units:0-100:aaaaaaaaaaaa"],"as_of":"2026-01-01"}, "model_binding":binding,
            "driver_deltas":[{"driver_ref":"economic:volume","mode":"relative_level","delta":delta,"because":"Scenario volume growth", "refs":["doc-passage:units:0-100:aaaaaaaaaaaa"]}],
            "triggers":[{"label":"Reported units enter scenario interval"}]})
    price={"id":"price:1","content_hash":"p"*64,"company_ref":"company:test","as_of":"2026-01-01","close":"200","currency":"USD"}
    pe=[{"as_of":f"2025-{month:02d}-{day:02d}","value":str(10+i),"basis":"gaap","source_ref":"historical-pe:"+str(i)}
        for i,(month,day) in enumerate((m,d) for m in range(1,11) for d in (1,15))]
    dividends={"status":"confirmed_none","amount":"0","source_ref":"company-filing:no-dividends","window":{"start":"2026-01-01","end":"2027-01-01"}}
    return {"company_ref":"company:test","model":model,"scenarios":scenarios,"price":price,"pe_history":pe,"dividends":dividends}


class ScenarioValuationTests(unittest.TestCase):
    def annual_model(self):
        from dalton_core.annual_share_forecast import annual_share_source
        model=inputs()["model"]
        model["forecast_policy"]["fiscal_year_end"]="2025-12-31"
        line={"concept":"us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding","duration_facts":[
            {"period_start":f"{year}-01-01","period_end":f"{year}-12-31","value":str(value),"unit":"shares",
             "source_forms":["10-K"],"source_accessions":[f"synthetic-{year}"]} for year,value in [(2024,120),(2025,100)]]}
        source=annual_share_source(line)
        model["financial_statement_structure_replay"]["forecast_methods"].append({"line_ref":"shares","annual_status":"validated","annual_forecast_source":source})
        return model,line

    def test_exact_fiscal_window_uses_independent_annual_shares(self):
        from dalton_core.annual_share_forecast import annual_share_forecast
        model,line=self.annual_model()
        value=forward_eps(model,"2025-12-31","2026-12-31")
        numerator=next(r for r in model["results"] if r["role"]=="diluted_eps_numerator")
        total=sum(Decimal(c["value"]) for c in numerator["cells"][:4])
        denominator=Decimal(annual_share_forecast(line,period_start="2026-01-01",period_end="2026-12-31")["value"])
        self.assertEqual(Decimal(value["value"]),total/denominator)
        self.assertEqual(value["annual_shares"]["steps"],1)
        self.assertEqual(value["annual_shares"]["as_of"],"2025-12-31")
        self.assertNotEqual(value["value"],forward_eps(model,"2025-12-31","2026-12-31",prefer_annual=False)["value"])

    def test_cross_fiscal_window_keeps_explicit_quarter_weighting(self):
        model,_=self.annual_model()
        value=forward_eps(model,"2026-03-31","2027-03-31")
        self.assertNotIn("annual_shares",value)
        self.assertIn("day-weighted",value["basis"])

    def test_annual_source_tamper_and_missing_common_income_refuse(self):
        model,_=self.annual_model()
        model["financial_statement_structure_replay"]["forecast_methods"][-1]["annual_forecast_source"]["base_value"]="999"
        with self.assertRaisesRegex(ScenarioValuationError,"does not replay"):
            forward_eps(model,"2025-12-31","2026-12-31")
        model,_=self.annual_model()
        model["results"]=[r for r in model["results"] if r["role"]!="diluted_eps_numerator"]
        with self.assertRaisesRegex(ScenarioValuationError,"four complete common-income"):
            forward_eps(model,"2025-12-31","2026-12-31")

    def test_historical_valuation_rule_remains_replayable(self):
        value=build_valuation(**inputs(),rule_ref=LEGACY_RULE_REF)
        self.assertEqual(replay(value),value)

    def test_workbook_economic_units_use_readable_chinese(self):
        from dalton_core.fund_xlsx_export import _model_unit_suffix
        self.assertEqual(_model_unit_suffix("count"),"（个）")
        self.assertEqual(_model_unit_suffix("usd_per_unit"),"（美元/个）")

    def test_targets_and_weighted_return_are_recomputed(self):
        data=inputs();value=build_valuation(**data)
        self.assertEqual(value["status"],"available",value["reason"])
        self.assertEqual(value["window"]["start"],data["price"]["as_of"])
        self.assertEqual(value["window"]["end"],"2027-01-01")
        for row in value["scenarios"]:
            self.assertEqual(Decimal(row["target_price"]),Decimal(row["eps"]["value"])*Decimal(row["pe"]))
        expected=sum(Decimal(r["probability"])*Decimal(r["return_percent"]) for r in value["scenarios"])
        self.assertAlmostEqual(Decimal(value["summary"]["expected_return_percent"]),expected,places=20)
        replay(value)

    def test_no_model_eps_or_dividend_input_is_not_a_proxy_or_zero(self):
        for field in ("model","dividends"):
            data=inputs();data[field]=None
            value=build_valuation(**data)
            self.assertEqual(value["status"],"unavailable")
            self.assertEqual(value["scenarios"],[])
        data=inputs();data["model"]["results"]=[r for r in data["model"]["results"] if r["role"]!="diluted_eps"]
        value=build_valuation(**data)
        self.assertEqual(value["status"],"unavailable")
        self.assertIn("EPS",value["reason"])

    def test_probabilities_and_stale_model_binding_are_refused(self):
        data=inputs();data["scenarios"][0]["probability"]=".3"
        self.assertIn("sum exactly",build_valuation(**data)["reason"])
        data=inputs();data["scenarios"][1]["model_binding"]["model_version_hash"]="old"
        self.assertIn("stale",build_valuation(**data)["reason"])

    def test_joint_volume_price_shock_recalculates_income_and_rejects_absurd_scale(self):
        data=inputs();model=data["model"]
        deltas=[{"driver_ref":ref,"mode":"relative_level","delta":amount,"because":"Source-bound scenario", "refs":["doc-passage:synthetic:0-100:aaaaaaaaaaaa"]}
                for ref,amount in [("economic:volume",".1"),("economic:price",".05")]]
        moved=recompute_linked(model,deltas)
        before=next(r for r in model["results"] if r["role"]=="revenue")["cells"][0]["value"]
        after=next(r for r in moved["results"] if r["role"]=="revenue")["cells"][0]["value"]
        self.assertAlmostEqual(Decimal(after)/Decimal(before),Decimal("1.155"),places=8)
        deltas[0]["delta"]="100"
        with self.assertRaises(SensitivityUnavailable): recompute_linked(model,deltas)

    def test_valuation_versions_are_append_only_and_idempotent(self):
        value=build_valuation(**inputs());store=DaltonStore(":memory:");self.addCleanup(store.close)
        authority=ScenarioValuationAuthority(store)
        first=authority.publish(value);again=authority.publish(value)
        self.assertEqual(first["id"],again["id"]);self.assertEqual(again["status_marker"],"duplicate")
        data=inputs();data["price"]["close"]="210";data["price"]["id"]="price:2"
        second=authority.publish(build_valuation(**data))
        self.assertEqual(second["prior_version_ref"],first["id"])
        self.assertEqual(second["version"],2)

if __name__=="__main__":unittest.main()
