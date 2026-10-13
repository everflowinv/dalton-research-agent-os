"""No-cost structural recovery keeps the complete plan and paid ancestry."""
import copy
import json
import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from dalton_core import planner_structural_revalidation as recovery
from dalton_core.store import canonical_json, content_hash
from dalton_core.research_state import state_content_hash
from dalton_core.research_planner import plan_from_response
from dalton_core.planner_paid_output_recovery import recovery_readers
from tests import test_planner_paid_output_recovery as paid_fixtures
from tests import test_cockpit_model_fallback as chain


class WrapperTests(unittest.TestCase):
    def wire(self):
        rows=[]
        for company in ('company:synthetic:alpha','company:synthetic:beta'):
            inner={'strategy_version':'directed-document:0.1','document_ref':'doc:'+company,
                'document_version_hash':'a'*64,'query_terms':['exact revenue'],'query_rationale':'Read the original.',
                'numeric_targets':[{'target_ref':'target:'+company}]}
            outer={k:copy.deepcopy(inner[k]) for k in ('strategy_version','document_ref','document_version_hash','query_terms','query_rationale')}
            outer['directed_document']=inner
            rows.append({'company_ref':company,'question':'Exact filed amount?','directed_document':outer})
        return {'inquiries':rows,'directives':[{'reason':'Keep this directive'}],'sufficiency':[{'reason':'Keep this assessment'}]}

    def test_two_companies_full_plan_is_preserved_and_only_one_redundant_layer_removed(self):
        wire=self.wire();before=copy.deepcopy(wire);got,proof=recovery.normalize_wrappers(wire)
        self.assertEqual(wire,before);self.assertEqual(len(proof),2)
        for old,new in zip(wire['inquiries'],got['inquiries']):
            self.assertEqual(new['directed_document'],old['directed_document']['directed_document'])
            self.assertEqual(new['question'],old['question'])
        self.assertEqual(got['directives'],wire['directives']);self.assertEqual(got['sufficiency'],wire['sufficiency'])
        self.assertEqual(recovery.normalize_wrappers(got),(got,[]))

    def test_conflicting_missing_unknown_or_recursive_wrappers_are_not_repaired(self):
        for mutation in ('source','terms','extra','missing','recursive'):
            wire=self.wire();outer=wire['inquiries'][0]['directed_document']
            if mutation=='source':outer['document_version_hash']='f'*64
            if mutation=='terms':outer['query_terms']=['different instruction']
            if mutation=='extra':outer['unknown']='preserve this information'
            if mutation=='missing':del outer['query_rationale']
            if mutation=='recursive':outer['directed_document']['directed_document']={'another':'level'}
            got,proof=recovery.normalize_wrappers(wire)
            with self.subTest(mutation=mutation):
                self.assertEqual(got['inquiries'][0],wire['inquiries'][0]);self.assertEqual(len(proof),1)


class SuccessfulPlanAdapter(chain.ChainAdapter):
    def __init__(self,text=None):
        super().__init__({});self.text=text
    def execute(self,work,route,profile):
        inv,result=super().execute(work,route,profile)
        inv=replace(inv,usage={'raw_provider_telemetry':{'cost':{'available':True,'usd':0.001}}})
        text=canonical_json({'schema_version':'0.1','assessment':'Complete unchanged synthetic input.',
            'directives':[],'inquiries':[],'sufficiency':[]})
        return inv,replace(result,outputs={'text':self.text or text},metadata={'route_decision_ref':route['id'],
            'profile_version_ref':profile['profile_version_ref'],'thinking_level':route['thinking_level']})


