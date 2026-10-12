"""Typed arithmetic operators and exact-original render recovery, offline only."""
import unittest
from pathlib import Path
from tests import test_evidence_index as index_fixture
from dalton_core import evidence_index as ei
from dalton_core.product_passage_validation import checked_rendered_passages
from dalton_core.initial_screen_failure_feedback import _checked_originals
from dalton_core.mission_deliverable import validate_derived_number, MissionDeliverableConflict

class ProductArithmeticTests(unittest.TestCase):
    def derive(self,text,expression,result,sourced):
        return validate_derived_number({'text':text,'derivation':{'expression':expression,'result':result}},
                                       [{'text':str(v)} for v in sourced])
    def test_scale_is_an_operator_on_a_cited_amount(self):
        self.assertEqual(self.derive('市值 12.50 亿美元','1250000000/100000000','12.50',[1250000000])['derivation']['result'],'12.50')
        self.derive('12.50','12500000/1000000','12.50',[12500000])
    def test_scale_cannot_supply_a_new_financial_operand(self):
        for expression,result in [('100000000','100000000'),('1250000000+100000000','1350000000'),('1250000000/200000000','6.25'),
                                  ('(2*100000000)+100000000','300000000'),
                                  ('(2*100000000)/100000000+100000000','100000002')]:
            with self.subTest(expression=expression),self.assertRaises(MissionDeliverableConflict):
                self.derive(result,expression,result,[1250000000,2])
    def test_verified_typographic_growth_formula_is_not_a_prose_fact(self):
        self.derive('（981.67/948.44−1）×100＝3.50%','(981.67/948.44-1)*100','3.50',[981.67,948.44])
    def test_unrelated_constant_or_financial_value_in_prose_is_still_rejected(self):
        for extra in ('，利润 100','，收入 1','，目标价 15.8'):
            with self.subTest(extra=extra),self.assertRaises(MissionDeliverableConflict):
                self.derive('(110/100-1)*100=10.0'+extra,'(110/100-1)*100','10.0',[110,100]) if extra!='，利润 100' else self.derive('(22/20-1)*100=10.0'+extra,'(22/20-1)*100','10.0',[22,20])
    def test_invented_operand_and_wrong_result_cannot_hide_in_formula(self):
        for expression,result in [('(981.67/900-1)*100','9.07'),('(981.67/948.44-1)*100','9.50')]:
            with self.subTest(expression=expression),self.assertRaises(MissionDeliverableConflict):
                self.derive(expression+'='+result,expression,result,[981.67,948.44])
        with self.assertRaises(MissionDeliverableConflict):
            self.derive('(22/20-1)*100+999=10.0','(22/20-1)*100','10.0',[22,20])

