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

    def test_only_exact_source_title_date_header_can_be_recovered(self):
        proof=_checked_originals(self.state,{self.ref},include_render_metadata=True)[self.ref]
        header=proof['render_header']
        result=checked_rendered_passages(self.state,self.sections,{self.ref:self.raw.strip()+'\n'+header})
        self.assertEqual(result[self.ref],self.raw)
        for wrong in (header.replace('2026','2025'),header.replace('Accenture','Foreign Issuer'),header+' profit 9.5'):
            with self.subTest(header=wrong),self.assertRaises(ValueError):
                checked_rendered_passages(self.state,self.sections,{self.ref:self.raw.strip()+'\n'+wrong})
