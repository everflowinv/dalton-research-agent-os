"""Strong zero-answer feedback informs planning, never grants admission itself."""
import copy
import unittest
from unittest.mock import patch
from dalton_core.directed_numeric_executor import numeric_feedback
from dalton_core.planner_numeric_requirements import numeric_waiting_targets
from dalton_core.research_planner import build_prompt, project_state_for_prompt, expand_numeric_waiting_context
from dalton_core.store import content_hash
from tests import test_numeric_waiting_target_revision as revision_fixture
from tests.test_research_planner import state

class PlannerRevisionFeedbackTests(unittest.TestCase):
    def fixture(self, **kwargs):
        h=revision_fixture.NumericWaitingRevisionTests('runTest');h.addCleanup=self.addCleanup
        return h,h.fixture(**kwargs)

    def test_strong_zero_answer_hint_keeps_old_work_bytes_and_reaches_prompt(self):
        h,(helper,f,a,parent,result,p,v)=self.fixture()
        before=f.store.connection.execute('SELECT record_json,content_hash FROM mission_document_research_admissions WHERE admission_id=?',(parent['id'],)).fetchone()
        work_before=list(f.store.connection.execute('SELECT work_order_id,work_order_json,work_order_hash FROM scheduler_work_orders'))
        rows=numeric_feedback(f.store.connection,f.mission['id'])
        self.assertTrue(rows[-1]['target_revision_candidate'])
        pending=numeric_waiting_targets(rows)
        self.assertTrue(all(r['target_revision_candidate'] for r in pending['targets']))
        held=state();held['companies'][0]['numeric_waiting_targets']=pending
        projected=project_state_for_prompt(held,max_input_bytes=64000)
        self.assertEqual(expand_numeric_waiting_context(projected['companies'][0]['numeric_waiting_targets']),pending)
        prompt=build_prompt(projected)
        for anchor in ('target_revision_candidate=true','real period/unit/currency/basis change',
                       'planning hint, never permission','target_revision_proof','unconsumed-result',
                       'Renaming target_ref/metric_ref','completed/partial results cannot qualify',
                       'calculated YoY needs a separate calculation task','broader question stay open',
                       'only changed original bytes or genuinely new verified excerpts','No identical paid retry'):
            self.assertIn(anchor,prompt)
        self.assertEqual(tuple(before),tuple(f.store.connection.execute('SELECT record_json,content_hash FROM mission_document_research_admissions WHERE admission_id=?',(parent['id'],)).fetchone()))
        self.assertEqual([tuple(r) for r in work_before],[tuple(r) for r in f.store.connection.execute('SELECT work_order_id,work_order_json,work_order_hash FROM scheduler_work_orders')])
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_consumption_and_withdrawal_remove_candidate_without_replenishing_it(self):
        h,(helper,f,a,parent,result,p,v)=self.fixture()
        self.assertTrue(numeric_feedback(f.store.connection,f.mission['id'])[-1]['target_revision_candidate'])
        admitted=h.admit(helper,f,a,parent,h.inquiry(parent))
        self.assertNotIn('target_revision_candidate',numeric_feedback(f.store.connection,f.mission['id'])[-1])
        from dalton_core.mission_document_research_withdrawal import MissionDocumentWithdrawalAuthority
        MissionDocumentWithdrawalAuthority(f.store).withdraw(admitted['admission_ref'],actor_ref='human:owner',reason='Synthetic closure.')
        self.assertNotIn('target_revision_candidate',numeric_feedback(f.store.connection,f.mission['id'])[-1])
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_partial_and_missing_formal_evidence_never_get_candidate(self):
        h,(helper,f,a,parent,result,p,v)=self.fixture(zero=False,percent=False)
        self.assertNotIn('target_revision_candidate',numeric_feedback(f.store.connection,f.mission['id'])[-1])
        class MissingFormal:
            def execute(self,sql,args=()):
                if sql.startswith('SELECT result_envelope_json,result_envelope_hash,terminal_state'):
                    return f.store.connection.execute('SELECT 1 WHERE 0')
                return f.store.connection.execute(sql,args)
        self.assertEqual(numeric_feedback(MissingFormal(),f.mission['id']),[])

    def test_candidate_flag_cannot_be_added_without_feedback_hash_proof(self):
        h,(helper,f,a,parent,result,p,v)=self.fixture()
        rows=numeric_feedback(f.store.connection,f.mission['id'])
        self.assertTrue(rows[-1]['target_revision_candidate'])
        forged=copy.deepcopy(rows[-1]);forged['target_revision_candidate']=False
        with self.assertRaisesRegex(ValueError,'feedback hash differs'):
            numeric_waiting_targets([forged])
        from dalton_core.numeric_target_revision import revision_source_candidate
        repaired=copy.deepcopy(parent)
        repaired['numeric_authority']['target_revision_proof']={'synthetic':'already revised'}
        self.assertFalse(revision_source_candidate(f.store.connection,repaired,result['numeric_result'],result['numeric_result']['content_hash']))
