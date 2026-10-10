import unittest
from dalton_core.house_view import build_house_view,stage_note

class HouseViewTests(unittest.TestCase):
    def test_active_house_sources_changes_and_preview_are_exposed(self):
        theses=[{"id":"thesis-version:1","thesis_ref":"thesis:test:demand","version":1,"company_ref":"company:test","stance":"house","view_status":"active",
            "summary":"Demand converts to profitable growth","created_at":"2026-01-01","claim_refs":["claim:1","claim:2","claim:3"],
            "falsification_conditions":[{"metric_ref":"margin","threshold":".1","deadline":"2027-01-01","evidence_refs":["claim:2"]}]}]
        claims=[{"ref":"claim:"+str(i),"statement":"Sourced reason "+str(i)} for i in range(1,4)]
        value=build_house_view(company_ref="company:test",theses=theses,claims=claims,
            judgements=[{"decision":"NO_CHANGE"},{"decision":"NO_CHANGE"}],scenario_valuation={"status":"unavailable","reason":"EPS missing"},
            catalyst={"kind":"earnings","date":"2026-03-01","key_observations":[{"text":"Check conversion","refs":["preview:1"]}],"observations_status":"available"},today="2026-01-01")
        self.assertEqual(len(value["reasons"]),3);self.assertEqual(value["unchanged_updates"],2)
        self.assertIn("2条",value["change_label"]);self.assertEqual(value["risk_reward_missing_reason"],"EPS missing")
        self.assertEqual(value["next_catalyst"]["what_to_watch"][0]["refs"],["preview:1"])

    def test_counter_broken_and_legacy_unknown_never_become_house_statement(self):
        for stance,status in [("counter","active"),("house","broken"),(None,None)]:
            value=build_house_view(company_ref="company:test",theses=[{"id":"t","thesis_ref":"thesis:test:demand","company_ref":"company:test","version":1,
                "stance":stance,"view_status":status,"statement":"Wrong house"}],claims=[],judgements=[],scenario_valuation=None,catalyst=None,today="2026-01-01")
            self.assertIsNone(value["statement"]);self.assertEqual(value["reasons"],[])
        self.assertEqual(stage_note(stage_ref="company_model",deliverable={"id":"screen"},model_coverage={"status":"blocked","label":"预测缺驱动依据"},current_note="资料底座齐了，等着起草初步研究筛查报告"),"预测缺驱动依据")

if __name__=="__main__":unittest.main()
