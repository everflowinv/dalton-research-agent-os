"""Real FTS/MaterialRequest tests, no provider or network."""
from dalton_core import material_acquisition as ma, material_requests as mr
from dalton_core.material_retrieval import contract
from tests.test_material_acquisition import Fixture, NOW, FakeRung, later
from tests.test_material_pack import SYNW, OTHER, FILL

class StructuredMaterialRetrievalTests(Fixture):
    def answer(self, *, terms='cost of revenues, selling general administrative', form='10-Q', **kwargs):
        return self.request(need={'kind':'answer','question':
            f'{form} 中这些成本分别是多少、口径是什么？ -- wants: 原文各项财务段落 -- terms: {terms}'},
            acceptance={'min_passages':1,'min_coverage':.5},**kwargs)

    def add(self, kind='sec_10q', *, company=SYNW, terms='Cost of revenues were $100 million. Selling, general and administrative expenses were $40 million.', date='2026-08-06'):
        return self.corpus.add(kind, 'Synthetic Widgets '+('10-Q' if kind=='sec_10q' else '8-K'),terms+' '+FILL*3,
                               date=date, company=company)

    def test_held_issuer_filing_covers_compound_targets_without_chinese_metadata_in_search(self):
        req=self.answer();ref=self.add()
        queries=ma.build_queries(req, {'name':'Synthetic Widgets','ticker':'SYNW'},now=NOW)
        self.assertFalse(any('--' in q or '分别' in q for q in queries))
        verdict=ma.evaluate_acceptance(self.state,req,now=NOW)
        self.assertTrue(verdict['met'],verdict)
        self.assertEqual(verdict['missing_targets'],[])
        self.assertEqual({x['document_ref'] for x in verdict['items']},{ref})
        self.assertTrue(all(x['document_sha256'] for x in verdict['items']))

    def test_one_target_or_generic_words_cannot_answer_whole_compound_need(self):
        req=self.answer();self.add(terms='Cost of revenues were $100 million. General business revenue grows.')
        result=ma.evaluate_acceptance(self.state,req,now=NOW)
        self.assertEqual((result['met'],result['partial']),(False,True))
        self.assertEqual(result['missing_targets'],['selling general administrative'])
        self.assertNotIn('General business',str(result['target_passages']))

    def test_wrong_form_and_foreign_issuer_association_are_rejected(self):
        req=self.answer();foreign=self.add(company=OTHER)
        # Deliberately mimic broad discovery/listed and named associations.
        from dalton_core import evidence_index as ei
        c=ei.open_index(self.state)
        try:
            doc=c.execute('SELECT doc_id FROM evidence_documents WHERE document_ref=?',(foreign,)).fetchone()[0]
            c.execute("INSERT INTO evidence_document_companies VALUES (?,?,'listed')",(doc,SYNW));c.commit()
        finally:c.close()
        self.add('sec_8k')
        verdict=ma.evaluate_acceptance(self.state,req,now=NOW)
        self.assertFalse(verdict['met']);self.assertFalse(verdict['partial']);self.assertEqual(verdict['items'],[])

    def test_period_and_minimum_passages_still_apply(self):
        req=self.answer(period='quarter_ending:2026-08-31')
        self.add(date='2024-08-06')
        self.assertFalse(ma.evaluate_acceptance(self.state,req,now=NOW)['met'])
        self.add(date='2026-09-25')
        self.assertTrue(ma.evaluate_acceptance(self.state,req,now=NOW)['met'])
        bounded={**req,'acceptance':{**req['acceptance'],'min_passages':10}}
        self.assertFalse(ma.evaluate_acceptance(self.state,bounded,now=NOW)['met'])

    def test_explicit_forms_route_sec_not_transcript_or_expert(self):
        for form,spec in [('10-K','annual-report-10k'),('10-Q','quarterly-report-10q'),('8-K','current-report-8k')]:
            req=self.answer(form=form)
            self.assertEqual(ma.TargetedSourceRung('sec_edgar').spec_for(req),spec)
            self.assertIsNone(ma.TargetedSourceRung('alphaengine').spec_for(req))
            self.assertIsNone(ma.TargetedSourceRung('guidepoint').spec_for(req))
        unrestricted=self.answer(form='管理层讨论')
        self.assertEqual(ma.TargetedSourceRung('alphaengine').spec_for(unrestricted),'earnings-call-transcripts')

    def test_free_recheck_closed_request_once_preserves_history_and_all_spend(self):
        req=self.answer(source_priority=['indexed_corpus','web'])
        mr.record_attempt(self.state,req['id'],source='indexed_corpus',outcome='empty',now=NOW)
        ma.ensure_schema(self.state);ma._record_spend(self.state,req['id'],'web',3,105000,now=NOW)
        mr.close(self.state,req['id'],status='exhausted',reason='old lexical query missed',now=NOW)
        before=mr.request(self.state,req['id']);self.add()
        web=FakeRung('web',[])
        result=ma.serve(self.state,req['id'],rungs={'web':web},now=later(hours=1))
        self.assertEqual(result['outcome'],'found');self.assertEqual(web.runs,0)
        after=mr.request(self.state,req['id'])
        self.assertEqual(after['events'][:len(before['events'])],before['events'])
        self.assertEqual(ma.request_spend(self.state,req['id']),{'calls':3,'cost_micros':105000})
        count=len(after['events'])
        self.assertEqual(ma.serve(self.state,req['id'],rungs={'web':web},now=later(hours=2))['outcome'],'cached')
        self.assertEqual(len(mr.request(self.state,req['id'])['events']),count)

    def test_failed_free_recheck_stays_closed_no_new_paid_round(self):
        req=self.answer(source_priority=['web'])
        mr.close(self.state,req['id'],status='exhausted',reason='old miss',now=NOW)
        web=FakeRung('web',[])
        first=ma.serve(self.state,req['id'],rungs={'web':web},now=later(hours=1))
        self.assertEqual(first['outcome'],'cached');self.assertEqual(web.runs,0)
        count=len(mr.request(self.state,req['id'])['events'])
        second=ma.serve(self.state,req['id'],rungs={'web':web},now=later(hours=2))
        self.assertEqual(second['outcome'],'cached');self.assertEqual(web.runs,0)
        self.assertEqual(len(mr.request(self.state,req['id'])['events']),count)

    def test_new_conversion_preserves_original_need_identity_and_adds_terms(self):
        from dalton_core.research_planner import inquiry_material_request
        inquiry={'company_ref':SYNW,'question':'10-Q 成本分别多少？','wants':'成本原文',
                 'directed_document':{'query_terms':['cost of revenues','selling general administrative'],
                                      'document_ref':'doc:x','document_version_hash':'a'*64}}
        spec=inquiry_material_request(inquiry)
        self.assertEqual(spec['need']['question'],'10-Q 成本分别多少？ -- wants: 成本原文 -- terms: cost of revenues, selling general administrative')
        self.assertEqual(spec['acceptance']['retrieval']['query_terms'],inquiry['directed_document']['query_terms'])
        self.assertEqual(contract(spec)['doc_kinds'],['sec_10q'])

    def test_legacy_chinese_question_can_find_held_original_but_not_claim_full_answer(self):
        req=self.request(need={'kind':'answer','question':
            '第二季度 10-Q 中，营业成本和销售及管理费用各占收入多少？一次性项目或汇率影响多少？'},
            acceptance={'min_passages':1})
        self.add()
        result=ma.evaluate_acceptance(self.state,req,now=NOW)
        self.assertEqual((result['met'],result['partial']),(False,True))
        self.assertIn('one-time items',result['missing_targets'])
        self.assertIn('foreign exchange',result['missing_targets'])
        self.assertTrue(result['target_passages']['cost of revenues'])
        self.assertTrue(result['target_passages']['selling general administrative'])
        self.assertEqual(ma.TargetedSourceRung('sec_edgar').spec_for(req),'quarterly-report-10q')

    def test_multiple_forms_and_overlong_target_list_cannot_be_silently_reduced(self):
        req=self.answer(form='10-K 和 10-Q')
        self.add('sec_8k')
        self.add()
        result=ma.evaluate_acceptance(self.state,req,now=NOW)
        self.assertFalse(result['met'])
        from dalton_core.material_retrieval import retrieval_fields
        inquiry={'question':'10-Q 各项','directed_document':{'query_terms':[f'target {i}' for i in range(13)]}}
        self.assertIsNone(retrieval_fields(inquiry))

    def test_actual_sec_cik_url_cannot_be_overridden_by_company_associations(self):
        from dalton_core.material_retrieval import issuer_matches
        issuer='company:sec-cik:0001234567'
        item={'filed_company_refs':[issuer],'kind':'sec_10q',
              'url':'https://www.sec.gov/Archives/edgar/data/9999999/20260101/a.htm'}
        self.assertFalse(issuer_matches(item,issuer,{}))
        item['url']='https://www.sec.gov/Archives/edgar/data/1234567/20260101/a.htm'
        self.assertTrue(issuer_matches(item,issuer,{}))
        item['url']='https://sec.gov.attacker.test/Archives/edgar/data/1234567/x.htm'
        self.assertFalse(issuer_matches(item,issuer,{}))

    def test_free_read_failure_does_not_reopen_closed_or_dispatch_anything(self):
        from unittest.mock import patch
        req=self.answer();mr.close(self.state,req['id'],status='exhausted',reason='old miss',now=NOW)
        before=mr.request(self.state,req['id'])
        with patch('dalton_core.evidence_index.evidence_search',side_effect=RuntimeError('unavailable index')):
            with self.assertRaisesRegex(RuntimeError,'unavailable index'):
                ma.serve(self.state,req['id'],now=later(hours=1))
        self.assertEqual(mr.request(self.state,req['id'])['events'],before['events'])

    def test_unknown_typed_contract_cannot_fall_back_to_weaker_bare_question(self):
        from dalton_core.material_retrieval import MaterialRetrievalError
        req=self.answer();self.add()
        req['acceptance']['retrieval']={'rule_ref':'future:unknown','query_terms':['cost of revenues'],
                                       'wants':'','doc_kinds':['sec_10q']}
        with self.assertRaises(MaterialRetrievalError):
            ma.evaluate_acceptance(self.state,req,now=NOW)

    def test_numeric_request_never_closed_by_retrieval(self):
        req=self.answer();self.add()
        numeric={**req,'acceptance':{**req['acceptance'],'numeric_contract_ref':'directed-document-numeric:0.1'}}
        self.assertFalse(ma.evaluate_acceptance(self.state,numeric,now=NOW)['met'])