class ProductPassageTests(unittest.TestCase):
    def setUp(self):
        f=index_fixture.IndexFixture('runTest');f.setUp();self.addCleanup(f.doCleanups);self.state=f.state
        item=ei.evidence_search(self.state,'New bookings were',company_ref=index_fixture.ACN)['results'][0]
        # A real source-backed subspan with boundary whitespace.
        raw=item['text'];end=raw.find('\n')+1
        if end<=1:end=len(raw)
        self.raw=raw[:end]
        self.ref=ei.make_passage_ref(item['document_ref'],item['start'],item['start']+len(self.raw),self.raw)
        self.assertNotEqual(self.raw,self.raw.strip())
        self.sections=[{'numbers':[{'text':'22.2','passage_ref':self.ref}]}]
    def test_boundary_strip_restored_from_original_without_writing_source(self):
        path=ei.index_path(self.state);before=path.read_bytes()
        resolved=checked_rendered_passages(self.state,self.sections,{self.ref:self.raw.strip()})
        self.assertEqual(resolved[self.ref],self.raw);self.assertEqual(path.read_bytes(),before)
        self.assertEqual(ei.verify_against_source(self.state,self.ref)['status'],'verified')
    def test_internal_change_wrong_span_or_missing_original_is_refused(self):
        for text in (self.raw.strip().replace('Accenture','Invented Issuer'),self.raw.strip()+' profit 7.5'):
            with self.subTest(text=text),self.assertRaises(ValueError):
                checked_rendered_passages(self.state,self.sections,{self.ref:text})
        with self.assertRaises(ValueError):checked_rendered_passages(':memory:',self.sections,{self.ref:self.raw.strip()})
    def test_only_used_passages_need_read_and_exact_hashed_pack_stays_compatible(self):
        resolved=checked_rendered_passages(':memory:',self.sections,{self.ref:self.raw,'not-a-ref':'unused'})
        self.assertEqual(resolved,{self.ref:self.raw})

    def test_unbound_metadata_header_is_refused_even_when_provenance_is_tampered(self):
        proof=_checked_originals(self.state,{self.ref},include_render_metadata=True)[self.ref]
        self.assertNotIn('render_header',proof)
        for header in ('《Accenture Q1 2026》2026-03-20：管理层发言',
                       '《Foreign Issuer》1999-01-01：管理层发言'):
            with self.subTest(header=header),self.assertRaises(ValueError):
                checked_rendered_passages(self.state,self.sections,{self.ref:self.raw.strip()+'\n'+header})
        # A provenance edit cannot authorize removing any added header.
        from dalton_core.readonly_sqlite import connect_read_only
        import sqlite3,json
        core=sqlite3.connect(self.state/'core.sqlite')
        row=core.execute('SELECT document_ref,record_json FROM document_provenance_records LIMIT 1').fetchone()
        if row:
            core.execute('DROP TRIGGER IF EXISTS document_provenance_records_no_update')
            body=json.loads(row[1]);body.update(title='Foreign Issuer',published_at='1999-01-01')
            core.execute('UPDATE document_provenance_records SET record_json=? WHERE document_ref=?',(json.dumps(body),row[0]));core.commit()
        core.close()
        with self.assertRaises(ValueError):
            checked_rendered_passages(self.state,self.sections,{self.ref:self.raw.strip()+'\n《Foreign Issuer》1999-01-01：管理层发言'})

    def test_qualitative_refs_require_original_even_when_pack_hash_matches(self):
        sections=[{'numbers':[],'source_refs':[{'ref':self.ref}]}]
        result=checked_rendered_passages(self.state,sections,{self.ref:self.raw})
        self.assertEqual(result,{self.ref:self.raw})
        with self.assertRaises(ValueError):checked_rendered_passages(':memory:',sections,{self.ref:self.raw})
        with self.assertRaises(ValueError):checked_rendered_passages(self.state,sections,{})
        from dalton_core.investment_memo_cli import cited_refs
        self.assertEqual(cited_refs(sections,[]),{self.ref})

    def test_new_pack_stops_at_renderer_header_and_preserves_strong_original(self):
        from dalton_core.product_contract import tag_entry
        p={'tags':{'P1':self.ref,'P2':self.ref},'json':{},'text':
            '[P1] · 电话会纪要 · 2026-10-01 · 《Synthetic Call》\n'+self.raw.strip()+
            '\n\n《Next Call》2026-11-01：管理层发言\n[P2] · 电话会纪要\n'+self.raw.strip()}
        row=tag_entry(p,'P1');self.assertEqual(row['text'],self.raw.strip())
        self.assertEqual(checked_rendered_passages(self.state,self.sections,{self.ref:row['text']})[self.ref],self.raw)
        # A similar line inside the source, without a next block, is not erased.
        p['text']='[P1] · 电话会纪要\n'+self.raw.strip()+'\n《Next Call》2026-11-01：管理层发言\nquoted continuation'
        self.assertIn('quoted continuation',tag_entry(p,'P1')['text'])
        with self.assertRaises(ValueError):
            checked_rendered_passages(self.state,self.sections,{self.ref:tag_entry(p,'P1')['text']})
