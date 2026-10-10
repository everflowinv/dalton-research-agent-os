from __future__ import annotations
import copy
import json
import sqlite3
import unittest
from unittest.mock import patch
from dalton_core.store import DaltonStore, content_hash
from dalton_core.research_followup import ResearchFollowupAuthority, requests, approval_items
from dalton_core.research_followup_automation import FollowupAutomationAuthority, groups, model_redraft_context, latest
from dalton_core.model_forecast_driver import ForecastModelAuthority
from dalton_core.model_cell_reconciliation import ModelCellReconciliationAuthority
from dalton_core.company_model_state import build_company_model_state
from dalton_core.company_model_cli import choose_company
from dalton_core.company_model_spec import TASK_HASH, build_prompt
from dalton_core.company_model_forecast_cli import pending_companies
from dalton_core.research_cycle_reflection import backlog_candidates
from tests import test_company_model_forecast as financial_fixture
from tests import test_model_forecast_driver as model_fixture


class AutomatedModelTests(unittest.TestCase):
    def setUp(self):
        self.f=financial_fixture.LaneStateTests('test_the_first_run_publishes_a_model_and_its_forecast_lines')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.store=self.f.store;self.c=self.store.connection
        self.models=ForecastModelAuthority(self.store)
        # This is an isolated synthetic pre-disclosure model, genuinely stored
        # at the fixture clock. Actual arrives later through the normal SEC authority.
        with patch('dalton_core.model_forecast_driver._now',return_value='2026-06-01T00:00:00+00:00'):
            self.f.child()
        self.old=self.models.latest(financial_fixture.ACN)
        self.auth=FollowupAutomationAuthority(self.store)
        self.requests=ResearchFollowupAuthority(self.store)

    def actual(self):
        mission=self.f.mission;missions=self.f.missions
        grant=missions.authorize_sec_lane(company_ref=financial_fixture.ACN,ticker='ACN',actor_ref='automation:coverage-mission',mission_version_ref=mission['id'],mission_version_hash=mission['content_hash'])
        dispatch=missions.queue_statement_dispatch(authorization=grant,form='10-Q',attempt=1)
        missions.mark_statement_dispatch_launched(dispatch['dispatch_id'],'sec-financials-run:'+ 'f'*24)
        values={financial_fixture.REVENUE_CONCEPT:'2000000000',financial_fixture.COST_CONCEPT:'1600000000',financial_fixture.SGA_CONCEPT:'200000000',financial_fixture.OPERATING_CONCEPT:'200000000',financial_fixture.PRETAX_CONCEPT:'200000000',financial_fixture.TAX_CONCEPT:'50000000',financial_fixture.NET_CONCEPT:'150000000'}
        lines=[{'statement':'income','concept':concept,'label':concept,'level':0,'parent_concept':None,'is_breakdown':False,'dimension_axis':None,'dimension_member':None,'period_start':'2026-06-01','period_end':'2026-08-31','value':value,'unit':'USD','balance':'credit'} for concept,value in values.items()]
        missions.record_statement_observation(dispatch_id=dispatch['dispatch_id'],observation={'schema_version':'0.1','cik':'0001467373','entity_name':'Synthetic services company','filings':[{'accession':'0001467373-26-000099','form':'10-Q','filed':'2026-09-15','report_date':'2026-08-31','lines':lines}],'source_record_refs':['raw-sink:'+'e'*64],'next_cursor':None,'provider_status':200},governance_ref='g',governance_hash='b'*64)
        missions.settle_statement_dispatch(dispatch['dispatch_id'],outcome='succeeded')
        records=ModelCellReconciliationAuthority(self.store).refresh(financial_fixture.ACN)
        self.assertTrue(any(r['redraft_required'] for r in records))

    def test_exact_source_enters_normal_spec_forecast_and_closes_only_after_strong_publication(self):
        prior_bytes=list(self.c.execute('SELECT record_json,content_hash FROM forecast_model_versions'))
        self.actual()
        before=list(self.c.execute('SELECT record_json,content_hash FROM research_followup_requests'))
        company,state=choose_company(self.f.missions,self.f.mission)
        self.assertEqual(company,financial_fixture.ACN)
        self.assertIn('model_redraft_context',state)
        self.assertIn('MATERIAL FORECAST DEVIATION REVIEW',build_prompt(state))
        spec=self.f.record_spec()
        self.auth.bind_spec(spec,state)
        self.assertTrue(pending_companies(self.f.missions,self.models,self.f.mission))
        outcome=self.f.child()
        self.assertEqual(outcome['cost_micros'],0)
        current=self.models.latest(company)
        self.assertNotEqual(current['id'],self.old['id'])
        completed=[latest(self.c,g['group_ref']) for g in groups(self.c)]
        self.assertTrue(completed)
        self.assertTrue(all(r['state']=='completed' for r in completed))
        self.assertFalse(approval_items(self.c,lambda ref:ref))
        self.assertEqual(before,list(self.c.execute('SELECT record_json,content_hash FROM research_followup_requests')))
        self.assertEqual(prior_bytes[0],self.c.execute('SELECT record_json,content_hash FROM forecast_model_versions WHERE version_id=?',(self.old['id'],)).fetchone())
        # Closing the system work does not drop the source binding and pay again.
        self.assertEqual(build_company_model_state(self.f.missions,company,ticker='ACN')['state_hash'],state['state_hash'])
        self.assertEqual(choose_company(self.f.missions,self.f.mission),(None,None))
        n=self.c.execute('SELECT count(*) FROM research_followup_automation_versions').fetchone()[0]
        self.assertEqual(self.f.child()['cost_micros'],0)
        self.auth.complete_model(current['id'])
        self.assertEqual(self.c.execute('SELECT count(*) FROM research_followup_automation_versions').fetchone()[0],n)

    def test_newer_unbound_model_original_model_and_fake_result_cannot_close(self):
        self.actual(); group=groups(self.c)[0]
        with self.assertRaisesRegex(ValueError,'binding'):
            self.auth.model_result(self.old['id'],group)
        with self.assertRaisesRegex(ValueError,'exact verified'):
            self.auth.observe(group,state='completed',reason='A later model exists.',evidence={'ref':'model:new','hash':'a'*64})
        self.assertEqual(latest(self.c,group['group_ref']),None)

    def test_later_formal_model_without_exact_redraft_input_receipt_remains_pending(self):
        self.actual(); before=groups(self.c)
        self.f.record_spec()  # Formal new specification, but no consume receipt.
        self.f.child()
        later=self.models.latest(financial_fixture.ACN)
        self.assertNotEqual(later['id'],self.old['id'])
        self.assertTrue(self.models.filing_proof(later['id']))
        for group in before:
            with self.assertRaisesRegex(ValueError,'binding'):
                self.auth.model_result(later['id'],group)
        self.assertTrue(approval_items(self.c,lambda ref:ref))
        self.assertTrue(all(latest(self.c,g['group_ref']) is None for g in before))

    def test_real_human_decision_takes_priority_without_touching_original_record(self):
        self.actual(); current=requests(self.c)[0]; old=current['content_hash']
        self.requests.decide(current['id'],action='declined',actor_ref='human:owner',reason='Explicit owner instruction takes priority.')
        self.assertFalse(any(current['id']==r['ref'] for g in groups(self.c) for r in g['requests']))
        self.assertEqual(requests(self.c)[0]['content_hash'],old)

    def test_grouping_coalesces_versions_but_never_crosses_company_or_period(self):
        self.actual(); first=groups(self.c)[0]
        request=next(r for r in requests(self.c) if r['id']==first['requests'][0]['ref'])
        second_scope={**request['scope'],'model_version_ref':'forecast-version:older-synthetic'}
        # Same exact company/cell/period and held evidence; context later refuses
        # a spoofed model version, but grouping does not allocate four children.
        self.requests.record(kind='model_redraft',company_ref=request['company_ref'],scope=second_scope,reason='Another old version of the same cell.',evidence=request['evidence'])
        combined=next(g for g in groups(self.c) if g['group_ref']==first['group_ref'])
        self.assertEqual(len(combined['requests']),2)
        with self.assertRaisesRegex(ValueError,'exact material comparison'):
            model_redraft_context(self.c,request['company_ref'])
        with self.assertRaisesRegex(ValueError,'different company'):
            self.requests.record(kind='model_redraft',company_ref='company:synthetic-other',scope=request['scope'],reason='Cross-company cannot merge.',evidence=request['evidence'])

    def test_budget_wait_and_repeated_tick_never_create_tasks_raise_budget_or_close(self):
        self.actual(); before=list(self.c.execute('SELECT record_json,content_hash FROM research_followup_requests'))
        result={'status':'budget_exhausted','company_ref':financial_fixture.ACN,'reason':'existing mission budget'}
        self.auth.observe_lane('dispatch_company_model_spec',result)
        n=self.c.execute('SELECT count(*) FROM research_followup_automation_versions').fetchone()[0]
        self.auth.observe_lane('dispatch_company_model_spec',result)
        self.assertEqual(self.c.execute('SELECT count(*) FROM research_followup_automation_versions').fetchone()[0],n)
        self.assertEqual(before,list(self.c.execute('SELECT record_json,content_hash FROM research_followup_requests')))
        self.assertTrue(all(latest(self.c,g['group_ref'])['state']=='waiting_budget' for g in groups(self.c)))
        self.assertTrue(all(c['decision_required'] is False for c in approval_items(self.c,lambda r:r)))
        self.assertEqual(self.f.missions.mission(self.f.mission['id'])['budget'],self.f.mission['budget'])


class ReflectionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.store=DaltonStore(':memory:');self.addCleanup(self.store.close);self.c=self.store.connection
        self.c.execute('CREATE TABLE research_cycle_reflection_versions(version_id TEXT,record_json TEXT,content_hash TEXT)')
        self.auth=ResearchFollowupAuthority(self.store);self.auto=FollowupAutomationAuthority(self.store)

    def reflection(self, *, granted=True, unknown=False):
        metrics={'spend':{'available':False},'backlog':{'available':True,'new_questions':8,'answered':1,'new_question_refs':['question:synthetic:1']},
          'planner':{'available':True,'inquiries_raised':8,'dispatched':0,'research_task_granted':granted,'probe_templates':2},
          'human_checkpoints':{'available':False},'ticks':{'available':False},'quality_scores':{'available':True,'published':0},'journal':{'available':True,'entries':0}}
        window={'iso_week':'2026-W40'}
        candidates=backlog_candidates(metrics,window)
        if unknown:candidates.append({'question':'Unclassified new finding','because':'Requires review','refs':[]})
        body={'id':'reflection:synthetic:1','mission_ref':'mission:synthetic','created_at':'2026-10-01','metrics':metrics,'window':window,'backlog_candidates':candidates}
        body['content_hash']=content_hash(body)
        self.c.execute('INSERT INTO research_cycle_reflection_versions VALUES(?,?,?)',(body['id'],json.dumps(body),body['content_hash']))
        return self.auth.capture_existing([body['id']])

    def test_internal_backlog_scoring_and_no_feedback_are_not_owner_decisions(self):
        self.reflection(); before=list(self.c.execute('SELECT * FROM research_followup_requests'))
        cards=approval_items(self.c,lambda r:r)
        self.assertEqual(len(cards),4)
        self.assertTrue(all(c['decision_required'] is False and not c['actions'] for c in cards))
        self.assertEqual(sum(c['responsibility']=='optional_feedback' for c in cards),1)
        self.auto.observe_lane('dispatch_research_task',{'status':'skipped:pool_exhausted','pool':{'remaining_micros':0}})
        self.auto.observe_lane('dispatch_quality_scoring',{'status':'idle','pending':0,'reason':'every Initial Screen version is scored'})
        states={g['route']:latest(self.c,g['group_ref']) for g in groups(self.c)}
        self.assertEqual(states['research_dispatch']['state'],'waiting_budget')
        self.assertEqual(states['backlog_research']['state'],'waiting_budget')
        self.assertEqual(states['quality_scoring']['state'],'waiting_evidence')
        budget_card=next(c for c in approval_items(self.c,lambda r:r) if c['ref']==states['research_dispatch']['group_ref'])
        self.assertEqual(budget_card['summary'],'等待研究预算恢复后继续。')
        self.assertEqual(before,list(self.c.execute('SELECT * FROM research_followup_requests')))
        n=self.c.execute('SELECT count(*) FROM research_followup_automation_versions').fetchone()[0]
        self.auto.observe_lane('dispatch_quality_scoring',{'status':'idle','pending':0,'reason':'every Initial Screen version is scored'})
        self.assertEqual(self.c.execute('SELECT count(*) FROM research_followup_automation_versions').fetchone()[0],n)

    def test_missing_grant_is_owner_decision_and_unknown_candidate_remains_visible(self):
        self.reflection(granted=False,unknown=True)
        cards=approval_items(self.c,lambda r:r)
        human=[c for c in cards if c.get('decision_required') is not False]
        self.assertEqual(len(human),2)
        self.assertTrue(all(c['actions'] for c in human))
        self.assertEqual({g['route'] for g in groups(self.c)} & {'owner_permission','needs_review'},{'owner_permission','needs_review'})

    def test_assignment_is_human_instruction_not_automatic_machine_ownership(self):
        rows=self.reflection()
        self.auth.decide(rows[0]['id'],action='assign',actor_ref='human:owner',reason='I am reviewing this exact issue.',owner_ref='human:analyst',due_on='2030-01-01')
        cards=approval_items(self.c,lambda r:r)
        human=[c for c in cards if c.get('decision_required') is not False]
        self.assertEqual(len(human),1)
        self.assertEqual(human[0]['owner_ref'],'human:analyst')

    def test_no_direct_unverified_write_or_mutation_is_allowed(self):
        self.reflection()
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("INSERT INTO research_followup_automation_versions VALUES('x','y','z','{}','a','now')")
        group=groups(self.c)[0]
        self.auto.observe(group,state='pending',reason='Normal existing lane will inspect.')
        with self.assertRaises(sqlite3.IntegrityError):self.c.execute('DELETE FROM research_followup_automation_versions')

