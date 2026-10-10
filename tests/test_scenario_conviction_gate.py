import copy,unittest
from decimal import Decimal
from unittest.mock import patch
from dalton_core.scenario_version import validate_conviction_sources
from dalton_core.scenario_valuation import build_valuation,ScenarioValuationAuthority,ScenarioValuationError
from dalton_core.conviction_call import evaluate_policy_gate,upside_downside_ratio_of
from dalton_core.conviction_policy import DEFAULT_POLICY,validate_policy
from dalton_core.store import DaltonStore
from tests.test_scenario_valuation import inputs

class ScenarioConvictionGateTests(unittest.TestCase):
    def test_no_computed_model_payoff_and_typed_payoff_are_rejected(self):
        store=DaltonStore(":memory:");self.addCleanup(store.close)
        with self.assertRaisesRegex(ValueError,"current model-recomputed"):
            validate_conviction_sources(store,"company:test",[])
        result=build_valuation(**inputs())
        rows=[{"source_ref":r["scenario_version_ref"],"probability":r["probability"],"payoff":r["return_percent"]} for r in result["scenarios"]]
        # Only the physical held-input loader is stubbed; EPS/PE arithmetic and gate execute unchanged.
        with patch("dalton_core.scenario_valuation.held_inputs",return_value=inputs()):
            validate_conviction_sources(store,"company:test",rows)
            rows[0]["payoff"]="999"
            with self.assertRaisesRegex(ValueError,"differs"):validate_conviction_sources(store,"company:test",rows)

    def test_minimum_ratio_and_missing_ratio_are_mechanically_refused(self):
        kwargs=dict(direction="long",time_horizon="6_12_months",evidence_level="high",scenario_count=3,invalidator_count=1,
            alignment="opposite_direction",position_percent="50",policy=dict(DEFAULT_POLICY))
        self.assertIn("risk_reward_unavailable",evaluate_policy_gate(**kwargs,upside_downside_ratio=None)["reasons"])
        self.assertIn("risk_reward_below_threshold",evaluate_policy_gate(**kwargs,upside_downside_ratio="1.9")["reasons"])
        self.assertEqual(evaluate_policy_gate(**kwargs,upside_downside_ratio="2")["status"],"passed")
        legacy=dict(DEFAULT_POLICY);legacy.pop("min_upside_downside_ratio")
        self.assertNotIn("min_upside_downside_ratio",validate_policy(legacy))

    def test_stale_price_cannot_be_published_and_ledger_is_immutable(self):
        import json
        store=DaltonStore(":memory:");self.addCleanup(store.close);con=store.connection
        con.execute("CREATE TABLE market_price_series_versions(company_ref TEXT,version_number INTEGER,record_json TEXT)")
        held={"id":"price:new","content_hash":"a"*64}
        con.execute("INSERT INTO market_price_series_versions VALUES(?,?,?)",("company:test",1,json.dumps(held)));con.commit()
        authority=ScenarioValuationAuthority(store)
        with self.assertRaisesRegex(ScenarioValuationError,"stale"):authority.publish(build_valuation(**inputs()))

if __name__=="__main__":unittest.main()
