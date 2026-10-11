"""Failure summaries retain real immutable bills and never buy recovery."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from tests import test_cockpit_model_fallback as fx
from tests import test_investment_memo_draft as memo
from dalton_core.cockpit_model import CockpitModelError, model_failure_trace, independent_model_call
from dalton_core.investment_memo_failure_cost import MemoFailureCostReporter, failure_cost, with_failure_costs
from dalton_core.investment_memo_repair_budget import MemoRepairBudget
from dalton_core.investment_memo_cli import run
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from dalton_core.planner_paid_output_recovery import _authority_connection


class AllFailureAdapter(fx.ChainAdapter):
    failure = None
    def execute(self, work, route, profile):
        if self.failure is not None:
            self.script[profile['id']] = self.failure
        return super().execute(work, route, profile)


class FailureCostTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fx.CockpitChainTests('runTest'); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.mission = self.fixture.root, self.fixture.mission
        self.adapter = AllFailureAdapter({})
        self.model = self.fixture._model(self.adapter, policy_version_ref=self.fixture.chain_policy)
        for purpose in ('investment_memo','investment_memo_verifier'):
            self.model.config['purpose_call_budgets'][purpose] = {'max_output_tokens':500,'max_cost_usd':0.5,'timeout_seconds':180}

    def kw(self, company='company:one', n=0, purpose='investment_memo'):
        return dict(purpose=purpose, request_id='memo-fee-'+company+'-'+str(n),
            prompt='Company: '+company+' group '+str(n), mission=self.mission)

    def fail(self, code='PROVIDER_BUDGET_EXCEEDED', kwargs=None):
        kwargs = kwargs or self.kw()
        self.adapter.failure = {'code':code,'message':'provider failed'}
        with self.assertRaises(CockpitModelError) as caught:
            self.model.call(**kwargs)
        return caught.exception, kwargs

    def ledger(self):
        with _authority_connection(self.root/'budget.sqlite') as c:
            return [tuple(r)for r in c.execute('SELECT settlement_id,record_json FROM thesis_impact_day_settlements ORDER BY rowid')]

    def test_two_companies_three_failure_types_read_original_settlements(self):
        for company, code in [('company:one','PROVIDER_BUDGET_EXCEEDED'),('company:two','TIMEOUT'),('company:three','PROVIDER_ERROR')]:
            exc, kw = self.fail(code,self.kw(company));before=self.ledger();calls=len(self.adapter.served)
            proof=failure_cost(self.model,exc,kw)
            self.assertEqual(proof['status'],'uncertain' if code=='TIMEOUT' else 'settled',proof)
            self.assertGreater(proof['settled_micros'],0)
            self.assertTrue(proof['fees'][0]['settlement_ref'])
            self.assertEqual(self.ledger(),before);self.assertEqual(len(self.adapter.served),calls)

    def test_unsettled_is_unknown_and_retains_reservation_not_zero_free(self):
        with patch('dalton_core.cockpit_model._settle_without_losing_the_lease', return_value=None):
            exc,kw=self.fail('TIMEOUT')
        report=MemoFailureCostReporter(self.model);report.failures['parent']=failure_cost(self.model,exc,kw)
        summary=with_failure_costs({'cost_micros':0},report)
        self.assertEqual(report.failures['parent']['status'],'unsettled',summary)
        self.assertIsNone(summary['cost_micros']);self.assertTrue(summary['unknown_failure_cost'])
        self.assertGreater(summary['unsettled_failure_reservation_micros'],0)

    def test_failure_replay_does_not_double_count_or_change_original_fee(self):
        report=MemoFailureCostReporter(self.model);kw=self.kw();self.adapter.failure={'code':'PROVIDER_ERROR','message':'failed'}
        with self.assertRaises(CockpitModelError):report.call(**kw)
        original = self.ledger(); calls = len(self.adapter.served)
        with self.assertRaises(CockpitModelError):report.call(**kw)
        self.assertEqual(self.ledger(), original); self.assertEqual(len(self.adapter.served), calls)
        self.assertEqual(len(report.failures),1)
        summary=with_failure_costs({'cost_micros':123},report)
        amount=next(iter(report.failures.values()))['settled_micros']
        self.assertEqual(summary['cost_micros'],123+amount)

    def test_foreign_request_mission_or_rehashed_trace_never_claims_a_fee(self):
        exc,kw=self.fail();trace=model_failure_trace(exc)
        for bad in [dict(kw,prompt='other company'),dict(kw,mission={**self.mission,'content_hash':'0'*64})]:
            self.assertEqual(failure_cost(self.model,exc,bad)['status'],'unproved')
        badexc=CockpitModelError(str(exc),failure_trace={**trace,'work_order_hash':'0'*64})
        self.assertEqual(failure_cost(self.model,badexc,kw)['status'],'unproved')

    def test_verifier_wrapper_preserves_independence_routes(self):
        seen={}
        class Model:
            def call(self, *, producer_route_decision_refs, **kwargs):
                seen['routes']=producer_route_decision_refs;return {'text':'pass'}
        report=MemoFailureCostReporter(Model())
        independent_model_call(report,producer_route_decision_refs=['route:producer'],**self.kw(purpose='investment_memo_verifier'))
        self.assertEqual(seen['routes'],('route:producer',))

    def test_actual_cli_summary_adds_failed_bill_without_resetting_macro_reserve(self):
        from dalton_core import investment_memo_cli
        frozen={'status':'ready','mission':self.mission,'playbook':{'id':'pb','content_hash':'b'*64,
            'deliverable_templates':{'investment_memo':memo.TITLES},'key_questions':memo.QUESTIONS},
            'company':{'company_ref':'company:one'},'material':[],'input_bindings':[]}
        p=self.root/'producer.json';q=self.root/'verifier.json';p.write_text('{}');q.write_text('{}')
        self.adapter.failure={'code':'PROVIDER_BUDGET_EXCEEDED','message':'provider max_output_tokens telemetry exceeds WorkOrder budget'}
        with patch.object(investment_memo_cli,'DaltonStore',return_value=SimpleNamespace()),patch.object(investment_memo_cli,'collect_frozen_input',return_value=frozen):
            summary=run(state_dir=self.root,model_config_path=p,verifier_model_config_path=q,model_factory=lambda:self.model,verifier_model_factory=lambda:None)
        self.assertEqual(summary['memo_status'],'draft_refused',summary)
        self.assertGreater(summary['cost_micros'],0,summary)
        self.assertEqual(summary['cost_micros'],summary['settled_failure_cost_micros'])
        self.assertEqual(summary['budget']['repair_accounted_micros'],14_000_000)
        self.assertGreater(len(self.adapter.served),0)

    def test_unbound_exception_does_not_misreport_zero_as_known(self):
        class Model:
            def call(self,**kwargs):raise CockpitModelError('unknown failure')
        report=MemoFailureCostReporter(Model())
        with self.assertRaises(CockpitModelError):report.call(**self.kw())
        summary=with_failure_costs({'cost_micros':0},report)
        self.assertIsNone(summary['cost_micros']);self.assertTrue(summary['unknown_failure_cost'])

    def test_successful_group_replay_and_later_failure_keep_separate_totals(self):
        report=MemoFailureCostReporter(self.model)
        first=report.call(**self.kw(n=0)); calls=len(self.adapter.served)
        replay=report.call(**self.kw(n=0)); self.assertEqual(len(self.adapter.served),calls)
        self.assertTrue(replay['replayed'])
        self.adapter.failure={'code':'PROVIDER_ERROR','message':'failed next group'}
        with self.assertRaises(CockpitModelError):report.call(**self.kw(n=1))
        settled=next(iter(report.failures.values()))['settled_micros']
        summary=with_failure_costs({'cost_micros':first['cost_micros']},report)
        self.assertEqual(summary['cost_micros'],first['cost_micros']+settled)
        self.assertEqual(len(report.failures),1)

    def test_earlier_unproved_boundary_is_not_overwritten_as_settled(self):
        exc,kw=self.fail('PROVIDER_ERROR');report=MemoFailureCostReporter(self.model)
        report.failures['work']=failure_cost(self.model,exc,kw)
        summary=with_failure_costs({'cost_micros':0,'cost_status':'unproved'},report)
        self.assertEqual(summary['cost_status'],'partially_known')
        self.assertTrue(summary['unknown_failure_cost'])