class FollowupUITests(unittest.TestCase):
    def test_background_optional_and_admin_stay_visible_but_do_not_count_as_owner_decisions(self):
        from tests.test_cockpit_v2_migration import fixtures, run, posts
        f=fixtures()
        f['/v1/cockpit/needs-human']={'items':[],'count':0}
        common={'kind':'research_followup','who':'ACN · Synthetic','summary':'Actual held status.','next_step':'Actual held status.','decision_required':False,'actions':[], 'followup_fields':False}
        f['/v1/cockpit/approvals']={'items':[
            {**common,'ref':'followup:system','title':'自动复核偏差','responsibility':'background'},
            {**common,'ref':'followup:admin','title':'真实失败诊断','responsibility':'administrator'},
            {**common,'ref':'followup:feedback','title':'使用反馈是可选项','responsibility':'optional_feedback'},
            {'kind':'research_followup','ref':'followup:unknown','who':'ACN · Synthetic','title':'未知研究决定','summary':'Needs review, retained.','actions':[{'decision':'declined','label':'不开展并说明原因'}]}],'count':1}
        out=run({'t':'#/tasks'},fixtures=f)
        text=out['results']['t']['text']
        self.assertIn('1 项需要处理',text)
        self.assertIn('后台推进',text);self.assertIn('管理员诊断',text);self.assertIn('可选反馈',text)
        self.assertEqual(text.count('Actual held status.'),3)
        self.assertIn('未知研究决定',text)
        self.assertLess(text.index('未知研究决定'),text.index('后台推进'))
        self.assertEqual(posts(out,'/v1/cockpit/decide'),[])
        self.assertNotIn('已完成并附结果',text)

    def test_shell_badge_uses_same_owner_only_projection(self):
        from tests.test_cockpit_v2_shell import node
        out=node('call',{'module':'pages/task_kinds.js','calls':[['actionCount',[],[{'decision_required':False},{'ref':'owner'}]]]})
        # Explicitly verify the exported shared count (shell and page share it).
        self.assertEqual(out,[{'ok':1}])

