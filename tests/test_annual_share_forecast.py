import copy
import unittest
from decimal import Decimal
from dalton_core.annual_share_forecast import annual_share_source, annual_share_forecast
from dalton_core.store import content_hash


def filed_shares(values=(100,98,96)):
    return {"concept":"us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", "duration_facts":[
        {"period_start":f"{2023+i}-01-01", "period_end":f"{2023+i}-12-31", "period_kind":"cumulative",
         "unit":"shares", "value":str(value), "basis":"reported", "source_forms":["10-K"], "source_accessions":[f"filing:annual:{2023+i}"]}
        for i,value in enumerate(values)]}


class AnnualShareForecastTests(unittest.TestCase):
    def test_filed_annual_growth_is_independent_and_compounds_by_exact_fiscal_steps(self):
        source=annual_share_source(filed_shares())
        self.assertEqual(source["status"],"validated")
        self.assertEqual(source["as_of"],"2025-12-31")
        self.assertEqual(source["content_hash"],content_hash({k:v for k,v in source.items() if k!="content_hash"}))
        result=annual_share_forecast(filed_shares(),period_start="2027-01-01",period_end="2027-12-31")
        self.assertEqual(result["steps"],2)
        rate=(Decimal(98)/100-1+Decimal(96)/98-1)/2
        self.assertAlmostEqual(Decimal(result["value"]),96*(1+rate)**2,places=20)

    def test_direct_annual_value_wins_even_without_two_year_growth_history(self):
        line=filed_shares((100,))
        result=annual_share_forecast(line,period_start="2023-01-01",period_end="2023-12-31")
        self.assertEqual(result["value"],"100")
        self.assertEqual(result["method"],"direct_annual_filing")
        self.assertEqual(result["steps"],0)

    def test_quarterly_average_not_equal_to_annual_does_not_invent_a_historical_tie(self):
        line=filed_shares()
        line["cells"]={"2025-03-31":{"value":"200"},"2025-06-30":{"value":"200"},"2025-09-30":{"value":"200"},"2025-12-31":{"value":"200"}}
        result=annual_share_forecast(line,period_start="2026-01-01",period_end="2026-12-31")
        self.assertEqual(result["status"],"computed")
        self.assertLess(Decimal(result["value"]),100)
        self.assertIn("independently",result["source"]["hypothesis"])

    def test_missing_latest_prior_fiscal_year_refuses_compounding_over_a_gap(self):
        line=filed_shares();line["duration_facts"].pop(1)
        self.assertEqual(annual_share_source(line)["status"],"unavailable")

    def test_conflicting_annual_value_is_refused_instead_of_selecting_by_amount(self):
        line=filed_shares();other=copy.deepcopy(line["duration_facts"][-1]);other["value"]="97";line["duration_facts"].append(other)
        self.assertIn("conflicting",annual_share_source(line)["reason"])

    def test_partial_or_past_year_is_not_a_future_annual_forecast(self):
        for start,end in (("2026-01-01","2026-09-30"),("2022-01-01","2022-12-31")):
            self.assertEqual(annual_share_forecast(filed_shares(),period_start=start,period_end=end)["status"],"unavailable")

    def test_bad_unit_nonannual_form_and_derived_source_cannot_become_direct_annual_authority(self):
        for field,value in (("unit","usd"),("source_forms",["10-Q"]),("basis","derived_from_cumulative"),("value","0"),("source_accessions",[])):
            line=filed_shares()
            for fact in line["duration_facts"]:fact[field]=value
            self.assertEqual(annual_share_source(line)["status"],"unavailable",(field,value))

    def test_partial_ten_k_share_average_is_not_returned_as_direct_full_year(self):
        line=filed_shares((100,));line["duration_facts"][0]["period_start"]="2023-10-01"
        result=annual_share_forecast(line,period_start="2023-10-01",period_end="2023-12-31")
        self.assertEqual(result["status"],"unavailable")

if __name__ == "__main__":
    unittest.main()
