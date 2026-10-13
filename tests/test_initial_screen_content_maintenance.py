"""Fixed local correction uses real Scheduler/formal/fee admissions offline."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest import mock
from dalton_core import initial_screen_content_maintenance as maintenance
from dalton_core import initial_screen_validator_recovery as recovery
from dalton_core import initial_screen_failure_feedback as feedback
from dalton_core import initial_screen_cli as cli
from dalton_core.initial_screen import build_document_prompt
from dalton_core.store import canonical_json,content_hash
from tests import test_initial_screen_validator_recovery as fixtures
from tests.synthetic_company import strl_document,strl_worksheet,material_pack


class ContentMaintenanceTests(unittest.TestCase):
    def fixture(self,company='synw'):
        from tests import test_initial_screen_failure_feedback as original
        from tests import p9a_fixtures
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
        original_params=p9a_fixtures.mission_params;register=ThesisImpactBudgetStore.register_policy
        def params(*args,**kwargs):
            value=original_params(*args,**kwargs);value['budget']['max_daily_cost_usd']=100.0;return value
        def policy(store,**kwargs):
            kwargs['day_cap_micros']=100000000;return register(store,**kwargs)
        with mock.patch.object(p9a_fixtures,'mission_params',side_effect=params),mock.patch.object(ThesisImpactBudgetStore,'register_policy',policy),mock.patch.object(original,'COMPANY','company:ticker:'+company),mock.patch.object(original,'material_pack',side_effect=lambda:material_pack(company='company:ticker:'+company)):
            f=fixtures.ValidatorRecoveryTests('runTest');f.setUp()
        self.addCleanup(f.doCleanups)
        raw=strl_document()
        raw['sections'][0]['claims']=['C999']
        f.f.adapter.value=raw
        prompt=build_document_prompt(company=f.f.company,pack=f.f.pack,worksheet=strl_worksheet(),repair=[{'code':'unknown_tag','detail':'C999'}])
        # The original child already exited in the inherited fixture. Its
        # frozen binding remains, but only a real original model call seeds
        # the settled doc1; do not invent a live checkpoint ticket.
        f.model.call(purpose='initial_screen',request_id=f.base+':document:1',prompt=feedback.validation_prompt(prompt,f.context),mission=f.f.mission)
        f.f.adapter.value=strl_document()
        return f

    def test_two_companies_one_fixed_repair_remaining_verifier_and_restart_free(self):
        for company in ('synw','synz'):
            with self.subTest(company=company):
                f=self.fixture(company);result=f.qualify();before=len(f.f.adapter.served)
                corrected=maintenance.run(result,model=f.model,state=f.state,mission=f.f.mission,authority=f.f.authority)
                self.assertEqual(corrected['record']['findings'],[])
                self.assertEqual(len(f.f.adapter.served),before+1)
                f.f.adapter.value={'verdict':'pass','findings':[]}
                verdict,call=recovery.verifier(corrected,model=f.model,state=f.state,mission=f.f.mission)
                self.assertEqual(verdict['verdict'],'pass')
                restarted=maintenance.run(f.qualify(),model=f.model,state=f.state,mission=f.f.mission,authority=f.f.authority)
                verdict,replayed=recovery.verifier(restarted,model=f.model,state=f.state,mission=f.f.mission)
                self.assertEqual(call['work_order_ref'],replayed['work_order_ref']);self.assertEqual(len(f.f.adapter.served),before+2)
                from dalton_core.planner_paid_output_recovery import _authority_connection
                with _authority_connection(f.model.scheduler_db) as c:
                    self.assertEqual(c.execute(f'SELECT count(*) FROM {maintenance.TABLE}').fetchone()[0],1)
                    self.assertEqual(c.execute(f'SELECT count(*) FROM {maintenance.CLAIMS}').fetchone()[0],1)

    def test_whole_body_guard_rejects_other_fields_formula_unit_period_and_valid_citation_deletion(self):
        f=self.fixture();result=f.qualify();raw=result['slots']['document:1'][1]
        allowed=maintenance.plan(raw,result['record']['findings'],f.f.pack)
        corrected=strl_document();maintenance.assert_changes(raw,corrected,allowed,f.f.pack)
        mutations=(lambda v:v['sections'][0].update(body='立场：看空。'),
            lambda v:v['sections'][1]['numbers'][0].update(unit='EUR'),
            lambda v:v['sections'][0]['numbers'][-1]['derivation'].update(result='99'),
            lambda v:v['sections'][0]['numbers'][0].update(period='FY2030'),
            lambda v:v['sections'][1].update(sources=[]),
            lambda v:v['sections'][0].update(claims=['C1000']),
            lambda v:v.update(extra='unbound'))
        for mutate in mutations:
            bad=copy.deepcopy(corrected);mutate(bad)
            with self.subTest(bad=bad['sections'][0].get('body')):
                with self.assertRaises(ValueError):maintenance.assert_changes(raw,bad,allowed,f.f.pack)

    def test_current_cli_guard_reuses_prefix_then_only_maintenance_and_original_verifier(self):
        f=self.fixture();before=len(f.f.adapter.served)
        original=f.f.adapter.execute
        def answer(work,route,profile):
            f.f.adapter.value=({'verdict':'pass','findings':[]} if work.metadata['purpose']=='initial_screen_verifier' else strl_document())
            return original(work,route,profile)
        f.f.adapter.execute=answer
        args=SimpleNamespace(dry_run=False,as_of=None,summary_dir=str(f.q.path),timeout_seconds=180)
        with mock.patch.object(cli,'check_sufficiency',return_value={'drafting_allowed':True,'status':'sufficient'}),mock.patch.object(cli,'build_pack',return_value=f.f.pack),mock.patch.object(cli,'record_pack_build'),mock.patch.object(cli,'_publish_strl_revalidated',return_value={'status':'succeeded'}) as publish:
            actual=cli._run_strl(args,state=f.state,store=f.f.store,mission=f.f.mission,playbook={},entry=f.f.company,summary={'drafted':f.f.company},deliverable_authority=f.f.authority,missions=None,claims={},model=f.model)
        self.assertEqual(actual['status'],'succeeded');publish.assert_called_once();self.assertEqual(len(f.f.adapter.served),before+2)
        self.assertEqual(len(publish.call_args.kwargs['sections']),9)

    def test_parent_context_or_unknown_fee_cannot_issue_permission(self):
        f=self.fixture();before=len(f.f.adapter.served)
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
        original=ThesisImpactBudgetStore.admission
        def pending(store,**kwargs):
            row=copy.deepcopy(original(store,**kwargs));row['settlement']=None;return row
        with mock.patch.object(ThesisImpactBudgetStore,'admission',pending):
            with self.assertRaisesRegex(ValueError,'unsettled'):f.qualify()
        result=f.qualify();path=f.q.path/'frozen-context.json';value=feedback._read(path)
        value['context']['source_bindings']={'foreign':{'sha256':'a'*64}}
        value['scope']['context_hash']=content_hash(value['context']);value['content_hash']=content_hash({k:v for k,v in value.items() if k!='content_hash'})
        feedback._write(path,value)
        with self.assertRaises(ValueError):maintenance.run(result,model=f.model,state=f.state,mission=f.f.mission,authority=f.f.authority)
        self.assertEqual(len(f.f.adapter.served),before)

    def test_run_budget_and_quiet_refuse_before_issue_without_removing_paid_fees(self):
        f=self.fixture();result=f.qualify();before=len(f.f.adapter.served)
        f.model.config['purpose_run_budgets']={'initial_screen':{'max_cost_usd':0.01}}
        with self.assertRaisesRegex(ValueError,'reserve'):maintenance.run(result,model=f.model,state=f.state,mission=f.f.mission,authority=f.f.authority)
        f.model.config['purpose_run_budgets']['initial_screen']['max_cost_usd']=112
        feedback._write(f.q.path/'quiesce-request.json',{})
        with self.assertRaisesRegex(ValueError,'stop requested'):maintenance.run(result,model=f.model,state=f.state,mission=f.f.mission,authority=f.f.authority,summary_dir=f.q.path)
        self.assertEqual(len(f.f.adapter.served),before)
        self.assertEqual(result['record']['paid_prefix'],f.qualify()['record']['paid_prefix'])

    def test_paid_unknown_or_failed_maintenance_remains_consumed_after_new_budget_validator_and_ticket(self):
        f=self.fixture();result=f.qualify();before=len(f.f.adapter.served)
        from dalton_core.model_router import ModelRouter
        with ModelRouter(f.f.router_db,read_only=True) as r:
            profiles=r.connection.execute('SELECT profile_json FROM model_endpoint_profile_versions').fetchall()
        f.f.adapter.script={json.loads(row[0])['id']:{'code':'TIMEOUT','message':'paid unknown'} for row in profiles}
        from dalton_core.cockpit_model import CockpitModelError
        with self.assertRaises(CockpitModelError):maintenance.run(result,model=f.model,state=f.state,mission=f.f.mission,authority=f.f.authority)
        self.assertEqual(len(f.f.adapter.served),before+1)
        f.model.config['purpose_run_budgets']={'initial_screen':{'max_cost_usd':224}}
        with mock.patch.object(recovery,'validator_fingerprint',return_value='future'):
            with self.assertRaises(ValueError):maintenance.run(f.qualify(),model=f.model,state=f.state,mission=f.f.mission,authority=f.f.authority)
        self.assertEqual(len(f.f.adapter.served),before+1)

    def test_permission_marker_cannot_be_called_without_live_authority(self):
        f=self.fixture();before=len(f.f.adapter.served)
        from dalton_core.cockpit_model import CockpitModelError
        with self.assertRaisesRegex(CockpitModelError,'live owner authority'):
            f.model.call(purpose='initial_screen',request_id=f.f.mission['id']+maintenance.MARKER+'a'*64,prompt='invented grant',mission=f.f.mission)
        self.assertEqual(len(f.f.adapter.served),before)

    def test_finite_rules_refuse_ambiguous_arithmetic_or_real_economic_missing(self):
        f=self.fixture();result=f.qualify();raw=result['slots']['document:1'][1]
        for code in ('forecast_eps_basis_spread_in_document','worksheet_shape','presence','quote_not_in_passage'):
            with self.assertRaises(ValueError):maintenance.plan(raw,[{'code':code,'section':'结论与观点'}],f.f.pack)
        with self.assertRaises(ValueError):maintenance.plan(raw,[{'code':'citation_or_contract','section':'结论与观点','detail':'unbound operand 999'}],f.f.pack)

    def test_bound_display_gap_request_and_added_citation_preserve_original_values(self):
        f=self.fixture();raw=strl_document();s=raw['sections'][0];n=s['numbers'][-1]
        n['text']='与一致预期的差异';s['body']+=' 材料缺少。';ref='material-request:'+'a'*32
        appendix=raw['sections'][-1];appendix['gap_items']=[{'kind':'not_acquired','request_ref':ref}]
        findings=[{'code':'citation_or_contract','section':'结论与观点','detail':"a derived number entry's text must state its result 11.1"},
            {'code':'material_missing_over_limit'}, {'code':'appendix_request_not_cited','section':'附录：我们不知道什么','detail':ref},
            {'code':'citation_or_contract','section':'结论与观点','detail':"section「结论与观点」carries figures with no source behind them: ['80.00']；写 缺来源 而不是猜一个数字"}]
        allowed=maintenance.plan(raw,findings,f.f.pack);fixed=copy.deepcopy(raw)
        fixed['sections'][0]['numbers'][-1]['text']+='（精确结果：11.1）'
        fixed['sections'][0]['numbers'].append({'tag':'N5','figure':'80.00'})
        fixed['sections'][0]['body']=fixed['sections'][0]['body'].replace('材料缺少','待补资料')
        fixed['sections'][-1]['body']+='\n未取得资料请求：'+ref
        maintenance.assert_changes(raw,fixed,allowed,f.f.pack)
        fixed['sections'][0]['numbers'][-2]['derivation']['expression']='1+10.1'
        with self.assertRaises(ValueError):maintenance.assert_changes(raw,fixed,allowed,f.f.pack)

    def test_genuine_unsourced_value_cannot_be_removed_or_invented_to_pass(self):
        f=self.fixture();raw=strl_document();raw['sections'][2]['body']+=' unsupported 987654321.'
        findings=[{'code':'citation_or_contract','section':'行业结构与竞争','detail':"section「行业结构与竞争」carries figures with no source behind them: ['987654321']；写 缺来源 而不是猜一个数字"}]
        allowed=maintenance.plan(raw,findings,f.f.pack)
        removed=copy.deepcopy(raw);removed['sections'][2]['body']=strl_document()['sections'][2]['body']
        with self.assertRaises(ValueError):maintenance.assert_changes(raw,removed,allowed,f.f.pack)
        invented=copy.deepcopy(raw);invented['sections'][2]['numbers'].append({'tag':'P999','quote':'987654321'})
        with self.assertRaises(ValueError):maintenance.assert_changes(raw,invented,allowed,f.f.pack)

    def test_exact_derived_named_phrase_cannot_edit_equal_value_in_other_measure_or_ambiguous_period(self):
        f=self.fixture();raw=strl_document();s=raw['sections'][0]
        n=s['numbers'][-1];n['derivation']={'expression':'-0.004153*100','result':'-0.4153'}
        n['text']='自由现金流收益率为 -0.4%';s['body']+=' 自由现金流收益率为 -0.4%；其它经营指标为 -0.4%。'
        findings=[{'code':'citation_or_contract','section':'结论与观点','detail':"a derived number entry's text must state its result -0.4153"}]
        allowed=maintenance.plan(raw,findings,f.f.pack);fixed=copy.deepcopy(raw)
        fixed['sections'][0]['numbers'][-1]['text']='自由现金流收益率为 -0.4153%'
        fixed['sections'][0]['body']=fixed['sections'][0]['body'].replace('自由现金流收益率为 -0.4%','自由现金流收益率为 -0.4153%')
        maintenance.assert_changes(raw,fixed,allowed,f.f.pack)
        fixed['sections'][0]['body']=fixed['sections'][0]['body'].replace('其它经营指标为 -0.4%','其它经营指标为 -0.4153%')
        with self.assertRaises(ValueError):maintenance.assert_changes(raw,fixed,allowed,f.f.pack)
        raw['sections'][0]['body']+=' 另一历史期间自由现金流收益率为 -0.4%。'
        with self.assertRaisesRegex(ValueError,'ambiguous prose'):maintenance.plan(raw,findings,f.f.pack)

    def test_new_date_requires_same_exact_consensus_parent_never_unrelated_reference(self):
        f=self.fixture();raw=strl_document();s=raw['sections'][0]
        ref='pack-json:/numbers/consensus/periods/1/revenue/mean'
        # A minimal closed numeric parent for this pure metadata projection;
        # full current source/formal readers are exercised by the real fixture.
        entry={'kind':'number','source_kind':'consensus','ref':ref,'pointer':ref.split(':',1)[1],'period':'FY2027'}
        pack={'company_ref':'company:ticker:synw','json':{'numbers':{'consensus':{'company_ref':'company:ticker:synw','version':{'ref':'consensus-estimate-version:'+'a'*32},'periods':[{}, {'label':'FY2027','status':'available','revenue':{'period_end':'2027-12-31','currency':'USD'}}]}}}}
        self.assertEqual(maintenance._period_end(pack,entry),'2027-12-31')
        for mutate in (lambda p:p['json']['numbers']['consensus'].update(company_ref='company:ticker:other'),
            lambda p:p['json']['numbers']['consensus']['periods'][1].update(label='FY2028'),
            lambda p:p['json']['numbers']['consensus']['periods'][1]['revenue'].update(period_end='2027-02-30')):
            changed=copy.deepcopy(pack);mutate(changed);self.assertIsNone(maintenance._period_end(changed,entry))
        findings=[{'code':'citation_or_contract','section':'结论与观点','detail':"section「结论与观点」carries figures with no source behind them: ['31']；写 缺来源 而不是猜一个数字"}]
        allowed=maintenance.plan(raw,findings,f.f.pack);changed=copy.deepcopy(raw)
        changed['sections'][0]['numbers'].append({'tag':'N5','period':'2027-12-31'})
        with self.assertRaisesRegex(ValueError,'date differs'):maintenance.assert_changes(raw,changed,allowed,f.f.pack)

    def test_updated_failure_feedback_is_strong_free_replay_not_a_new_call_or_permission(self):
        f=self.fixture();result=f.qualify();before=len(f.f.adapter.served)
        # Return the unchanged bad tag; the one maintenance call is consumed,
        # current complete diagnostics remain visible and no second call occurs.
        f.f.adapter.value=result['slots']['document:1'][1]
        maintained=maintenance.run(result,model=f.model,state=f.state,mission=f.f.mission,authority=f.f.authority)
        self.assertTrue(maintained['record']['findings']);recovery.persist(f.state,result)
        value=recovery.read_feedback(f.f.authority,state=f.state,mission=f.f.mission,model=f.model,company_ref=f.f.company['company_ref'])
        self.assertEqual(value['status'],'verified');self.assertEqual(value['items'][0]['contract_ref'],maintenance.CONTRACT)
        self.assertFalse(value['items'][0]['retry_authority']);self.assertEqual(len(f.f.adapter.served),before+1)
        maintenance.run(f.qualify(),model=f.model,state=f.state,mission=f.f.mission,authority=f.f.authority)
        self.assertEqual(len(f.f.adapter.served),before+1)

    def test_owner_scope_is_fixed_and_permission_capacity_refuses_before_purchase(self):
        f=self.fixture();result=f.qualify();before=len(f.f.adapter.served)
        authority=maintenance.Authority(result=result,model=f.model,state=f.state,mission=f.f.mission,authority=f.f.authority)
        with mock.patch.object(maintenance,'MAX_GRANTS',0):
            with self.assertRaisesRegex(ValueError,'capacity exhausted'):authority.issue()
        first=authority.issue();self.assertEqual(first,authority.issue())
        alias=copy.deepcopy(result);alias['record']['source_ticket_ref']='initial-screen:'+'f'*24
        alias['record']['validator_fingerprint']='a future validator'
        self.assertEqual(maintenance.permission(result),maintenance.permission(alias))
        f.model.config['purpose_run_budgets']={'initial_screen':{'max_cost_usd':224}}
        self.assertEqual(first,authority.issue());self.assertEqual(len(f.f.adapter.served),before)

    def test_same_permission_cannot_replay_a_foreign_formal_or_self_rehashed_claim(self):
        f=self.fixture();result=f.qualify();before=len(f.f.adapter.served)
        maintenance.run(result,model=f.model,state=f.state,mission=f.f.mission,authority=f.f.authority)
        real=maintenance._row
        def changed(connection,table,ref):
            row=real(connection,table,ref)
            if row and table==maintenance.CLAIMS:
                row['permission_hash']='f'*64
            return row
        with mock.patch.object(maintenance,'_row',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'child hash changed'):
                maintenance.run(f.qualify(),model=f.model,state=f.state,mission=f.f.mission,authority=f.f.authority)
        self.assertEqual(len(f.f.adapter.served),before+1)