class CurrentGrantTests(unittest.TestCase):
    setUp=ReflectionRoutingTests.setUp
    reflection=ReflectionRoutingTests.reflection
    def test_old_missing_grant_uses_only_verified_current_same_project(self):
        from tests.p9a_fixtures import bootstrap_method_authorities,mission_params
        from dalton_core.coverage_mission import CoverageMissionAuthority
        from dalton_core.research_followup_automation import reflection_route
        methods=bootstrap_method_authorities(self.store); missions=CoverageMissionAuthority(self.store)
        params=mission_params(methods); mission_ref=params.pop('mission_ref')
        original=missions.create_mission(mission_ref,**params)
        rows=self.reflection(granted=False)
        r=copy.deepcopy(rows[0]);r['owner_binding']={'mission_version_ref':original['id'],'mission_hash':original['content_hash'],'owner_ref':original['actor_ref']}
        next_params=copy.deepcopy(params);next_params['idempotency_key']='followup-current-grant';next_params['version_id']='coverage-mission-version:synthetic:2';next_params['prior_version_ref']=original['id'];next_params['autonomy']['may_write']+=['research_task','model_run','stage_record']
        # Deduplicate valid scopes; no cap is raised and the authority owns the lineage.
        next_params['autonomy']['may_write']=sorted(set(next_params['autonomy']['may_write']))
        current=missions.create_mission(mission_ref,**next_params)
        self.assertEqual(current['prior_version_ref'],original['id'])
        self.assertEqual(current['budget'],original['budget'])
        self.assertEqual(reflection_route(self.c,r),'research_dispatch')
        revoke=copy.deepcopy(next_params);revoke['idempotency_key']='followup-revoke-grant';revoke['version_id']='coverage-mission-version:synthetic:3';revoke['prior_version_ref']=current['id'];revoke['autonomy']['may_write'].remove('research_task')
        revoked=missions.create_mission(mission_ref,**revoke)
        self.assertEqual(reflection_route(self.c,r),'owner_permission')
        other=copy.deepcopy(next_params);other['idempotency_key']='followup-other-principal';other['version_id']='coverage-mission-version:synthetic:4';other['prior_version_ref']=revoked['id'];other['autonomy']['automation_principal']='automation:synthetic-other'
        missions.create_mission(mission_ref,**other)
        self.assertEqual(reflection_route(self.c,r),'owner_permission')

