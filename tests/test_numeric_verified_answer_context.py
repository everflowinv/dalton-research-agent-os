import copy
import unittest
from dalton_core.directed_numeric_executor import numeric_feedback
from dalton_core.planner_numeric_requirements import numeric_completed_context,numeric_requirements
from dalton_core.store import content_hash
from tests import test_directed_numeric_workflow as fixture
from tests import test_numeric_completed_context as context_fixture

class NumericVerifiedAnswerContextTests(unittest.TestCase):
    def setup_completed(self):
        h=fixture.NumericWorkflowTests('runTest');h.addCleanup=self.addCleanup
        f,authority,admission,executor,producer,verifier=h.fixture();h.drive(executor,admission)
        pending=context_fixture.requirement(admission['request']['registration']['document_ref'],admission['request']['registration']['content_hash'])
        pending['company_ref']=admission['company_ref']
        return f,admission,executor,pending

    def test_real_complete_values_quote_and_scope_are_read_only_planning_context(self):
        f,a,e,pending=self.setup_completed();before=e.connection.total_changes
        rows=numeric_feedback(e.connection,f.mission['id']);context=numeric_completed_context([pending,*rows],company_ref=a['company_ref'])
        self.assertEqual(e.connection.total_changes,before)
        targets=context['sources'][0]['targets'];self.assertEqual(len(targets),3)
        for t in targets:
            answer=t['verified_answer'];figure=e.connection.execute('SELECT * FROM coverage_mission_document_figures WHERE figure_id=?',(t['figure_ref'],)).fetchone()
            for key in ['value','unit','currency','scale','period','basis','as_reported_label','source_grade','quote_id']:
                self.assertEqual(answer[key],figure[key])
            start=answer['citation_excerpt_start'];self.assertEqual(answer['citation_excerpt'],figure['citation_text'][start:start+512])
            self.assertEqual(answer['citation_text_hash'],content_hash(figure['citation_text']))
        self.assertEqual(numeric_requirements([pending,*rows]),numeric_requirements([pending]))

    def test_bound_figure_drift_is_refused_not_used_as_known_answer(self):
        f,a,e,pending=self.setup_completed();ref=numeric_feedback(e.connection,f.mission['id'])[0]['completed_targets'][0]['figure_ref']
        triggers=e.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='coverage_mission_document_figures'").fetchall()
        for t in triggers:e.connection.execute('DROP TRIGGER '+t[0])
        e.connection.execute('UPDATE coverage_mission_document_figures SET value=? WHERE figure_id=?',('999999',ref))
        with self.assertRaises(Exception):numeric_feedback(e.connection,f.mission['id'])

    def test_rehashed_feedback_cannot_change_answer_scope_binding(self):
        f,a,e,pending=self.setup_completed();row=copy.deepcopy(numeric_feedback(e.connection,f.mission['id'])[0]);row['completed_answers'][0]['period']='FY2040'
        row.pop('content_hash');row['content_hash']=content_hash(row)
        with self.assertRaisesRegex(ValueError,'answer scope differs'):
            numeric_completed_context([pending,row],company_ref=a['company_ref'])

    def test_unsettled_final_result_does_not_expose_approved_answer(self):
        f,a,e,pending=self.setup_completed()
        class Unsettled:
            def execute(self,sql,args=()):
                if sql.startswith('SELECT result_envelope_json,result_envelope_hash,terminal_state'):
                    return e.connection.execute('SELECT 1 WHERE 0')
                return e.connection.execute(sql,args)
        self.assertEqual(numeric_feedback(Unsettled(),f.mission['id']),[])

    def test_legacy_feedback_without_values_remains_readable(self):
        rows=[context_fixture.requirement(),context_fixture.completed()]
        target=numeric_completed_context(rows,company_ref=context_fixture.COMPANY)['sources'][0]['targets'][0]
        self.assertNotIn('verified_answer',target)

    def test_final_projected_prompt_keeps_actual_answers_without_unresolved_rejection(self):
        from dalton_core.research_planner import project_state_for_prompt, build_prompt
        from tests import test_research_planner as planner_fixture
        f,a,e,_=self.setup_completed()
        rows=numeric_feedback(e.connection,f.mission['id'])
        state=planner_fixture.state(checklist=[planner_fixture.entry(a['company_ref'],'SYNTH',planner_fixture.item('quarterly_financials'))],document_research_feedback={'status':'available','by_company':{a['company_ref']:rows}})
        projected=project_state_for_prompt(state,max_input_bytes=128000)
        company=next(c for c in projected['companies'] if c['company_ref']==a['company_ref'])
        context=company['numeric_completed_context'];self.assertEqual(sum(len(s['targets']) for s in context['sources']),3)
        self.assertEqual(numeric_requirements(rows)['requirements'],[])
        prompt=build_prompt(projected)
        for source in context['sources']:
            self.assertIn(source['document_version_hash'],prompt)
            for t in source['targets']:
                self.assertIn(t['figure_hash'],prompt);self.assertIn(t['verified_answer']['citation_text_hash'],prompt)
        self.assertLessEqual(len(prompt.encode()),128000)
