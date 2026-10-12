import copy,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from tests import test_initial_screen_failure_feedback as f
from dalton_core import initial_screen_failed_input as guard,initial_screen_cli as cli
from dalton_core.store import content_hash

class FailedInputTests(unittest.TestCase):
    def setUp(self):
        self.f=f.InitialScreenFailureFeedbackTests('runTest');self.f.setUp();self.addCleanup(self.f.doCleanups)
    def binding(self,pack=None,sources=None,**kwargs):
        return guard.input_binding(self.f.mission,self.f.company,pack or self.f.pack,None,
            sources or {'passage':{'document_sha256':'1'*64,'start':0,'end':10}},**kwargs)
    def test_time_market_quote_and_global_counter_cannot_open_failed_input(self):
        p=copy.deepcopy(self.f.pack);p['json']['as_of']='2026-10-11';p['json']['numbers']={'market':{'price':80},'valuation':{'pe':20}}
        one=self.binding(p);guard.record_failure(self.f.root,{'status':'failed','_failed_input_binding':one,'validation_feedback':{'status':'unverified'}})
        p['json']['as_of']='2026-10-13';p['json']['numbers']['market']['price']=95;p['json']['numbers']['valuation']['pe']=30
        two=self.binding(p);self.assertEqual(one['content_hash'],two['content_hash']);self.assertIsNotNone(guard.unchanged_failure(self.f.root,two))
    def test_source_model_definition_rule_or_explicit_authorization_changes_identity(self):
        original=self.binding()
        self.assertNotEqual(original['content_hash'],self.binding(sources={'passage':{'document_sha256':'2'*64,'start':0,'end':10}})['content_hash'])
        p=copy.deepcopy(self.f.pack);p['json']['our_model']={'version_ref':'forecast:2','definition_ref':'gaap'}
        self.assertNotEqual(original['content_hash'],self.binding(p)['content_hash'])
        self.assertNotEqual(original['content_hash'],self.binding(reopen={'id':'reopen:approved','content_hash':'3'*64})['content_hash'])
        with mock.patch.object(guard,'PRODUCT_VALIDATION_CONTRACT','product-validation:2'):
            self.assertNotEqual(original['content_hash'],self.binding()['content_hash'])
    def test_success_and_quiesce_do_not_mark_failure_or_mutate_prior_failed_guard(self):
        key=self.binding();guard.record_failure(self.f.root,{'status':'quiesced','_failed_input_binding':key})
        self.assertIsNone(guard.unchanged_failure(self.f.root,key))
        guard.record_failure(self.f.root,{'status':'failed','_failed_input_binding':key,'failure_reason':'known failure'})
        path=guard._path(self.f.root,self.f.company['company_ref']);before=path.read_bytes()
        guard.record_failure(self.f.root,{'status':'succeeded','_failed_input_binding':key})
        self.assertEqual(before,path.read_bytes())
    def test_actual_cli_same_failed_pack_never_calls_model_again(self):
        args=SimpleNamespace(dry_run=False,as_of=None,summary_dir=str(self.f.directory),timeout_seconds=180)
        def run():
            return cli._run_strl(args,state=self.f.root,store=self.f.store,mission=self.f.mission,playbook={},
                entry=self.f.company,summary={'drafted':self.f.company},deliverable_authority=self.f.authority,missions=None,claims={},model=self.f.model)
        with mock.patch.object(cli,'check_sufficiency',return_value={'drafting_allowed':True,'status':'sufficient'}),mock.patch.object(cli,'build_pack',return_value=self.f.pack),mock.patch.object(cli,'record_pack_build'):
            first=run();calls=len(self.f.adapter.served);second=run()
        self.assertEqual(first['status'],'failed');self.assertEqual(second['status'],'held')
        self.assertEqual(second['reason_code'],'initial_screen_unchanged_failure');self.assertEqual(len(self.f.adapter.served),calls)
        self.assertEqual(first['validation_feedback']['status'],'recorded')
    def test_unverified_failure_also_cannot_authorize_a_new_paid_attempt(self):
        key=self.binding();guard.record_failure(self.f.root,{'status':'failed','_failed_input_binding':key,'validation_feedback':{'status':'unverified'}})
        self.assertEqual(guard.unchanged_failure(self.f.root,key)['failure_feedback']['status'],'unverified')
        path=guard._path(self.f.root,self.f.company['company_ref']);v=__import__('json').loads(path.read_text());v['inputs'][0]['input_hash']='fake';path.write_text(__import__('json').dumps(v))
        with self.assertRaises(ValueError):guard.unchanged_failure(self.f.root,key)

    def test_nested_number_pack_clocks_hashes_prices_and_tag_order_are_not_research(self):
        p=copy.deepcopy(self.f.pack);p['json']['numbers']={'as_of':'2026-10-11','generated_at':'2026-10-11T01:00Z',
            'consensus':{'generated_at':'first','periods':[{'period':'FY2027','eps':'4.2'}]},'market':{'price':80},'peers':{'other_company':100}}
        p['json']['numbers_hash']=content_hash(p['json']['numbers'])
        one=self.binding(p)
        p['json']['numbers']['generated_at']='2026-10-11T02:00Z';p['json']['numbers']['consensus']['generated_at']='second'
        p['json']['numbers']['market']['price']=100;p['json']['numbers']['peers']['other_company']=200
        p['json']['numbers_hash']=content_hash(p['json']['numbers'])
        self.assertEqual(one['content_hash'],self.binding(p)['content_hash'])
        p['json']['numbers']['consensus']['periods'][0]['eps']='4.3'
        self.assertNotEqual(one['content_hash'],self.binding(p)['content_hash'])

    def test_actual_purpose_policy_and_budget_changes_open_but_other_purpose_does_not(self):
        before=guard.execution_binding(self.f.model,self.f.root)
        config=self.f.model.config
        other=copy.deepcopy(config['purpose_call_budgets']);other['investment_memo']={'max_cost_usd':99,'max_output_tokens':128000}
        config['purpose_call_budgets']=other
        self.assertEqual(before,guard.execution_binding(self.f.model,self.f.root))
        config['purpose_call_budgets']['initial_screen']['max_cost_usd']=15
        changed=guard.execution_binding(self.f.model,self.f.root)
        self.assertNotEqual(before,changed)
        self.assertNotEqual(self.binding(execution=before)['content_hash'],self.binding(execution=changed)['content_hash'])
        with mock.patch('dalton_core.model_router.ModelRouter.get_policy',return_value={'purpose_overrides':{'initial_screen':['profile:owner-new']}}):
            self.assertNotEqual(changed,guard.execution_binding(self.f.model,self.f.root))

    def test_full_guard_refuses_a_new_input_before_any_paid_dispatch(self):
        for i in range(guard.MAX_INPUTS):
            key=self.binding(execution={'owner_epoch':i})
            guard.record_failure(self.f.root,{'status':'failed','_failed_input_binding':key})
        before=len(self.f.adapter.served)
        with self.assertRaisesRegex(ValueError,'full'):
            guard.unchanged_failure(self.f.root,self.binding(execution={'owner_epoch':'new'}))
        self.assertEqual(len(self.f.adapter.served),before)
        self.assertIsNotNone(guard.unchanged_failure(self.f.root,self.binding(execution={'owner_epoch':0})))
