"""Conviction's numerical admission uses real model/consensus authorities."""
import copy
import unittest
from datetime import date
from dalton_core.consensus_estimate import ConsensusEstimateAuthority
from dalton_core.conviction_call_cli import comparable_consensus_metrics
from dalton_core.model_forecast_driver import ForecastModelAuthority
from dalton_core.store import DaltonStore
from tests.test_consensus_estimate import wire,INVOCATION,ARTIFACT,GOVERNANCE,GOVERNANCE_HASH
from tests.test_forecast_financial_execution import economic_model


class SourceAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.store=DaltonStore(":memory:");self.addCleanup(self.store.close)
        body,_=economic_model();self.model=ForecastModelAuthority(self.store).publish(body)
        observed=wire();observed["as_of"]="2026-01-10"
        self.consensus=ConsensusEstimateAuthority(self.store).publish_consensus(company_ref="company:test",wire=observed,
            fiscal_year_end="12-31",last_reported_period_end="2025-12-31",invocation_ref=INVOCATION,artifact_hash=ARTIFACT,
            governance_ref=GOVERNANCE,governance_hash=GOVERNANCE_HASH,captured_at="2026-01-10T00:00:00+00:00")
        self.item={"metric":"revenue","period":"FY2026Q1","ours":"1","consensus":"2","unit":"USD",
            "gap_percent":"-50","refs":[self.model["id"],self.consensus["id"]]}

    def check(self,item=None,today=date(2026,1,11)):
        return comparable_consensus_metrics(self.store,"company:test",[item or self.item],today=today)

    def test_current_authority_bound_revenue_is_admitted(self):
        values,reason=self.check()
        self.assertEqual(values,[self.item]);self.assertIsNone(reason)

    def test_stale_future_unbound_and_wrong_units_are_not_admitted(self):
        for day in (date(2026,3,1),date(2026,1,1)):
            self.assertEqual(self.check(today=day)[0],[])
        for patch in ({"refs":[self.consensus["id"]]},{"unit":"USDm"},{"metric":"EPS","unit":"usd_per_share"}):
            with self.subTest(patch=patch):
                self.assertEqual(self.check({**self.item,**patch})[0],[])
