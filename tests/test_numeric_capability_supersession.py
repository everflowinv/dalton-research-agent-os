import copy
import unittest
from unittest.mock import patch
from tests import test_directed_numeric_workflow as workflow
from dalton_core.numeric_capability_supersession import successor_for
from dalton_core.document_research_feedback import read_document_research_feedback,document_research_feedback_signature
from dalton_core.directed_numeric_research import numeric_rejection
from dalton_core.planner_numeric_requirements import numeric_requirements

class SupersessionTests(unittest.TestCase):
    def fixture(self,company=None,**kwargs):
        h=workflow.NumericWorkflowTests('runTest');self.addCleanup(h.doCleanups)
        if company is None:f,authority,admission,executor,producer,verifier=h.fixture(old_rejection=True,**kwargs)
        else:
            from tests import test_mission_annual_research as annual
            from tests import test_research_plan_annual_report as source
            with patch.object(workflow,'COMPANY',company),patch.object(annual,'COMPANY',company),patch.object(source,'COMPANY',company):
                f,authority,admission,executor,producer,verifier=h.fixture(old_rejection=True,**kwargs)
        binding=admission['planner_inquiry']['directed_document']['replaces_numeric_rejection']
        rejection=numeric_rejection(f.store.connection,binding['admission_ref'])
        return h,f,authority,admission,executor,producer,verifier,rejection

    def test_real_admitted_numeric_upgrade_suppresses_only_ancestor_operation_and_stays_pending(self):
        h,f,authority,a,executor,producer,verifier,rejection=self.fixture();before=f.store.connection.total_changes
        with patch('dalton_core.numeric_capability_supersession.successor_for',return_value=None):
            old_signature=document_research_feedback_signature(f.store)
        proof=successor_for(f.store.connection,rejection,mission=f.mission)
        self.assertEqual(proof['successor_admission_ref'],a['id']);self.assertEqual(proof['research_status'],'pending')
        self.assertFalse(proof['financial_evidence']);self.assertEqual((producer.calls,verifier.calls),(0,0))
        feedback=read_document_research_feedback(f.store,f.mission);self.assertEqual(feedback['status'],'available')
        rows=feedback['by_company'][a['company_ref']]
        ancestor=[r for r in rows if r.get('admission_ref')==rejection['admission_ref']]
        self.assertTrue(ancestor);self.assertTrue(all(r['numeric_upgrade_status']=='superseded_by_admitted_numeric_read' for r in ancestor))
        self.assertFalse(numeric_requirements(ancestor)['requirements'])
        self.assertTrue(all(r['outcome']!='numeric_targets_completed' for r in ancestor))
        self.assertEqual(f.store.connection.total_changes,before)
        self.assertEqual(successor_for(f.store.connection,rejection,mission=f.mission),proof)
        wake=document_research_feedback_signature(f.store)
        self.assertNotEqual(wake,old_signature);self.assertEqual(document_research_feedback_signature(f.store),wake)

    def test_caller_rejection_and_foreign_mission_or_universe_do_not_suppress(self):
        _,f,_,a,_,_,_,rejection=self.fixture()
        bad={**rejection,'rejection_hash':'f'*64}
        with self.assertRaises(ValueError):successor_for(f.store.connection,bad,mission=f.mission)
        self.assertIsNone(successor_for(f.store.connection,rejection,mission={**f.mission,'id':'mission:foreign'}))
        self.assertIsNone(successor_for(f.store.connection,rejection,mission={**f.mission,'universe':[]}))

    def test_two_companies_use_exact_admitted_relation_without_shared_number_matching(self):
        for company in ('company:synthetic:cedar','company:synthetic:elm'):
            _,f,_,a,_,_,_,rejection=self.fixture(company)
            proof=successor_for(f.store.connection,rejection,mission=f.mission)
            self.assertEqual(proof['company_ref'],company)
            self.assertEqual(proof['successor_admission_ref'],a['id'])

    def test_real_completed_successor_keeps_existing_completion_semantics_and_no_pending_override(self):
        import dalton_core.numeric_capability_supersession as rule
        h,f,_,a,executor,producer,verifier,rejection=self.fixture()
        completed=h.drive(executor,a);self.assertEqual(completed['research_status'],'numeric_targets_completed')
        with patch.object(rule,'successor_for',side_effect=AssertionError('completed rejection needs no pending supersession')):
            feedback=read_document_research_feedback(f.store,f.mission)
        self.assertEqual(feedback['status'],'available')
        ancestors=[r for r in feedback['by_company'][a['company_ref']]if r.get('admission_ref')==rejection['admission_ref']]
        self.assertFalse(numeric_requirements(ancestors)['requirements'])
        self.assertTrue(all('numeric_upgrade_supersession' not in r for r in ancestors))

    def test_wrong_question_source_selected_proof_or_numeric_identity_never_suppresses(self):
        import dalton_core.numeric_capability_supersession as rule
        _,f,_,a,_,_,_,rejection=self.fixture();real=rule._admission
        for change in ('question','source','binding','frozen_proof','targets','source_binding','untyped'):
            def read(connection,ref):
                wire=real(connection,ref)
                if ref!=a['id']:return wire
                wire=copy.deepcopy(wire)
                if change=='question':wire['planner_inquiry']['question']='What is profit on the same FY2025 date?'
                elif change=='source':wire['planner_inquiry']['directed_document']['document_version_hash']='f'*64
                elif change=='binding':wire['planner_inquiry']['directed_document']['replaces_numeric_rejection']['rejection_hash']='e'*64
                elif change=='frozen_proof':wire['numeric_authority']['capability_upgrade_proofs']=[]
                elif change=='targets':wire['numeric_authority']['targets']=[]
                elif change=='source_binding':wire['numeric_authority']['source_binding']['company_ref']='company:foreign'
                else:wire['planner_inquiry']['directed_document']['strategy_version']='directed-document:0.1'
                return wire
            with self.subTest(change=change),patch.object(rule,'_admission',side_effect=read):
                try:proof=successor_for(f.store.connection,rejection,mission=f.mission)
                except ValueError:proof=None
                self.assertIsNone(proof)

    def test_withdrawn_or_overflow_successor_and_unproved_reader_keep_original_requirement(self):
        import dalton_core.numeric_capability_supersession as rule
        _,f,_,a,_,_,_,rejection=self.fixture()
        with patch.object(rule,'_withdrawn',side_effect=lambda c,ref:ref==a['id']):
            self.assertIsNone(successor_for(f.store.connection,rejection,mission=f.mission))
        with patch.object(rule,'MAX_CANDIDATES',0),self.assertRaisesRegex(ValueError,'bound'):
            successor_for(f.store.connection,rejection,mission=f.mission)
        with patch.object(rule,'successor_for',side_effect=ValueError('no proved successor')):
            feedback=read_document_research_feedback(f.store,f.mission)
        ancestor=[r for r in feedback['by_company'][a['company_ref']]if r.get('admission_ref')==rejection['admission_ref']]
        self.assertTrue(numeric_requirements(ancestor)['requirements'])
        self.assertTrue(all(r['numeric_upgrade_supersession_status']=='unverified' for r in ancestor))

    def test_failed_numeric_successor_preserves_exact_period_failure_and_original_fees(self):
        from dalton_core.directed_numeric_period_replacement import binding_for
        with patch('dalton_core.directed_numeric_research.assert_executable_target_periods'):
            _,f,_,a,executor,producer,verifier,rejection=self.fixture(
                period='文件所载最近报告期末',extra_text=' The disclosure is as of December 31, 2025.')
        with patch('dalton_core.directed_numeric_research.unexecutable_period_targets',return_value=[]),patch(
                'dalton_core.transcript_polish_model_worker.RoutedTranscriptPolishModelWorker._bounded_failure_status',return_value='failed'):
            for _ in range(8):
                executor.run_once(a['id'])
                if producer.calls:break
        for _ in range(20):
            result=executor.run_once(a['id'])
            if result['status'] in {'blocked','complete','stopped'}:break
        binding=binding_for(f.store.connection,a['id']);self.assertIsNotNone(binding)
        fees=[tuple(row)for row in f.budget.connection.execute('SELECT * FROM thesis_impact_day_admissions')]
        before=f.store.connection.total_changes
        feedback=read_document_research_feedback(f.store,f.mission)
        self.assertEqual(feedback['status'],'available',feedback)
        rows=feedback['by_company'][a['company_ref']]
        self.assertTrue(any(row.get('replaces_numeric_contract_failure')==binding for row in rows))
        ancestor=[row for row in rows if row.get('admission_ref')==rejection['admission_ref']]
        self.assertFalse(numeric_requirements(ancestor)['requirements'])
        self.assertTrue(all(row['numeric_upgrade_supersession']['research_status']=='pending'for row in ancestor))
        self.assertEqual((producer.calls,verifier.calls),(1,0))
        self.assertEqual(f.store.connection.total_changes,before)
        self.assertEqual([tuple(row)for row in f.budget.connection.execute('SELECT * FROM thesis_impact_day_admissions')],fees)