class PaidParentTests(unittest.TestCase):
    def fixture(self,adapter=None):
        h=paid_fixtures.PlannerRecoveryTests('runTest');h.setUp();self.addCleanup(h.doCleanups)
        state,model,adapter,config=h.planner_fixture(adapter or SuccessfulPlanAdapter())
        prompt='Fixed instructions.\nRESEARCH_STATE='+canonical_json(state)
        call=model.call(purpose='plan',request_id=state['content_hash'][:24],prompt=prompt,mission=h.mission)
        return h,state,model,adapter,config,call

    def candidate(self,h,state,*,mission=None,validate=None):
        with sqlite3.connect(h.root/'scheduler.sqlite') as c,recovery_readers(h.router_db,h.root/'budget.sqlite',now=datetime.now(timezone.utc)) as (router,budget):
            c.row_factory=sqlite3.Row
            return recovery.paid_parent(c,router,budget,state=state,mission=mission or h.mission,
                validate=validate or (lambda text:plan_from_response(state,text,created_at=h.mission['created_at'])))

    def ledger(self,h):
        with sqlite3.connect(h.root/'scheduler.sqlite') as c,sqlite3.connect(h.root/'budget.sqlite') as b:
            return (c.execute('SELECT * FROM scheduler_work_orders').fetchall(),
                c.execute('SELECT * FROM scheduler_formal_results').fetchall(),
                b.execute('SELECT * FROM thesis_impact_day_admissions').fetchall(),
                b.execute('SELECT * FROM thesis_impact_day_settlements').fetchall())

    def test_real_cockpit_work_route_success_fee_replays_whole_answer_without_new_work(self):
        h,state,model,adapter,config,call=self.fixture();before=self.ledger(h)
        for _ in range(2):
            got=self.candidate(h,state);self.assertIsNotNone(got)
            self.assertEqual(got['proof']['model_calls_allowed'],0);self.assertEqual(len(got['proof']['fees']),1)
            self.assertEqual(got['proof']['plan_hash'],got['plan']['content_hash'])
        self.assertEqual(self.ledger(h),before);self.assertEqual(len(adapter.served),1)

    def test_current_full_state_mission_source_change_or_display_state_cannot_borrow_parent(self):
        h,state,model,adapter,config,call=self.fixture()
        for mutation in ('mission','source','capability','display','hash'):
            changed=copy.deepcopy(state);mission=copy.deepcopy(h.mission)
            if mutation=='mission':mission['content_hash']='f'*64
            if mutation=='source':changed['companies'][0]['readable_documents']=[{'document_ref':'changed'}]
            if mutation=='capability':changed['document_research_policy']={'policy_ref':'changed'}
            if mutation=='display':changed['prompt_projection']={'full_state_hash':state['content_hash']}
            if mutation=='hash':changed['content_hash']='f'*64
            if mutation not in ('display','hash'):changed['content_hash']=state_content_hash(changed)
            with self.subTest(mutation=mutation):
                if mutation in ('display','hash'):
                    with self.assertRaises(ValueError):self.candidate(h,changed,mission=mission)
                else:self.assertIsNone(self.candidate(h,changed,mission=mission))
        self.assertEqual(len(adapter.served),1)

    def test_incomplete_fee_and_tampered_formal_refuse_without_new_calls(self):
        for mutation in ('fee','formal'):
            h,state,model,adapter,config,call=self.fixture()
            db=h.root/('budget.sqlite' if mutation=='fee' else 'scheduler.sqlite')
            with sqlite3.connect(db) as c:
                table='thesis_impact_day_settlements' if mutation=='fee' else 'scheduler_formal_results'
                for name, in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(table,)).fetchall():c.execute('DROP TRIGGER '+name)
                if mutation=='fee':c.execute('DELETE FROM thesis_impact_day_settlements')
                else:c.execute("UPDATE scheduler_formal_results SET result_envelope_hash='corrupt'")
            with self.subTest(mutation=mutation):
                if mutation=='fee':self.assertIsNone(self.candidate(h,state))
                else:
                    with self.assertRaises(ValueError):self.candidate(h,state)
            self.assertEqual(len(adapter.served),1)

    def test_unknown_null_negative_or_boolean_settlement_amount_is_not_a_fee_proof(self):
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
        h,state,model,adapter,config,call=self.fixture()
        original=ThesisImpactBudgetStore.admission
        for amount in (None,'unknown',-1,True):
            def changed(reader,**kwargs):
                paid=original(reader,**kwargs);paid['settlement']['actual_micros']=amount
                return paid
            with self.subTest(amount=amount),patch.object(ThesisImpactBudgetStore,'admission',changed):
                self.assertIsNone(self.candidate(h,state))
        self.assertEqual(len(adapter.served),1)

    def test_full_validator_failure_never_applies_a_partial_plan(self):
        h,state,model,adapter,config,call=self.fixture()
        def reject(_):raise ValueError('genuine unresolved source')
        self.assertIsNone(self.candidate(h,state,validate=reject));self.assertEqual(len(adapter.served),1)

    def test_cli_reentry_before_model_construction_then_restart_uses_formal_plan(self):
        from dalton_core.research_planner_cli import run_planner,record_attempt,read_attempt_ledger
        h,state,model,adapter,config,call=self.fixture()
        record_attempt(h.root,state['content_hash'],{'created_at':h.mission['created_at'],'status':'succeeded','plan_status':'refused','failure_reason':'closed strategy refused'})
        before=self.ledger(h)
        kwargs=dict(state_dir=h.root,model_config_path=config,scheduler_db=h.root/'scheduler.sqlite',plans_dir=h.root/'discovery-plans',summary_dir=h.root/'out')
        with patch('dalton_core.research_planner_cli.build_state',return_value=state),patch('dalton_core.research_planner_cli.CockpitModel',side_effect=AssertionError('free recovery must not create model')):
            first=run_planner(**kwargs);second=run_planner(**kwargs)
        self.assertEqual(first['plan_status'],'fresh');self.assertEqual(second['plan_status'],'unchanged')
        self.assertEqual(first['cost_micros'],0);self.assertEqual(first['structural_revalidation']['state_hash'],state['content_hash'])
        self.assertEqual(read_attempt_ledger(h.root)[state['content_hash']]['structural_revalidation_contract'],recovery.CONTRACT)
        self.assertEqual(self.ledger(h),before);self.assertEqual(len(adapter.served),1)

    def test_current_model_budget_change_preserves_original_work_and_fees_during_free_reentry(self):
        from dalton_core.research_planner_cli import run_planner,record_attempt
        h,state,model,adapter,config,call=self.fixture()
        revised=json.loads(config.read_text());revised['purpose_call_budgets']={'plan':{'max_cost_usd':2.0}}
        config.write_text(json.dumps(revised))
        record_attempt(h.root,state['content_hash'],{'created_at':h.mission['created_at'],'plan_status':'refused'})
        before=self.ledger(h)
        with patch('dalton_core.research_planner_cli.build_state',return_value=state),patch('dalton_core.research_planner_cli.CockpitModel',side_effect=AssertionError('no new paid request')):
            got=run_planner(state_dir=h.root,model_config_path=config,scheduler_db=h.root/'scheduler.sqlite',plans_dir=h.root/'discovery-plans',summary_dir=h.root/'out')
        self.assertEqual(got['plan_status'],'fresh');self.assertEqual(self.ledger(h),before)

    def test_genuine_content_failure_is_checked_once_and_remains_held_not_rebought(self):
        from dalton_core.research_planner_cli import run_planner,record_attempt,read_attempt_ledger
        bad=canonical_json({'schema_version':'0.1','assessment':'Keep all original errors.','directives':[],
            'inquiries':[{'company_ref':'company:foreign','question':'Cannot select a foreign company','wants':'Original','because':'No source'}],'sufficiency':[]})
        h,state,model,adapter,config,call=self.fixture(SuccessfulPlanAdapter(bad))
        record_attempt(h.root,state['content_hash'],{'created_at':h.mission['created_at'],'plan_status':'refused'})
        before=self.ledger(h)
        import dalton_core.planner_structural_revalidation as module
        with patch('dalton_core.research_planner_cli.build_state',return_value=state),patch('dalton_core.research_planner_cli.CockpitModel',side_effect=AssertionError('no rebuy')),patch.object(module,'candidate',wraps=module.candidate) as read:
            for _ in range(2):
                got=run_planner(state_dir=h.root,model_config_path=config,scheduler_db=h.root/'scheduler.sqlite',plans_dir=h.root/'discovery-plans',summary_dir=h.root/'out')
                self.assertEqual(got['plan_status'],'held')
            self.assertEqual(read.call_count,1)
            self.assertEqual(read_attempt_ledger(h.root)[state['content_hash']]['plan_status'],'refused')
            with patch('dalton_core.research_planner_cli.datetime',wraps=datetime) as clock:
                clock.now.return_value=datetime.now(timezone.utc)+timedelta(hours=2)
                later=run_planner(state_dir=h.root,model_config_path=config,scheduler_db=h.root/'scheduler.sqlite',plans_dir=h.root/'discovery-plans',summary_dir=h.root/'later')
            self.assertEqual(later['held_reason'],'terminal_for_this_state');self.assertEqual(read.call_count,1)
        self.assertEqual(self.ledger(h),before);self.assertEqual(len(adapter.served),1)


