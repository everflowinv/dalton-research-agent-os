import copy
import unittest
from dalton_core.store import content_hash,canonical_json
from dalton_core.research_planner import (project_state_for_prompt,build_prompt,
    _compact_numeric_waiting_context,expand_numeric_waiting_context,ResearchPlanError)
from tests import test_research_planner as fixtures
from tests import test_numeric_waiting_feedback_projection as waiting_fixtures
from dalton_core.planner_numeric_requirements import numeric_waiting_targets

class DescriptiveAuditCompactionTests(unittest.TestCase):
    def context(self):
        return numeric_waiting_targets([waiting_fixtures.NumericWaitingProjectionTests('runTest').observation(count=3,reason='Missing exact original amounts, not a numeric disclosure.')])

    def test_shared_source_bindings_are_exact_and_unambiguous(self):
        original=self.context();compacted=_compact_numeric_waiting_context(original)
        self.assertEqual(len(compacted['source_bindings']),1)
        self.assertTrue(all(t['source_binding_index']==0 for t in compacted['targets']))
        self.assertEqual(compacted['binding_schema'],'planner-numeric-waiting-source-bindings:0.1')
        self.assertEqual(expand_numeric_waiting_context(compacted),original)
        different=copy.deepcopy(original);different['targets'][1]['document_version_hash']='d'*64
        self.assertEqual(expand_numeric_waiting_context(_compact_numeric_waiting_context(different)),different)
        for field in ('document_ref','document_version_hash','numeric_result_hash'):
            self.assertEqual(compacted['source_bindings'][0][field],original['targets'][0][field])
        self.assertIn("same company's source_bindings",build_prompt({'companies':[]}))

    def test_invalid_source_index_or_conflicting_binding_is_refused(self):
        compacted=_compact_numeric_waiting_context(self.context())
        for index in (-1,True,2,'0'):
            bad=copy.deepcopy(compacted);bad['targets'][0]['source_binding_index']=index
            with self.assertRaises(ResearchPlanError):expand_numeric_waiting_context(bad)
        bad=copy.deepcopy(compacted);bad['targets'][0]['document_version_hash']='e'*64
        with self.assertRaises(ResearchPlanError):expand_numeric_waiting_context(bad)
        bad=copy.deepcopy(compacted);bad['binding_schema']='unsupported:0.2'
        with self.assertRaises(ResearchPlanError):expand_numeric_waiting_context(bad)

    def test_final_stage_keeps_company_requirements_and_full_source_hashes(self):
        helper=fixtures.FeedbackProjectionTests();state=helper.worst_case(4)
        for company in state['companies']:
            company['numeric_waiting_targets']=self.context()
            for doc in company['readable_documents']:doc['title']='Descriptive original report title '*100
        state=helper.rebind(state);before=canonical_json(state)
        baseline=project_state_for_prompt(state,max_input_bytes=64000)
        bound=min(64000,len(build_prompt(baseline).encode())+1024)
        projected=project_state_for_prompt(state,max_input_bytes=bound)
        self.assertIn('deduplicate_descriptive_audit',projected['prompt_projection']['stages_applied'])
        self.assertLessEqual(len(build_prompt(projected).encode()),64000)
        self.assertEqual(before,canonical_json(state));self.assertEqual(projected['content_hash'],state['content_hash'])
        self.assertEqual([c['company_ref'] for c in projected['companies']],[c['company_ref'] for c in state['companies']])
        for old,new in zip(state['companies'],projected['companies']):
            self.assertEqual(old['numeric_waiting_targets'],expand_numeric_waiting_context(new['numeric_waiting_targets']))
            for doc in new['readable_documents']:
                held=next(d for d in old['readable_documents'] if (d['document_ref'],d['document_version_hash'])==(doc['document_ref'],doc['document_version_hash']))
                for key in ('document_ref','document_version_hash','source_ref','doc_date','doc_kind','readability','readable','operations'):
                    if key in doc:self.assertEqual(doc[key],held[key])
                self.assertNotIn('title',doc)
            self.assertGreater(new['omitted_titles']['count'],0);self.assertTrue(new['omitted_titles']['content_hash'])
            self.assertTrue(new['historical_counts']['content_hash'])
            self.assertTrue(new['historical_omissions']['content_hash'])
