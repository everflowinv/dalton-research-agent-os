import copy
import unittest
from unittest.mock import patch
from tests import test_research_task_numeric_refusal_isolation as fixtures
from tests import test_research_planner as planner_fixtures
from dalton_core.numeric_source_admission_feedback import read_numeric_source_feedback, OUTCOME
from dalton_core.document_research_feedback import read_document_research_feedback, document_research_feedback_signature
from dalton_core.research_state import planning_core_hash
from dalton_core.research_planner import build_prompt, project_state_for_prompt
from dalton_core.store import content_hash

class NumericSourceFeedbackTests(unittest.TestCase):
    def fixture(self):
        ftest=fixtures.NumericAdmissionRefusalIsolationTests('runTest');ftest.addCleanup=self.addCleanup
        f,authority,plan,runtime,kwargs,producer,verifier=ftest.fixture()
        registrations=authority.registration_resolver.__self__
        def inventory(**kwargs):
            refs=kwargs['selected_document_refs']
            self.assertLessEqual(len(refs),6)
            rows=list(registrations.values())
            return {'registration_by_hash':{r['content_hash']:r for r in rows}}
        return f,authority,plan,inventory,producer,verifier

    def test_selected_inventory_sql_and_materialization_are_bounded_to_exact_refs(self):
        from dalton_core.document_research_inventory import inventory_with_registry
        f,authority,plan,inventory,producer,verifier=self.fixture()
        ref=plan['inquiries'][0]['directed_document']['document_ref'];queries=[]
        f.store.connection.set_trace_callback(queries.append)
        try:
            with patch.object(authority.registry,'inspect_acquired_document',wraps=authority.registry.inspect_acquired_document) as inspect:
                result=inventory_with_registry(core=f.store,mission=f.mission,registry=authority.registry,
                    purpose=authority.registry.policy['allowed_purposes'][0],selected_document_refs=(ref,))
                self.assertEqual(inspect.call_count,1)
                self.assertEqual([r['document_ref'] for r in result['registration_by_hash'].values()],[ref])
                selected_queries=[sql for sql in queries if 'SELECT record_id,mission_version_ref,company_ref' in sql]
                self.assertTrue(selected_queries)
                self.assertTrue(all('AND document_ref IN (' in sql for sql in selected_queries))
                inspect.reset_mock();queries.clear()
                empty=inventory_with_registry(core=f.store,mission=f.mission,registry=authority.registry,
                    purpose=authority.registry.policy['allowed_purposes'][0],selected_document_refs=())
                self.assertEqual(inspect.call_count,0)
                self.assertEqual(empty['registration_by_hash'],{})
                self.assertFalse(any('SELECT record_id,mission_version_ref,company_ref' in sql for sql in queries))
        finally:f.store.connection.set_trace_callback(None)

    def test_strong_transcript_grade_refusal_is_feedback_not_answer_and_filed_is_not_refused(self):
        f,authority,plan,inventory,producer,verifier=self.fixture()
        result=read_numeric_source_feedback(f.store,f.mission,inventory_loader=inventory)
        self.assertEqual(result['status'],'verified')
        self.assertEqual(len(result['records']),1)
        row=result['records'][0]
        self.assertEqual(row['outcome'],OUTCOME)
        self.assertEqual(row['plan_ref'],plan['plan_id'])
        self.assertEqual(row['plan_hash'],plan['content_hash'])
        self.assertEqual(row['source_grade'],'earnings-call-transcript')
        self.assertIn('separate numeric inquiry',row['missing_evidence'][0])
        self.assertIn('Do not move the old rejection binding',row['missing_evidence'][0])
        self.assertEqual((producer.calls,verifier.calls),(0,0))

    def test_missing_exact_version_is_unverified_not_source_grade_refusal(self):
        f,authority,plan,inventory,producer,verifier=self.fixture()
        result=read_numeric_source_feedback(f.store,f.mission,inventory_loader=lambda **kwargs:{'registration_by_hash':{}})
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['records'],[])
        self.assertEqual(len(result['unverified']),3)
        self.assertEqual((producer.calls,verifier.calls),(0,0))

    def test_tampered_registration_is_unverified_and_never_becomes_a_grade_verdict(self):
        f,authority,plan,inventory,producer,verifier=self.fixture()
        def tampered(**kwargs):
            value=copy.deepcopy(inventory(**kwargs))
            for registration in value['registration_by_hash'].values():
                registration['source_authority']['company_ref']='company:foreign'
            return value
        result=read_numeric_source_feedback(f.store,f.mission,inventory_loader=tampered)
        self.assertEqual(result['records'],[])
        self.assertEqual(result['status'],'partial')
        self.assertTrue(result['unverified'])
        self.assertEqual((producer.calls,verifier.calls),(0,0))

    def test_source_failure_does_not_erase_existing_valid_execution_feedback(self):
        f,authority,plan,inventory,producer,verifier=self.fixture()
        original={'id':'proved:one','mission_version_ref':f.mission['id'],'company_ref':plan['inquiries'][0]['company_ref'],'outcome':'no_verified_claim'}
        with patch('dalton_core.document_research_feedback._read_observations',return_value=[original]),patch('dalton_core.document_research_inventory.load_document_inventory_authority',side_effect=RuntimeError('source reader unavailable')):
            result=read_document_research_feedback(f.store,f.mission)
        self.assertEqual(result['status'],'available')
        self.assertEqual(result['by_company'][original['company_ref']],[original])
        self.assertEqual(result['admission_source_feedback_status']['status'],'unverified')

    def test_same_inquiry_and_source_new_plan_provenance_does_not_change_wake_or_pacing(self):
        f,authority,plan,inventory,producer,verifier=self.fixture()
        from tests.test_directed_numeric_workflow import NumericWorkflowTests
        from dalton_core.mission_document_research import _exact_plan
        with patch('dalton_core.document_research_inventory.load_document_inventory_authority',side_effect=inventory):
            before=read_document_research_feedback(f.store,f.mission);wake=document_research_feedback_signature(f.store)
            exact,_=_exact_plan(f.store.connection,plan['plan_id']);exact.pop('plan_id');exact.pop('content_hash');exact['state_hash']='d'*64;exact['inquiries'][0]['because']='Different planning commentary'
            exact['content_hash']=content_hash(exact)
            newplan=NumericWorkflowTests('runTest')._record_plan(f,exact)
            after=read_document_research_feedback(f.store,f.mission);newwake=document_research_feedback_signature(f.store)
        company=plan['inquiries'][0]['company_ref']
        self.assertNotEqual(before['by_company'][company][0]['plan_ref'],after['by_company'][company][0]['plan_ref'])
        self.assertEqual(before['by_company'][company][0]['id'],after['by_company'][company][0]['id'])
        self.assertEqual(wake,newwake)
        first=planner_fixtures.state(mission=f.mission,checklist=[planner_fixtures.entry(company,"SYN")],document_research_feedback=before)
        second=planner_fixtures.state(mission=f.mission,checklist=[planner_fixtures.entry(company,"SYN")],document_research_feedback=after)
        self.assertEqual(planning_core_hash(first),planning_core_hash(second))
        empty=planner_fixtures.state(mission=f.mission,checklist=[planner_fixtures.entry(company,"SYN")])
        self.assertNotEqual(planning_core_hash(first),planning_core_hash(empty))

    def test_projection_preserves_refusal_identity_and_guidance_with_original_wake_hash(self):
        f,authority,plan,inventory,producer,verifier=self.fixture()
        with patch('dalton_core.document_research_inventory.load_document_inventory_authority',side_effect=inventory):
            feedback=read_document_research_feedback(f.store,f.mission)
        company=plan['inquiries'][0]['company_ref']
        state=planner_fixtures.state(mission=f.mission,checklist=[planner_fixtures.entry(company,'SYN')],document_research_feedback=feedback)
        row=feedback['by_company'][plan['inquiries'][0]['company_ref']][0]
        row={**row,'execution_proof_blob':'x'*100000}
        subject=state['companies'][0];subject['document_research_feedback']=[row];subject['numeric_source_refusal_ids']=[row['id']]
        subject['numeric_source_refusals']=[{key:row[key] for key in ('id','document_ref','document_version_hash','reason','source_grade')}]
        subject['financial_model']=None  # no model authority in this numeric-source fixture
        subject['old_company_detail']='x'*100000
        projection=project_state_for_prompt(state,max_input_bytes=64000)
        projected_row=projection['companies'][0]['numeric_source_refusals'][0]
        self.assertEqual(projected_row['reason'],row['reason'])
        self.assertEqual(projected_row['document_version_hash'],row['document_version_hash'])
        self.assertEqual(projected_row['source_grade'],row['source_grade'])
        self.assertEqual(planning_core_hash(state),planning_core_hash(projection))
        self.assertIn('numeric_admission_source_grade_refused',build_prompt(projection))
        self.assertIn('Never move its replaces_numeric_rejection binding across document/version',build_prompt(projection))
        self.assertIn('keep that task blocked and ask a separate numeric inquiry from eligible company-filed material',build_prompt(projection))
