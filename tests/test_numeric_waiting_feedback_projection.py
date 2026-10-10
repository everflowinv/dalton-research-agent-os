import copy
import unittest
from dalton_core.store import content_hash
from dalton_core.planner_numeric_requirements import numeric_waiting_targets
from dalton_core.research_planner import build_prompt, project_state_for_prompt, ResearchPlanInputTooLarge, expand_numeric_waiting_context
from tests import test_directed_numeric_workflow as numeric_fixtures
from tests import test_research_planner as fixtures

class NumericWaitingProjectionTests(unittest.TestCase):
    def observation(self, *, complete=False, count=1, reason='The original contains context tags, not a disclosed amount.', date='2026-10-10T18:00:00+00:00'):
        targets=[{'target_ref':f'target:{n}','metric_ref':f'metric:{n}','period':'FY2025','unit':'currency','currency':'USD','basis':'reported','metric_definition':{'label':'Amount','meaning':'Issuer amount'},'metric_definition_hash':'a'*64} for n in range(count)]
        value={'id':'feedback:'+date,'created_at':date,'outcome':'numeric_targets_completed' if complete else 'numeric_waiting_evidence',
               'numeric_targets':targets,'document_ref':'sec:filing:synthetic','document_version_hash':'b'*64,'numeric_result_hash':'c'*64,
               'completed_targets':[{'target_ref':t['target_ref']} for t in targets] if complete else [],
               'missing':[] if complete else [{'target_ref':t['target_ref'],'reason':reason} for t in targets]}
        return {**value,'content_hash':content_hash(value)}

    def test_bounded_reason_excerpts_omissions_and_complete_targets_clear_waiting(self):
        waiting=self.observation(count=6,reason='Context tags are not amounts. '*20)
        compact=numeric_waiting_targets([waiting])
        self.assertEqual((len(compact['targets']),compact['omitted']),(4,2))
        self.assertTrue(compact['omitted_hash'])
        for row in compact['targets']:
            self.assertTrue(row['reason_excerpted']);self.assertEqual(len(row['reason']),180)
            self.assertEqual(row['full_reason_hash'],content_hash(waiting['missing'][0]['reason']))
            self.assertEqual(row['document_version_hash'],'b'*64)
        complete=self.observation(complete=True,count=6,date='2026-10-10T19:00:00+00:00')
        self.assertEqual(numeric_waiting_targets([waiting,complete]),{'targets':[],'omitted':0})
        other=copy.deepcopy(complete)
        for t in other['numeric_targets']:t['period']='FY2026'
        other.pop('content_hash');other['content_hash']=content_hash(other)
        self.assertEqual(len(numeric_waiting_targets([waiting,other])['targets']),4)

    def test_waiting_identity_wakes_once_and_completion_clears_it(self):
        from dalton_core.research_state import planning_core_hash
        def state(rows):
            return fixtures.state(document_research_feedback={"status":"available",
                "by_company":{fixtures.ACN:rows}})
        waiting=self.observation()
        old=state([]);new=state([waiting])
        self.assertNotEqual(planning_core_hash(old),planning_core_hash(new))
        repeated=copy.deepcopy(waiting)
        repeated.update(id='feedback:another-plan',plan_ref='plan:later',created_at='2026-10-10T19:00:00+00:00')
        repeated.pop('content_hash');repeated['content_hash']=content_hash(repeated)
        self.assertEqual(planning_core_hash(new),planning_core_hash(state([repeated])))
        completed=self.observation(complete=True,date='2026-10-10T20:00:00+00:00')
        done=state([waiting,completed])
        self.assertNotEqual(planning_core_hash(new),planning_core_hash(done))
        self.assertEqual(planning_core_hash(old),planning_core_hash(done))

    def test_feedback_hash_drift_is_rejected_and_missing_exact_source_is_not_guessed(self):
        row=self.observation();row['missing'][0]['reason']='tampered'
        with self.assertRaisesRegex(ValueError,'hash differs'):numeric_waiting_targets([row])
        row=self.observation();row.pop('document_version_hash');row.pop('content_hash');row['content_hash']=content_hash(row)
        with self.assertRaisesRegex(ValueError,'exact source'):numeric_waiting_targets([row])
        self.assertEqual(numeric_waiting_targets([{'outcome':'query_miss','missing':['not a numeric receipt']}]),{'targets':[],'omitted':0})

    def test_strong_execution_feedback_is_required_before_diagnostic_enters_company(self):
        harness=numeric_fixtures.NumericWorkflowTests('runTest');harness.addCleanup=self.addCleanup
        f,authority,admission,executor,producer,verifier=harness.fixture(missing=True)
        harness.drive(executor,admission)
        from dalton_core.document_research_feedback import read_document_research_feedback
        from dalton_core.research_state import company_state
        feedback=read_document_research_feedback(f.store,f.mission)
        rows=feedback['by_company'][admission['company_ref']]
        held=company_state({'company_ref':admission['company_ref'],'items':[]},document_research_feedback=rows)
        diagnostic=held['numeric_waiting_targets']['targets'][0]
        self.assertEqual(diagnostic['target_ref'],'target:leases')
        self.assertEqual(diagnostic['document_version_hash'],admission['request']['registration']['content_hash'])
        self.assertEqual(diagnostic['reason'],next(row for row in rows if row.get('outcome')=='numeric_waiting_evidence')['missing'][0]['reason'])
        # Valid receipt without the bound final successful local result is not feedback.
        from dalton_core.directed_numeric_executor import numeric_feedback
        class UnsettledView:
            def execute(inner, sql, args=()):
                if sql.startswith('SELECT result_envelope_json,result_envelope_hash,terminal_state'):
                    return executor.connection.execute('SELECT 1 WHERE 0')
                return executor.connection.execute(sql, args)
        self.assertEqual(numeric_feedback(UnsettledView(),f.mission['id']),[])

    def test_pending_diagnostics_survive_zero_feedback_and_all_company_compression(self):
        helper=fixtures.FeedbackProjectionTests();state=helper.worst_case(4)
        for company in state['companies']:
            company['numeric_waiting_targets']=numeric_waiting_targets([self.observation()])
        state=helper.rebind(state)
        projected=project_state_for_prompt(state,max_input_bytes=64000)
        self.assertLessEqual(len(build_prompt(projected).encode()),64000)
        self.assertEqual(len(projected['companies']),4)
        self.assertEqual(projected['content_hash'],state['content_hash'])
        self.assertIn('context tags, not a disclosed amount',build_prompt(projected))
        for old,new in zip(state['companies'],projected['companies']):
            self.assertEqual(old['numeric_waiting_targets'],expand_numeric_waiting_context(new['numeric_waiting_targets']))
        with self.assertRaises(ResearchPlanInputTooLarge):project_state_for_prompt(state,max_input_bytes=1)

if __name__=='__main__':unittest.main()
