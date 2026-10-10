"""Synthetic acceptance of the new publication contract (no legacy mocks)."""
import copy,unittest
from datetime import date
from dalton_core.scenario_rules import validate_new_scenario_rules,partition_conditions
from dalton_core.scenario_valuation import build_valuation
from tests.test_scenario_valuation import inputs


def contract():
    data=inputs();partition={"observed_by":"sec_actual","metric_ref":"revenue","transform":"yoy_pct",
        "cutpoints":["5","15"],"source_refs":[data["model"]["id"]]}
    for row in data["scenarios"]:
        row.update(partition=copy.deepcopy(partition),triggers=partition_conditions(row["key"],partition,today=date(2026,1,1)),invalidators=[])
    return data,{**data["scenarios"][0]["model_binding"],"valuation_snapshot_ref":"valuation:1"}


class ScenarioRulesTests(unittest.TestCase):
    def test_partition_is_exhaustive_at_both_cutpoints_and_eps_is_recomputed(self):
        data,anchors=contract();validate_new_scenario_rules(data["scenarios"],anchors,today=date(2026,1,1))
        from decimal import Decimal
        for v,expected in [("4.9","bear"),("5","base"),("14.9","base"),("15","bull")]:
            matches=[]
            for row in data["scenarios"]:
                check={"lt":lambda x,y:x<y,"gte":lambda x,y:x>=y}
                if all(check[t["comparator"]](Decimal(v),Decimal(t["threshold"])) for t in row["triggers"]):matches.append(row["key"])
            self.assertEqual(matches,[expected])
        result=build_valuation(**data);self.assertEqual(result["status"],"available",result["reason"])

    def test_missing_valuation_cycles_overlap_and_noncompany_cash_are_rejected(self):
        mutations=[lambda d,a:a.pop("valuation_snapshot_ref"),lambda d,a:d["scenarios"][0]["triggers"][0].update(observed_by="event_judgement",metric_ref="THESIS_STRENGTHEN"),
            lambda d,a:d["scenarios"][1]["triggers"][0].update(threshold="4"),lambda d,a:d["scenarios"][0]["partition"].update(observed_by="company_fcf",metric_ref="free_cash_flow_system")]
        for mutation in mutations:
            data,anchors=contract();mutation(data,anchors)
            with self.assertRaises(ValueError):validate_new_scenario_rules(data["scenarios"],anchors,today=date(2026,1,1))

    def test_template_basis_owner_source_and_twelve_month_window_are_required(self):
        for mutation in (lambda s:s[0]["probability_basis"].update(kind="evidence_assessment"),
            lambda s:s[0]["triggers"][0].update(horizon="2027-01-02"),
            lambda s:s[0]["invalidators"].append({"observed_by":"owner","metric_ref":None,"horizon":"2027-01-01"})):
            data,anchors=contract();mutation(data["scenarios"])
            with self.assertRaises(ValueError):validate_new_scenario_rules(data["scenarios"],anchors,today=date(2026,1,1))
        data,anchors=contract();data["scenarios"][0]["invalidators"].append({"observed_by":"owner","metric_ref":None,"horizon":"2027-01-01",
            "owner_rule":{"source_ref":"claim:customer","as_of":"2026-01-01","rule":"Owner judges whether cancellation materially breaks demand evidence"}})
        validate_new_scenario_rules(data["scenarios"],anchors,today=date(2026,1,1))

if __name__=="__main__":unittest.main()
