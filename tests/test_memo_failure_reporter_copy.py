"""Actual memo second correction crosses the real reporting wrapper copy seam."""
import copy
import pickle
import unittest
from unittest.mock import patch
from dalton_core.investment_memo_failure_cost import MemoFailureCostReporter
from dalton_core.cockpit_model import CockpitModelError

class PickleDelegate:
    value='delegated'

class ReporterCopyTests(unittest.TestCase):
    def fixture(self,company="company:synthetic-a"):
        from tests.test_investment_memo_second_maintenance import MemoSecondMaintenanceTests
        harness=MemoSecondMaintenanceTests('runTest');self.addCleanup(harness.doCleanups)
        f=harness.fixture(company=company);wrapper,first,bad=harness.first_failed(f);harness.set_policy(f,2)
        return harness,f,first,bad

    def test_empty_copy_pickle_protocol_probe_is_attribute_error_not_recursion(self):
        empty=object.__new__(MemoFailureCostReporter)
        for name in ('model','__setstate__','__deepcopy__','anything'):
            with self.assertRaises(AttributeError):getattr(empty,name)
        reporter=MemoFailureCostReporter(PickleDelegate());reporter.failures['old']={'unknown':True}
        cloned=copy.copy(reporter);self.assertIs(cloned.model,reporter.model);self.assertIs(cloned.failures,reporter.failures)
        unpickled=pickle.loads(pickle.dumps(reporter));self.assertEqual(unpickled.value,'delegated');self.assertEqual(unpickled.failures,reporter.failures)

    def test_real_second_call_with_reporter_retains_parent_bills_and_restart_replays(self):
        for company in ('one','two'):
            with self.subTest(company=company):
                harness,f,first,bad=self.fixture(company="company:synthetic-"+company);reporter=MemoFailureCostReporter(f.model);f.model=reporter
                old=f.fees();paid=len(f.adapter.served);delegate=reporter.model;prior=getattr(delegate,'_run_cost_admission_guard',None)
                wrapper=f.wrapper(run=112000000);body,result=f.purchase(wrapper,bad)
                self.assertEqual(result['status'],'drafted');self.assertTrue(body['permission_ref'].endswith(':round2'))
                self.assertEqual(len(f.adapter.served),paid+1);self.assertIs(getattr(delegate,'_run_cost_admission_guard',None),prior)
                new=f.fees();self.assertEqual(new[0][:len(old[0])],old[0]);self.assertEqual(new[1][:len(old[1])],old[1])
                again=f.outcome(f.wrapper(run=112000000),1);self.assertEqual(again['status'],'drafted')
                self.assertEqual(f.fees(),new);self.assertEqual(len(f.adapter.served),paid+1)

    def test_second_provider_failure_survives_copy_and_keeps_unknown_fee_report(self):
        harness,f,first,bad=self.fixture();reporter=MemoFailureCostReporter(f.model);f.model=reporter
        wrapper=f.wrapper(run=112000000);body=wrapper.prepare_maintenance(context=f.contexts[1],outcome=bad,remaining_groups=2)
        f.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'unknown'}}
        wrapper.begin_stage(max_units=4,allow_technical=False,maintenance=body)
        with self.assertRaises(CockpitModelError):wrapper.call(purpose='investment_memo',request_id=body['entry']['request_id'],prompt=body['prompt'],mission=f.mission)
        self.assertEqual(len(reporter.failures),1)
        bill=next(iter(reporter.failures.values()));self.assertGreater(bill['settled_micros'],0)
        self.assertIn(bill['status'],('uncertain','settled'))
        paid=len(f.adapter.served);f.adapter.script={}
        self.assertIsNone(f.wrapper(run=168000000).prepare_maintenance(context=f.contexts[1],outcome=bad,remaining_groups=2))
        self.assertEqual(len(f.adapter.served),paid)

    def test_contract_wakes_once_without_changing_frozen_input(self):
        from dalton_core.mission_investment_memo_lane import company_signature
        from types import SimpleNamespace
        import tempfile,json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config';path.write_text(json.dumps({}))
            launcher=SimpleNamespace(model_config_path=path,verifier_model_config_path=path)
            frozen={'mission':{'id':'m','content_hash':'mhash'},'playbook':{'id':'p','content_hash':'phash'},'company':{'company_ref':'issuer'},'input_bindings':[]}
            now=company_signature(frozen,launcher);self.assertEqual(company_signature(frozen,launcher),now)
            with patch('dalton_core.investment_memo_failure_cost.COPY_PROTOCOL_CONTRACT','legacy'):
                self.assertNotEqual(company_signature(frozen,launcher),now)
            self.assertEqual(frozen['input_bindings'],[])