class CanonicalCurrentTests(unittest.TestCase):
    def fixture(self):
        from tests import test_directed_numeric_workflow as f
        h=f.NumericWorkflowTests('runTest');h.addCleanup=self.addCleanup
        data=h.fixture();fixture,authority,admission,*_=data
        inquiry=copy.deepcopy(admission['planner_inquiry'])
        inquiry['canonical_question']={'version_ref':admission['question_version_ref'],'content_hash':admission['question_version_hash']}
        state={'reflection_followup_context':{'backlog_questions':[{'state':'open','head':{'id':admission['question_version_ref'],'content_hash':admission['question_version_hash'],'company_ref':inquiry['company_ref']}}]}}
        return fixture,authority,admission,inquiry,state

    def test_actual_numeric_admission_source_and_current_open_head_are_resolved_without_grant(self):
        f,a,admission,inquiry,state=self.fixture();before=f.store.connection.total_changes
        got=recovery.current_numeric_canonical_binding(f.store.connection,f.mission,inquiry,state)
        self.assertEqual(got,inquiry['canonical_question']);self.assertEqual(set(got),{'version_ref','content_hash'})
        self.assertEqual(f.store.connection.total_changes,before)
        inquiry['directed_document']['numeric_targets'][0]['metric_ref']='metric:different'
        # Resolving the question is not resolving target equivalence or granting
        # a purchase. The ordinary numeric admission owner still checks it.
        self.assertEqual(recovery.current_numeric_canonical_binding(f.store.connection,f.mission,inquiry,state),got)

    def test_full_validator_accepts_redundant_wrapper_and_strong_omitted_canonical_then_rejects_other_bad_inquiry(self):
        f,a,admission,inquiry,followup=self.fixture()
        reg=admission['request']['registration']
        state={'schema_version':'0.1','content_hash':'a'*64,'goal':{},
            'companies':[{'company_ref':inquiry['company_ref'],'items':[],
                'readable_documents':[{'document_ref':reg['document_ref'],'document_version_hash':reg['content_hash'],
                    'readable':True,'authority_ref':reg['id'],'authority_hash':reg['content_hash'],
                    'source_ref':reg['source_ref'],'operations':['search_registered_document','read_registered_document']}]}],
            'document_research_policy':a.registry.policy,'document_research_contract_ref':'directed-document:0.1',
            'document_research_numeric_contract_ref':'directed-document-numeric:0.1',**followup}
        inquiry.pop('rank',None)
        inner=copy.deepcopy(inquiry['directed_document'])
        outer={k:copy.deepcopy(inner[k]) for k in ('strategy_version','document_ref','document_version_hash','query_terms','query_rationale')}
        outer['directed_document']=inner;inquiry['directed_document']=outer
        other=copy.deepcopy(inquiry);other['question']='Preserve the second question as written.'
        wire={'schema_version':'0.1','assessment':'Read complete originals.','directives':[],'inquiries':[inquiry,other],'sufficiency':[]}
        resolver=lambda i,st:recovery.current_numeric_canonical_binding(f.store.connection,f.mission,i,st)
        got=plan_from_response(state,canonical_json(wire),created_at=f.mission['created_at'],canonical_binding_resolver=resolver)
        self.assertEqual(len(got['inquiries']),2);self.assertEqual(got['inquiries'][0]['directed_document'],inner)
        self.assertEqual(got['inquiries'][1]['question'],other['question'])
        self.assertEqual(got['inquiries'][0]['canonical_question'],inquiry['canonical_question'])
        wire['inquiries'][1]['directed_document']['document_version_hash']='f'*64
        with self.assertRaises(ValueError):plan_from_response(state,canonical_json(wire),created_at=f.mission['created_at'],canonical_binding_resolver=resolver)

    def test_original_withdrawal_or_wrong_principal_cannot_supply_numeric_anchor(self):
        from dalton_core.mission_document_research_withdrawal import MissionDocumentWithdrawalAuthority
        f,a,admission,inquiry,state=self.fixture()
        wrong=copy.deepcopy(f.mission);wrong['autonomy']['automation_principal']='automation:foreign'
        with self.assertRaises(ValueError):recovery.current_numeric_canonical_binding(f.store.connection,wrong,inquiry,state)
        owner=MissionDocumentWithdrawalAuthority(f.store,clock=f.harness.clock)
        owner.withdraw(admission['id'],actor_ref='human:owner',reason='Do not reuse this original authority.',
            hold={'disposition':'recovery_required','reason':'contract_failed_after_automatic_retry','ticket_ref':None})
        with self.assertRaises(ValueError):recovery.current_numeric_canonical_binding(f.store.connection,f.mission,inquiry,state)

    def test_stale_full_state_cannot_override_a_new_owner_backlog_head(self):
        from dalton_core.research_question_backlog import ResearchQuestionBacklog
        f,a,admission,inquiry,state=self.fixture()
        original=admission['planner_inquiry']
        new=ResearchQuestionBacklog(f.store).record_question(
            mandate_version_ref=f.mission['bindings']['mandate_version']['ref'],
            company_ref=inquiry['company_ref'],question=original['question'],
            answer_criteria=original['wants']+' Recheck the exact statement scope.',
            source_refs=[admission['source_ref']],actor_ref=f.mission['autonomy']['automation_principal'],
            mission_binding={'ref':f.mission['id'],'hash':f.mission['content_hash']})
        self.assertNotEqual(new['question_version_ref'],inquiry['canonical_question']['version_ref'])
        with self.assertRaises(ValueError):recovery.current_numeric_canonical_binding(f.store.connection,f.mission,inquiry,state)

    def test_closed_head_foreign_company_source_hash_or_untyped_is_refused(self):
        f,a,admission,inquiry,state=self.fixture()
        for mutation in ('closed','company','source','hash','untyped'):
            i=copy.deepcopy(inquiry);s=copy.deepcopy(state)
            if mutation=='closed':s['reflection_followup_context']['backlog_questions'][0]['state']='closed'
            if mutation=='company':i['company_ref']='company:foreign'
            if mutation=='source':i['directed_document']['document_version_hash']='f'*64
            if mutation=='hash':i['canonical_question']['content_hash']='f'*64
            if mutation=='untyped':i['directed_document'].pop('numeric_targets')
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):recovery.current_numeric_canonical_binding(f.store.connection,f.mission,i,s)