class RequiredNarrativeWaitTests(unittest.TestCase):
    def test_strong_current_gap_is_background_but_unknown_wrong_hash_and_superseded_are_not(self):
        import tempfile
        from pathlib import Path
        from tests import test_mission_deliverable as deliverables
        from dalton_core.needs_human import _required_initial_screen_gaps, parked_publications, completion_entry
        from dalton_core.cockpit_research_library import research_library
        from dalton_core.research_source_projection import publication_source_hash
        f=deliverables.DeliverableHarness('runTest');f.setUp();self.addCleanup(f.doCleanups);f.grant('deliverable')
        source=f.publish([{'title':'业务','body':'The held company describes its service contracts.','claim_refs':[],'numbers':[],'gaps':[]},{'title':'估值','body':'','claim_refs':[],'numbers':[],'gaps':['缺少可比较的估值输入。']}])
        product=next(p for p in research_library(f.store.connection,f.mission,deliverables.ACN,localize=False)['products'] if p['kind']=='initial_screen')
        identity={'kind':'initial_screen','subject_ref':deliverables.ACN,'version_ref':source['id']}
        with tempfile.TemporaryDirectory() as temporary:
            state=Path(temporary); target=sqlite3.connect(str(state/'core.sqlite'));f.store.connection.backup(target);target.close()
            strong=_required_initial_screen_gaps(state,identity,publication_source_hash(product))
            self.assertEqual(strong['source_hash'],source['content_hash'])
            self.assertEqual(strong['classification'],'waiting_material_required_narrative')
            self.assertIsNone(_required_initial_screen_gaps(state,identity,'0'*64))
            self.assertIsNone(_required_initial_screen_gaps(state,{**identity,'version_ref':'missing:version'}))
            self.assertIsNone(_required_initial_screen_gaps(state,{**identity,'kind':'debate_map'}))
            work=state/'publication';products=work/'products';products.mkdir(parents=True)
            from dalton_core.research_publication_worker import _hash
            h=_hash(identity);digest=publication_source_hash(product)
            (products/(h+'.json')).write_text(json.dumps({'product_hash':digest,'status':'skipped','result':{'skip_reason':'source_section_empty','empty_sections':['估值']}}))
            (work/'worker-last-run.json').write_text(json.dumps({'products':[{'identity':identity,'product_hash':digest,'parked':True}]}))
            with patch('dalton_core.final_text_state.publication_work_dir',return_value=work): cards=parked_publications(state)
            self.assertEqual(len(cards),1);self.assertFalse(cards[0]['actionable']);self.assertEqual(cards[0]['responsibility'],'background')
            self.assertEqual(cards[0]['detail']['version_ref'],source['id'])
            self.assertNotEqual(completion_entry(cards[0])['mode'],'acknowledge')
            self.assertNotIn('retry',cards[0]['detail'])

    def test_waiting_material_visible_without_owner_retry_or_badge_in_dom(self):
        from tests.test_cockpit_v2_migration import fixtures,run,posts
        f=fixtures();f['/v1/cockpit/approvals']={'items':[],'count':0}
        f['/v1/cockpit/needs-human']={'items':[{'kind':'publication_parked','ref':'source:gap','title':'必需章节尚缺正文','why_blocked':'尚无合格资料或研究结论',
            'actionable':False,'responsibility':'background','detail':{'company_ref':'company:sec-cik:0001467373','status':'waiting_material'},'completion_entry':{'mode':'none'}}],'count':0}
        out=run({'t':'#/tasks'},fixtures=f);text=out['results']['t']['text']
        self.assertIn('后台推进',text);self.assertIn('必需章节尚缺正文',text);self.assertNotIn('1 项需要处理',text)
        self.assertNotIn('重新生成中文定稿',text);self.assertEqual(posts(out,'/v1/cockpit/language_retry'),[])
        self.assertTrue(any('查看当前研究与资料缺项' in label for label,href in out['results']['t']['links']))


