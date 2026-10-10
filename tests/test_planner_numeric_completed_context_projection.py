import copy,json,subprocess,unittest
from pathlib import Path
from dalton_core.research_planner import build_prompt,project_state_for_prompt,OUTPUT_SCHEMA,expand_numeric_waiting_context
from dalton_core.store import canonical_json,content_hash
from tests import test_research_planner as fixtures

class CompletedContextProjectionTests(unittest.TestCase):
    def context(self):
        return {'sources':[{'document_ref':'sec:filing:synthetic','document_version_hash':'a'*64,
            'numeric_result_hash':'b'*64,'admission_ref':'admission:synthetic','admission_hash':'c'*64,
            'targets':[{'target_ref':'target:rpo','metric_ref':'metric:rpo',
                'metric_definition':{'label':'Consolidated RPO','meaning':'Total issuer obligations, not segment-only amounts or maturity buckets.'},
                'metric_definition_hash':'d'*64,'period':'FY2025','unit':'currency','currency':'USD','basis':'gaap-reported',
                'figure_ref':'figure:synthetic','figure_hash':'e'*64}]}],'omitted':0}
    def test_every_company_keeps_full_completed_definitions_and_source_figure_bindings(self):
        helper=fixtures.FeedbackProjectionTests();state=helper.worst_case(4);context=self.context()
        for c in state['companies']:c['numeric_completed_context']=copy.deepcopy(context)
        state=helper.rebind(state);original=canonical_json(state)
        p=project_state_for_prompt(state,max_input_bytes=64000)
        self.assertEqual(p['content_hash'],state['content_hash']);self.assertEqual(canonical_json(state),original)
        self.assertEqual([c['company_ref'] for c in p['companies']],[c['company_ref'] for c in state['companies']])
        for c in p['companies']:self.assertEqual(c['numeric_completed_context'],context)
        self.assertLessEqual(len(build_prompt(p).encode()),64000)
        prompt=build_prompt(p)
        self.assertIn('not to automatically close different metric definitions or rejection bindings',prompt)
        self.assertIn('aggregate or average does not prove narrower business coverage or a complete maturity breakdown',prompt)
    def test_compact_instructions_keep_original_authority_and_financial_constraints(self):
        text=build_prompt({'companies':[]})
        for phrase in ('Claims do not replace originals','no preview does not mean missing material','original-language query_terms',
                       'not numeric/formula authority','ALL numeric_targets','period/unit/currency/basis',
                       'independent original verification is required','No identical paid retry',
                       'copy document/version/result hashes exactly','not a human gate','Inventing either voids the whole plan',
                       'invent no item or lower sufficient count','Never move its replaces_numeric_rejection binding across document/version',
                       'money uses unit currency with currency USD','a duration uses years with null currency'):
            self.assertIn(phrase,text)
        self.assertIn('OUTPUT_SCHEMA='+canonical_json(OUTPUT_SCHEMA),text)
        self.assertIn('Return raw strict JSON matching OUTPUT_SCHEMA',text)
