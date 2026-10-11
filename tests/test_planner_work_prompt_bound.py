"""Storage bytes and model-call authority are separate finite contracts."""
import copy
import json
import unittest
from datetime import datetime,timezone
from unittest.mock import patch
from dalton_core.cockpit_model import build_work,CockpitModelError
from dalton_core.research_planner import build_prompt,project_state_for_prompt,ResearchPlanInputTooLarge,PROMPT_PROJECTION_REF
from dalton_core.research_planner_cli import MAX_WORK_ORDER_PROMPT_BYTES,build_state,attempt_hold,record_attempt,read_attempt_ledger
from dalton_core.store import content_hash
from tests import test_research_planner_cli as fixtures

class PlannerStorageBoundTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.PlannerChildTests();self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.addCleanup(lambda:self.fx.store.close())
        self.mission=self.fx.publish_mission()
        self.base=build_state(self.fx.store,self.fx.missions,self.mission,
            plans_dir=self.fx.state/'discovery-plans',as_of='2026-09-11T12:01:00+00:00')
        self.config=self.fx.root/'model.json';self.config.write_text('{}')

    def inventory(self,count):
        s=copy.deepcopy(self.base)
        s['reflection_followup_context']={'groups':[],'backlog_questions':[
            {'company_ref':s['companies'][-1]['company_ref'],
             'question_ref':f'question:synthetic:{i}',
             'question':'What source-bound fact remains unresolved? '+'Q'*430,'state':'open'}
             for i in range(count)]}
        return self.rebind(s)

    @staticmethod
    def rebind(s):
        context=s.get('reflection_followup_context')
        if context is not None:
            context.pop('content_hash',None);context['content_hash']=content_hash(context)
        s.pop('content_hash',None);s['content_hash']=content_hash(s);return s

    def exact_64844(self):
        s=self.inventory(55)
        measured=len(build_prompt(project_state_for_prompt(s,max_input_bytes=MAX_WORK_ORDER_PROMPT_BYTES)).encode())
        remaining=64844-measured
        self.assertGreaterEqual(remaining,0)
        questions=s['reflection_followup_context']['backlog_questions']
        for i in range(remaining):questions[i%len(questions)]['question']+='Q'
        self.assertTrue(all(len(q['question'])<=600 for q in questions))
        s=self.rebind(s)
        self.assertEqual(len(build_prompt(project_state_for_prompt(s,max_input_bytes=MAX_WORK_ORDER_PROMPT_BYTES)).encode()),64844)
        return s

    def run_with_budget(self,state,input_tokens):
        budget={'max_input_tokens':input_tokens,'max_output_tokens':16384,'max_cost_usd':1.,'timeout_seconds':300}
        self.config.write_text(json.dumps({'purpose_call_budgets':{'plan':budget}}))
        built_works=[]
        def call(**kwargs):
            work=build_work(purpose='plan',request_id=kwargs['request_id'],prompt=kwargs['prompt'],
                 mission_version_ref=self.mission['id'],max_input_tokens=budget['max_input_tokens'],
                 max_output_tokens=budget['max_output_tokens'],max_cost_usd=budget['max_cost_usd'],max_seconds=300)
            built_works.append(work)
            return {'text':json.dumps({'schema_version':'0.1','assessment':'Prepared a bounded plan.',
                     'directives':[],'inquiries':[],'sufficiency':[]}),
                    'cost_micros':0,'replayed':False}
        # This fixture has no real followup groups to consume. Its empty observer
        # is outside the storage/Work-admission contract tested here.
        with patch('dalton_core.research_followup_automation.FollowupAutomationAuthority.observe_planner',return_value=[]),patch('dalton_core.research_planner_cli.build_state',return_value=state),patch('dalton_core.research_planner_cli.CockpitModel')as model:
            model.return_value.budget_for.return_value=budget
            model.return_value.call.side_effect=call
            summary=self.fx.plan(dry_run=False,model_config_path=self.config)
            calls=model.return_value.call.call_count
        return summary,built_works,calls

    def test_current_64844_size_reaches_normal_work_with_all_company_and_open_question_identity(self):
        state=self.exact_64844();before=copy.deepcopy(state)
        summary,works,calls=self.run_with_budget(state,250000)
        self.assertEqual(summary['plan_status'],'fresh');self.assertEqual(calls,1)
        self.assertEqual(summary['work_order_prompt_bound'],128000)
        self.assertEqual(summary['model_max_input_tokens'],250000)
        self.assertEqual(summary['prompt_bytes'],64844)
        self.assertEqual(works[0].budget['max_input_tokens'],250000)
        # The actual Work carries every company and every original question;
        # none was removed merely to fit the former 64KB ceiling.
        prompt=works[0].question
        for company in state['companies']:self.assertIn(company['company_ref'],prompt)
        for q in state['reflection_followup_context']['backlog_questions']:
            self.assertIn(q['question_ref'],prompt);self.assertIn(q['question'],prompt)
        self.assertEqual(state,before)

    def test_smaller_model_authority_limits_projection_and_real_work_admission(self):
        state=self.exact_64844()
        summary,works,calls=self.run_with_budget(state,2048)
        self.assertEqual(summary['plan_status'],'input_too_large')
        self.assertEqual(summary['prompt_input']['configured_max_input_bytes'],2048)
        self.assertEqual(works,[]);self.assertEqual(calls,0)
        prompt=build_prompt(project_state_for_prompt(state,max_input_bytes=128000))
        with self.assertRaisesRegex(CockpitModelError,'exceed the model input bound'):
            build_work(purpose='plan',request_id='too-small',prompt=prompt,
                mission_version_ref=self.mission['id'],max_input_tokens=64000,
                max_output_tokens=16384,max_cost_usd=1.,max_seconds=300)

    def test_default_eighty_thousand_input_does_not_fill_preview_beyond_its_authority(self):
        summary,works,calls=self.run_with_budget(self.inventory(80),80000)
        self.assertEqual(summary['plan_status'],'fresh');self.assertEqual(calls,1)
        self.assertLessEqual(summary['prompt_bytes'],80000)
        self.assertEqual(summary['work_order_prompt_bound'],128000)
        self.assertEqual(summary['conservative_work_input_byte_bound'],80000)
        self.assertEqual(works[0].budget['max_input_tokens'],80000)

    def test_oversized_required_inventory_refuses_then_holds_without_any_model_work(self):
        state=self.inventory(300)
        summary,works,calls=self.run_with_budget(state,250000)
        self.assertEqual(summary['plan_status'],'input_too_large');self.assertEqual(calls,0);self.assertEqual(works,[])
        self.assertEqual(summary['prompt_input']['configured_max_input_bytes'],128000)
        self.assertTrue(summary['prompt_input']['document_identities_preserved'])
        again,works,calls=self.run_with_budget(state,250000)
        self.assertEqual(again['plan_status'],'held');self.assertEqual(calls,0);self.assertEqual(works,[])

    def test_changed_input_budget_reprojects_old_size_failure_once(self):
        state=self.exact_64844()
        self.config.write_text(json.dumps({"purpose_call_budgets":{"plan":{"max_input_tokens":2048}}}))
        first,works,calls=self.run_with_budget(state,2048)
        self.assertEqual(first['plan_status'],'input_too_large')
        record=read_attempt_ledger(self.fx.state)[state['content_hash']]
        self.assertEqual(record['conservative_work_input_byte_bound'],2048)
        same,works,calls=self.run_with_budget(state,2048)
        self.assertEqual(same['plan_status'],'held');self.assertEqual(calls,0)
        self.config.write_text(json.dumps({"purpose_call_budgets":{"plan":{"max_input_tokens":250000}}}))
        next_run,works,calls=self.run_with_budget(state,250000)
        self.assertEqual(next_run['plan_status'],'fresh');self.assertEqual(calls,1)
        again,works,calls=self.run_with_budget(state,250000)
        self.assertEqual(again['plan_status'],'unchanged');self.assertEqual(calls,0)
        now=datetime.now(timezone.utc)
        refused={'at':now.isoformat(),'plan_status':'refused',
                 'conservative_work_input_byte_bound':2048,'attempts':1}
        self.assertEqual(attempt_hold({'s':refused},'s',now=now,input_byte_bound=250000)['held_reason'],
                         'terminal_for_this_state')

    def test_old_storage_failure_reopens_once_and_current_contract_failure_remains_held(self):
        now=datetime.now(timezone.utc)
        old={'at':now.isoformat(),'status':'succeeded','plan_status':'input_too_large',
             'projection_rule_ref':PROMPT_PROJECTION_REF,'attempts':1}
        self.assertIsNone(attempt_hold({'state':old},'state',now=now))
        self.assertIsNone(attempt_hold({'state':{**old,'work_order_prompt_bound':64000}},'state',now=now))
        record_attempt(self.fx.state,'state',{'created_at':now.isoformat(),'status':'succeeded','plan_status':'input_too_large'})
        held=read_attempt_ledger(self.fx.state)
        self.assertEqual(held['state']['work_order_prompt_bound'],128000)
        self.assertEqual(attempt_hold(held,'state',now=now)['held_reason'],'terminal_for_this_state')
        # A content refusal under the old bound is not reopened by a storage fix.
        self.assertEqual(attempt_hold({'state':{**old,'plan_status':'refused'}},'state',now=now)['held_reason'],'terminal_for_this_state')

if __name__=='__main__':unittest.main()