class MaterialWaitingClassificationTests(unittest.TestCase):
    def test_exhausted_search_is_visible_background_but_budget_and_login_need_action(self):
        from pathlib import Path
        from datetime import datetime, timezone
        from dalton_core.needs_human import material_acquisition_cards, completion_entry
        raw=[{'kind':kind,'ref':'material:'+kind,'at':'2026-10-10T11:45:00+00:00',
              'title':kind,'why':'Actual immutable acquisition history.','action':'Optional supply; normal retry.',
              'consequence':'The missing material remains explicit.','where':'无需操作，系统会自动重试',
              'detail':{'company_ref':'company:synthetic','request_refs':['material-request:synthetic']}}
             for kind in ['material_sources_exhausted','material_budget_exceeded','material_credentials_needed']]
        with patch('dalton_core.material_acquisition.owner_door_cards',return_value=raw):
            cards=material_acquisition_cards(Path('/unused'),now=datetime.now(timezone.utc))
        self.assertFalse(cards[0]['actionable']);self.assertEqual(cards[0]['responsibility'],'background')
        self.assertEqual(cards[0]['detail']['request_refs'],['material-request:synthetic'])
        self.assertNotEqual(completion_entry(cards[0])['mode'],'acknowledge')
        self.assertTrue(all(card['actionable'] for card in cards[1:]))
        self.assertEqual(sum(card['actionable'] for card in cards),2)


class AutomationSchemaOwnershipTests(unittest.TestCase):
    def test_install_and_rehearsal_own_the_packaged_core_schema(self):
        from dalton_core.bootstrap import SCHEMA_DATABASES, apply_packaged_schemas
        from scripts.rehearse_deploy import CORE_MIGRATIONS
        from pathlib import Path
        import tempfile
        self.assertIn(('research_followup_automation_schema.sql',None),SCHEMA_DATABASES)
        spec=next(x for x in CORE_MIGRATIONS if x.schema=='research_followup_automation_schema.sql')
        self.assertEqual((spec.database,spec.kind,spec.symbol),('core.sqlite','core','FollowupAutomationAuthority'))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            apply_packaged_schemas(root)
            db=sqlite3.connect(root/'core.sqlite')
            try:
                names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({'research_followup_automation_versions','research_followup_spec_bindings'}.issubset(names))
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("INSERT INTO research_followup_automation_versions VALUES('x','g','c','{}','h','t')")
            finally:db.close()

class AutomaticSubjectLabelTests(unittest.TestCase):
 def test_project_and_industry_cards_use_verified_mission_title_without_mutating_records(self):
  from dalton_core.coverage_mission import CoverageMissionAuthority
  from dalton_core.research_followup_automation import _system_subject_label,_system_reason
  from tests.p9a_fixtures import bootstrap_method_authorities,mission_params
  store=DaltonStore(':memory:');self.addCleanup(store.close)
  missions=CoverageMissionAuthority(store);params=mission_params(bootstrap_method_authorities(store));ref=params.pop('mission_ref');params['title']='合成行业研究课题'
  mission=missions.create_mission(ref,**params);before=list(store.connection.execute('SELECT record_json,content_hash FROM coverage_mission_versions'))
  self.assertEqual(_system_subject_label(store.connection,mission['industry_ref'],lambda ref:ref),'合成行业研究课题')
  self.assertEqual(_system_subject_label(store.connection,ref,lambda ref:ref),'合成行业研究课题')
  self.assertEqual(_system_subject_label(store.connection,'industry:foreign',lambda ref:ref),'行业研究')
  self.assertEqual(_system_reason({'state':'running'},'old defensive words'),'研究已启动，结果仍待核验。')
  self.assertEqual(before,list(store.connection.execute('SELECT record_json,content_hash FROM coverage_mission_versions')))