class InputWakeTests(unittest.TestCase):
    def test_current_quality_and_forecast_move_signature_without_new_claims(self):
        from tests import test_research_planner_cli as f
        from dalton_core.research_quality_rubrics import INITIAL_SCREEN_STRL as rubric
        from dalton_core.research_quality_score import SCORER_VERSION
        from dalton_core.model_forecast_driver import ForecastModelAuthority
        from tests.test_model_forecast_driver import model
        h=f.PlannerChildTests('runTest');h.setUp();self.addCleanup(h.doCleanups);self.addCleanup(h.close)
        mission=h.publish_mission();c=h.store.connection
        before=recovery.input_signature(c)
        ForecastModelAuthority(h.store).publish(model(mission_version_ref=mission['id']))
        after=recovery.input_signature(c);self.assertNotEqual(before,after);self.assertEqual(recovery.input_signature(c),after)
        # Reuse canonical target/score fixtures, then copy the REAL mission
        # records from the owner. The signature reader validates their hashes.
        from tests.test_initial_quality_source_proof import FeedbackTests
        q=FeedbackTests('runTest');q.addCleanup=self.addCleanup
        qc=q.fixture()
        for table in ('coverage_mission_versions','coverage_mission_pointer'):
            cols=[row[1] for row in c.execute('PRAGMA table_info('+table+')')]
            qc.execute('CREATE TABLE '+table+' ('+','.join(cols)+')')
            values=[tuple(row) for row in c.execute('SELECT * FROM '+table)]
            qc.executemany('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in cols)+')',values)
        target=json.loads(qc.execute('SELECT record_json FROM mission_deliverable_versions').fetchone()[0])
        target['subject_ref']=mission['universe'][0]['company_ref'];target.pop('content_hash');target['content_hash']=content_hash(target)
        qc.execute('UPDATE mission_deliverable_versions SET subject_ref=?,record_json=?,content_hash=?',(target['subject_ref'],canonical_json(target),target['content_hash']))
        record=json.loads(qc.execute('SELECT record_json FROM research_quality_score_versions').fetchone()[0])
        record['target_hash']=target['content_hash'];record['deterministic']['target_hash']=target['content_hash'];record.pop('content_hash');record['content_hash']=content_hash(record)
        qc.execute('UPDATE research_quality_score_versions SET target_hash=?,record_json=?,content_hash=?',(target['content_hash'],canonical_json(record),record['content_hash']))
        scored=recovery.input_signature(qc);self.assertEqual(recovery.input_signature(qc),scored)
        from dalton_core.initial_screen_quality_feedback import read_feedback
        from dalton_core.research_state import planning_hash,planning_core_hash
        from dalton_core.research_planner_cli import build_state,replan_pacing_hold
        current=build_state(h.store,h.missions,mission,plans_dir=h.state/'discovery-plans',as_of=mission['created_at'])
        changed=copy.deepcopy(current)
        changed['companies'][0]['initial_screen_quality_feedback']=read_feedback(qc,company_ref=target['subject_ref'])
        self.assertNotEqual(planning_hash(current),planning_hash(changed))
        self.assertNotEqual(planning_core_hash(current),planning_core_hash(changed))
        prior={'at':datetime.now(timezone.utc).isoformat(),'planning_hash':planning_hash(current),'planning_core_hash':planning_core_hash(current)}
        self.assertIsNone(replan_pacing_hold(prior,planning_hash(changed),planning_core_hash=planning_core_hash(changed),now=datetime.now(timezone.utc)))
        previous=copy.deepcopy(current);previous['companies'][0]['financial_model']={'status':'missing','reason':'no_forecast_model'}
        self.assertNotEqual(planning_core_hash(previous),planning_core_hash(current))
        qc.execute('DELETE FROM research_quality_score_versions')
        awaiting=recovery.input_signature(qc);self.assertNotEqual(awaiting,scored)
        self.assertEqual(recovery.input_signature(qc),awaiting)
        from tests.test_research_planner_launcher import FakeLauncher
        from dalton_core.research_planner_launcher import ResearchPlannerCoordinator
        launcher=FakeLauncher(h.root);clock=datetime.now(timezone.utc)
        coordinator=ResearchPlannerCoordinator(store=h.store,launcher=launcher,clock=lambda:clock)
        proved_signature=recovery.input_signature
        with patch('dalton_core.planner_structural_revalidation.input_signature',side_effect=lambda _c:proved_signature(qc)):
            self.assertEqual(coordinator.dispatch_once()['status'],'launched')
            launcher.finish({'plan_status':'unchanged'});clock+=timedelta(minutes=5)
            self.assertEqual(coordinator.dispatch_once()['status'],'held')
            qc.execute('INSERT INTO research_quality_score_versions VALUES(?,?,?,?,?,?,?,?)',(canonical_json(record),record['content_hash'],target['id'],target['content_hash'],rubric.rubric_ref,rubric.content_hash,SCORER_VERSION,'later'))
            self.assertEqual(coordinator.dispatch_once()['status'],'launched')
            launcher.finish({'plan_status':'fresh'});clock+=timedelta(minutes=5)
            self.assertEqual(coordinator.dispatch_once()['status'],'held');self.assertEqual(launcher.starts,2)

    def test_revalidation_ledger_marker_survives_later_holds_and_legacy_shape_is_unchanged(self):
        import tempfile
        from pathlib import Path
        from dalton_core.research_planner_cli import record_attempt,read_attempt_ledger
        with tempfile.TemporaryDirectory() as root:
            path=Path(root);record_attempt(path,'legacy',{'plan_status':'refused'})
            self.assertNotIn('structural_revalidation_contract',read_attempt_ledger(path)['legacy'])
            record_attempt(path,'new',{'plan_status':'held','structural_revalidation_contract':recovery.CONTRACT})
            record_attempt(path,'new',{'plan_status':'held'})
            self.assertEqual(read_attempt_ledger(path)['new']['structural_revalidation_contract'],recovery.CONTRACT)
