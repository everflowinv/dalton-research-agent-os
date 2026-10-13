"""Reference tokens and vendor capability come from actual bound authorities."""
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dalton_core.material_request_citation import request_citation_resolver
from dalton_core.material_requests import open_request
from dalton_core.mission_deliverable import MissionDeliverableError, validate_section
from dalton_core.initial_screen import parse_document_output, source_kinds_of, verify_document
from dalton_core.product_contract import normalise_pack, tag_entry
from dalton_core.research_reasoning_lint import lint_text
from tests.synthetic_company import material_pack, strl_document, strl_worksheet


class InitialScreenBoundProvenanceTests(unittest.TestCase):
    def request(self, root, company):
        return open_request(root, requester={'lane': 'initial_screen'}, subject=company,
                            need={'kind': 'document', 'doc_kinds': ['sec_exhibit']},
                            period='quarter_ending:2026-06-30')

    def section(self, req):
        ref=req['id']
        return {'title': '附录', 'body': f'该披露缺口影响我们对经济驱动的判断，需要交叉核对而不能猜测。材料缺少公告，已尝试：已登记 {ref}。',
                'gap_items':[{'kind':'not_acquired','need':'公告','request_ref':ref,
                              'period':req['period'],'tried':'检索公告'}], 'numbers':[]}

    def test_two_company_exact_ledger_reference_not_amount_and_no_write(self):
        for company in ('company:synthetic:a','company:synthetic:b'):
            with self.subTest(company=company), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);req=self.request(root,company);s=self.section(req)
                before=(root/'material-requests.sqlite').read_bytes()
                with mock.patch('dalton_core.planner_paid_output_recovery._authority_connection',
                                wraps=__import__('dalton_core.planner_paid_output_recovery',fromlist=['_authority_connection'])._authority_connection) as reader:
                    resolver=request_citation_resolver(root,company)
                    out=validate_section(s,resolve_request=resolver,strict=True)
                    validate_section(s,resolve_request=resolver,strict=True)
                    self.assertEqual(reader.call_count,1)
                self.assertEqual(out['body'],s['body'])
                self.assertEqual((root/'material-requests.sqlite').read_bytes(),before)
                validate_section({**s,'body':s['body'].replace('。','.')},resolve_request=resolver)
                with self.assertRaises(MissionDeliverableError):validate_section(s)
                with self.assertRaises(MissionDeliverableError):
                    validate_section(s,resolve_request=request_citation_resolver(root,'company:synthetic:other'))

    def test_reference_exemption_does_not_source_neighbor_amount_or_fake_identifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            req=self.request(tmp,'company:synthetic:a');s=self.section(req)
            for body in (s['body']+' 收入987654美元。',
                         s['body'].replace(req['id'],req['id']+'abc'),
                         s['body'].replace(req['id'],req['id']+'.12345'),
                         s['body'].replace(req['id'],'x'+req['id']),
                         s['body'].replace(req['id'],'material-request:12345678901234567890123456789012')):
                with self.subTest(body=body),self.assertRaises(MissionDeliverableError):
                    validate_section({**s,'body':body},resolve_request=request_citation_resolver(tmp,'company:synthetic:a'))
            for mutation in ({'gap_items':[]},
                             {'gap_items':[{**s['gap_items'][0],'period':'quarter_ending:2025-06-30'}]}):
                with self.subTest(mutation=mutation),self.assertRaises(MissionDeliverableError):
                    validate_section({**s,**mutation},resolve_request=request_citation_resolver(tmp,'company:synthetic:a'))

    def test_tampered_record_hash_and_projection_do_not_authorize_exemption(self):
        for column,value in (('content_hash','0'*64),('subject_ref','company:synthetic:other'),
                             ('need_kind','answer'),('record_json','{}')):
            with self.subTest(column=column),tempfile.TemporaryDirectory() as tmp:
                req=self.request(tmp,'company:synthetic:a');s=self.section(req)
                with sqlite3.connect(Path(tmp)/'material-requests.sqlite') as c:
                    # Corruption fixture only: append authority's existing triggers
                    # are deliberately removed in this isolated database.
                    for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                        c.execute('DROP TRIGGER "'+r[0]+'"')
                    c.execute(f'UPDATE material_requests SET {column}=? WHERE request_id=?',(value,req['id']))
                with self.assertRaises(MissionDeliverableError):
                    validate_section(s,resolve_request=request_citation_resolver(tmp,'company:synthetic:a'))

    def pack(self,source='consensus_vendor'):
        p=normalise_pack(material_pack());pointer='/numbers/valuation/forward_pe/0/forward_pe'
        p['tags']['N99']='pack-json:'+pointer
        p['json']['number_tags']['N99']={'pointer':pointer,'value':'27','display':'27'}
        p['json'].setdefault('numbers',{}).setdefault('valuation',{})['forward_pe']=[{
            'source':source,'label':'一致预期（数据商）','status':'available','forward_pe':'27',
            'value':'9.65','method':'next_four_fiscal_quarters','window':['FY2026Q3','FY2026Q4','FY2027Q1','FY2027Q2']}]
        return p

    def test_exact_vendor_parent_capability_survives_document_projection(self):
        p=self.pack();doc=strl_document();doc['sections'][0].update(
            body='数据商一致预期的NTM市盈率27倍。',sources=['N99'],numbers=[{'tag':'N99'}])
        before=copy.deepcopy((p,doc));sections,findings=parse_document_output(doc,pack=p)
        self.assertEqual(findings,[]);self.assertEqual((p,doc),before)
        self.assertEqual(tag_entry(p,'N99')['source_kind'],'consensus')
        self.assertIn('consensus',source_kinds_of(sections[0]))
        self.assertEqual(lint_text(sections[0]['body'],source_kinds=source_kinds_of(sections[0])),[])

    def test_street_ours_label_only_wrong_pointer_value_and_unavailable_not_vendor(self):
        variants=[]
        for source in ('street_median','ours','single_broker'):
            variants.append(self.pack(source))
        for edit in ('value','display','transform','status','pointer','ref'):
            p=self.pack()
            if edit=='value':p['json']['number_tags']['N99']['value']='28'
            if edit=='display':p['json']['number_tags']['N99']['display']='28'
            if edit=='transform':p['json']['number_tags']['N99']['transform']='÷1000'
            if edit=='status':p['json']['numbers']['valuation']['forward_pe'][0]['status']='unavailable'
            if edit=='pointer':p['json']['number_tags']['N99']['pointer']='/numbers/valuation/forward_pe/1/forward_pe'
            if edit=='ref':p['tags']['N99']='pack-json:/numbers/valuation/forward_pe/0/value'
            variants.append(p)
        for p in variants:
            with self.subTest(pack=p):
                entry=tag_entry(p,'N99');self.assertNotEqual(entry['source_kind'],'consensus')
                findings=lint_text('数据商一致预期市盈率27倍。',source_kinds=[entry['source_kind']])
                self.assertTrue(any(f['code']=='lint:consensus_without_authority' for f in findings))

    def test_normal_deterministic_verifier_uses_real_request_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            company=material_pack().company_ref;req=self.request(tmp,company)
            pack=normalise_pack(material_pack());doc=strl_document();s=doc['sections'][-1]
            old=s['gap_items'][-1]['request_ref'];s['body']=s['body'].replace(old,req['id'])
            s['gap_items'][-1]['request_ref']=req['id']
            sections,findings=parse_document_output(doc,pack=pack);self.assertEqual(findings,[])
            result=verify_document(sections,worksheet=strl_worksheet(),pack=pack,
                resolve_request=request_citation_resolver(tmp,company))
            self.assertFalse(any(req['id'] in f.get('detail','')for f in result['findings']))
            self.assertFalse(any(f['code']=='appendix_request_not_cited'for f in result['findings']))
            self.assertFalse(any(f['code']=='citation_or_contract'and f.get('section')==sections[-1]['title']
                                 for f in result['findings']))
            sections[-1]['body']=sections[-1]['body'].replace(req['id'],'请求已登记')
            missing=verify_document(sections,worksheet=strl_worksheet(),pack=pack,
                resolve_request=request_citation_resolver(tmp,company))
            self.assertTrue(any(f['code']=='appendix_request_not_cited'for f in missing['findings']))
