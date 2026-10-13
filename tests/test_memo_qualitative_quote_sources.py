"""Qualitative quotes retain exact acquired sources without becoming numbers."""
import json
import unittest
from unittest.mock import patch
from dalton_core.evidence_index import make_passage_ref, passages_for_document
from dalton_core.investment_memo_draft import parse_group_output, draft_group, InvestmentMemoCitationError
from dalton_core.mission_deliverable import validate_passage_number, MissionDeliverableConflict
from tests import test_evidence_index as corpus_fixture


def payload(ref, text):
    return {'sections':[{'title':'Business','body':text,'claim_refs':[],
                         'numbers':[{'text':text,'passage_ref':ref}], 'gaps':[]}], 'key_questions':[]}


def parse(value, ref, source, **kwargs):
    return parse_group_output(value,section_titles=['Business'],questions=[],allowed_refs={ref},
                              material=[{'kind':'pack_passage','ref':ref,'text':source}],**kwargs)


class MemoQualitativeQuoteTests(unittest.TestCase):
    def setUp(self):
        self.source='Demand improved. Revenue was 22.2 billion.\n'
        self.ref=make_passage_ref('alphaengine-doc:fixture',0,len(self.source),self.source)

    def test_exact_quote_moves_to_sources_and_keeps_body(self):
        value=payload(self.ref,'Demand improved.')
        result=parse(value,self.ref,self.source)
        self.assertEqual(result['sections'][0],{'title':'Business','body':'Demand improved.',
                         'claim_refs':[],'numbers':[], 'gaps':[], 'source_refs':[{'ref':self.ref}]})
        self.assertNotIn('source_refs',value['sections'][0])

    def test_numbers_are_not_downgraded_even_when_unsupported(self):
        for text in ('22.2','999.2','1'):
            with self.subTest(text=text):
                result=parse(payload(self.ref,text),self.ref,self.source)['sections'][0]
                self.assertEqual(result['numbers'],[{'text':text,'passage_ref':self.ref}])
                self.assertNotIn('source_refs',result)
                if text!='22.2':
                    with self.assertRaises(MissionDeliverableConflict):
                        validate_passage_number(result['numbers'][0],lambda _:self.source)

    def test_paraphrase_translation_and_renderer_header_are_not_original_quotes(self):
        for text in ('Demand strengthened.','需求改善。','管理层发言'):
            with self.subTest(text=text),self.assertRaises(InvestmentMemoCitationError):
                parse(payload(self.ref,text),self.ref,self.source+'\n《Issuer》2026-01-01：管理层发言',
                      original_passages={self.ref:self.source})

    def test_trimmed_pack_without_original_cannot_authorize_quote(self):
        value=payload(self.ref,'Demand improved.')
        with self.assertRaises(InvestmentMemoCitationError):parse(value,self.ref,self.source.strip())
        result=parse(value,self.ref,self.source.strip(),original_passages={self.ref:self.source})
        self.assertEqual(result['sections'][0]['source_refs'],[{'ref':self.ref}])
        with self.assertRaises(InvestmentMemoCitationError):parse(value,self.ref,self.source,original_passages={})

    def test_open_shape_or_multiple_sources_cannot_be_hidden_by_normalization(self):
        for extra in ({'invented':'x'},{'number_ref':'number:x'}):
            value=payload(self.ref,'Demand improved.');value['sections'][0]['numbers'][0].update(extra)
            with self.subTest(extra=extra),self.assertRaises(ValueError):parse(value,self.ref,self.source)

    def test_duplicate_quotes_keep_one_source_and_preserve_numeric_output(self):
        value=payload(self.ref,'Demand improved.')
        value['sections'][0]['numbers']*=2
        value['sections'][0]['numbers'].append({'text':'22.2','passage_ref':self.ref})
        result=parse(value,self.ref,self.source)['sections'][0]
        self.assertEqual(result['source_refs'],[{'ref':self.ref}]);self.assertEqual(result['numbers'],[{'text':'22.2','passage_ref':self.ref}])

    def test_production_group_strong_reads_exact_original_once_and_preserves_call(self):
        f=corpus_fixture.IndexFixture('runTest');f.setUp();self.addCleanup(f.doCleanups)
        item=passages_for_document(f.state,f.refs['transcript'])[0]
        ref=item['passage_ref'];source=item['text'];quote='Julie Sweet'
        self.assertIn(quote,source)
        value=payload(ref,quote);value['sections'][0]['numbers']*=2
        class Model:
            scheduler_db=str(f.state/'scheduler.sqlite')
            def call(self,**kw):return {'text':json.dumps(value),'cost_micros':17,'work_order_ref':'work:existing'}
        from dalton_core.initial_screen_failure_feedback import _checked_originals
        with patch('dalton_core.initial_screen_failure_feedback._checked_originals',wraps=_checked_originals) as reader:
            result=draft_group(Model(),group='fixture',section_titles=['Business'],questions=[],
               material=[{'kind':'pack_passage','ref':ref,'text':source.strip()+'\n《Untrusted》：管理层发言'}],company={},mission={})
        self.assertEqual(result['status'],'drafted',result)
        self.assertEqual(result['sections'][0]['numbers'],[])
        self.assertEqual(result['sections'][0]['source_refs'],[{'ref':ref}])
        self.assertEqual(result['model']['cost_micros'],17);self.assertEqual(reader.call_count,1)
        self.assertEqual(reader.call_args.args[1],{ref})

    def test_original_failure_is_not_a_paid_semantic_repair(self):
        value=payload(self.ref,'Demand improved.')
        class Model:
            scheduler_db='/nonexistent/state/scheduler.sqlite'
            def call(self,**kw):return {'text':json.dumps(value),'cost_micros':17}
        with patch('dalton_core.initial_screen_failure_feedback._checked_originals',side_effect=ValueError('wrong document hash')):
            result=draft_group(Model(),group='fixture',section_titles=['Business'],questions=[],
                material=[{'kind':'pack_passage','ref':self.ref,'text':self.source}],company={},mission={})
        self.assertEqual(result['status'],'refused');self.assertFalse(result['repairable']);self.assertEqual(result['cost_micros'],17)

    def test_malformed_sections_still_follow_the_closed_draft_refusal(self):
        for sections in (None, 7, "not sections"):
            class Model:
                def call(self,**kw):return {'text':json.dumps({'sections':sections,'key_questions':[]}), 'cost_micros':17}
            result=draft_group(Model(),group='fixture',section_titles=['Business'],questions=[],material=[],company={},mission={})
            self.assertEqual(result['status'],'refused',result)
            self.assertIn('InvestmentMemoDraftError',result['reason'])
            self.assertEqual(result['cost_micros'],17)
