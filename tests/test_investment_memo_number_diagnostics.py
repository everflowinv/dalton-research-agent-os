import unittest
from dalton_core.investment_memo_number_diagnostics import findings
from dalton_core.mission_deliverable import validate_section,MissionDeliverableError


class MemoNumberDiagnosticsTests(unittest.TestCase):
    def check(self,numbers):
        section={'title':'同一组完整正文','body':'具备完整来源的数字说明。','numbers':numbers,'claim_refs':[],'gaps':[]}
        kwargs={'live_claim_refs':set(),'resolve_cell':lambda cell:False,
            'resolve_passage':lambda ref:None,'resolve_number':{'pack:a':'200','pack:b':'100'}.get}
        return section,kwargs,findings(section,**kwargs)

    def test_two_independent_bad_numbers_same_section_both_reported(self):
        section,kwargs,errors=self.check([{'text':'111','number_ref':'pack:a'},{'text':'333','number_ref':'pack:b'}])
        self.assertEqual(len(errors),2)
        self.assertIn('111',errors[0]['detail']);self.assertIn('333',errors[1]['detail'])
        with self.assertRaises(MissionDeliverableError):validate_section(section,**kwargs,strict=True)

    def test_full_verified_dependency_chain_is_not_an_extra_failure(self):
        numbers=[{'text':'200','number_ref':'pack:a'},{'text':'100','number_ref':'pack:b'},
            {'text':'结果 2','derivation':{'expression':'200/100','result':'2'}},
            {'text':'结果 200','derivation':{'expression':'2*100','result':'200'}}]
        section,kwargs,errors=self.check(numbers)
        self.assertEqual(errors,[]);validate_section(section,**kwargs,strict=True)

    def test_bad_formula_and_unverified_peer_cannot_become_allowed_operand(self):
        numbers=[{'text':'200','number_ref':'pack:a'},{'text':'100','number_ref':'pack:b'},
            {'text':'结果 3333','derivation':{'expression':'200/100','result':'3333'}},
            {'text':'结果 333300','derivation':{'expression':'3333*100','result':'333300'}}]
        section,kwargs,errors=self.check(numbers)
        self.assertEqual(len(errors),2)
        with self.assertRaises(MissionDeliverableError):validate_section(section,**kwargs,strict=True)

    def test_full_body_still_refuses_undeclared_value_after_all_individual_items_pass(self):
        section,kwargs,errors=self.check([{'text':'200','number_ref':'pack:a'}])
        self.assertEqual(errors,[]);section['body']+=' 未引证 999。'
        with self.assertRaises(MissionDeliverableError):validate_section(section,**kwargs,strict=True)

    def test_source_dag_estimate_label_and_prose_all_reported_together(self):
        for title in ('公司甲的敏感性','Company B valuation'):
            section,kwargs,_=self.check([
                {'text':'111','number_ref':'pack:a'},
                {'text':'200','number_ref':'pack:a'},
                {'text':'100','number_ref':'pack:b'},
                {'text':'结果 3333','derivation':{'expression':'200/100','result':'3333'}},
                {'text':'15.55','estimate':{'assumption_ref':'assumption:a'}}])
            section.update(title=title,body='预期利润为 15.55。详情 {{待填}}。')
            errors=findings(section,**kwargs)
            self.assertTrue(any('number[0]' in e['detail'] for e in errors))
            self.assertTrue(any('number[3]' in e['detail'] for e in errors))
            self.assertTrue(any('15.55' in e['detail'] and '我们的估计' in e['detail'] for e in errors))
            self.assertTrue(any('indirect_placeholder' in e['detail'] for e in errors))
            with self.assertRaises(MissionDeliverableError):validate_section(section,**kwargs,strict=True)

    def test_correct_label_valid_sources_and_dag_are_unchanged(self):
        section,kwargs,_=self.check([{'text':'200','number_ref':'pack:a'},
            {'text':'100','number_ref':'pack:b'},
            {'text':'2','derivation':{'expression':'200/100','result':'2'}},
            {'text':'15.55','estimate':{'assumption_ref':'assumption:a'}}])
        section['body']='我们的估计：利润为 15.55。已披露口径与风险均已核对。'
        before=dict(section)
        self.assertEqual(findings(section,**kwargs),[])
        self.assertEqual(section,before)
        self.assertEqual(validate_section(section,**kwargs,strict=True)['body'],section['body'])

    def test_invalid_estimate_is_not_label_evidence(self):
        section,kwargs,_=self.check([{'text':'15.55','estimate':{'assumption_ref':'foreign:a'}}])
        section['body']='利润为 15.55。'
        errors=findings(section,**kwargs)
        self.assertEqual(len(errors),1)
        self.assertNotIn('without saying',errors[0]['detail'])

    def test_invalid_derived_peer_cannot_hide_estimate_label(self):
        section,kwargs,_=self.check([{'text':'200','number_ref':'pack:a'},
            {'text':'100','number_ref':'pack:b'},
            {'text':'结果 2；无来源 3333，预期 15.55','derivation':{'expression':'200/100','result':'2'}},
            {'text':'15.55','estimate':{'assumption_ref':'assumption:a'}}])
        section['body']='预期利润为 15.55，风险需持续核对。'
        errors=findings(section,**kwargs)
        self.assertTrue(any('number[2]' in e['detail'] for e in errors))
        self.assertTrue(any('without saying' in e['detail'] for e in errors))

    def test_valid_peer_with_same_value_preserves_existing_label_semantics(self):
        section,kwargs,_=self.check([{'text':'200','estimate':{'assumption_ref':'assumption:a'}},
            {'text':'200','number_ref':'pack:a'}])
        section['body']='披露利润为 200，相关依据已核验。'
        self.assertEqual(findings(section,**kwargs),[])
        validate_section(section,**kwargs,strict=True)

    def test_open_or_oversized_body_still_uses_full_shape_refusal(self):
        section,kwargs,_=self.check([])
        for body in ({'text':'{{待填}}'},'x'*6001):
            section['body']=body
            self.assertEqual(findings(section,**kwargs),[])
            with self.assertRaises(MissionDeliverableError):validate_section(section,**kwargs,strict=True)

    def test_bad_gap_does_not_hide_label_or_become_valid_missing_evidence(self):
        section,kwargs,_=self.check([{'text':'15.55','estimate':{'assumption_ref':'assumption:a'}}])
        section.update(body='利润为 15.55。',gap_items=[{'invented':'source'}])
        errors=findings(section,**kwargs)
        self.assertTrue(any('without saying' in e['detail'] for e in errors))
        self.assertTrue(any('gap_items' in e['detail'] for e in errors))
        with self.assertRaises(MissionDeliverableError):validate_section(section,**kwargs,strict=True)

    def test_validation_wake_changes_only_lane_signature_not_paid_prompt(self):
        from unittest.mock import patch
        from dalton_core import investment_memo_number_diagnostics as diagnostic
        from dalton_core.investment_memo_draft import build_group_prompt
        from dalton_core.mission_investment_memo_lane import company_signature
        from tests.test_investment_memo_lane import MemoFairnessTests
        from types import SimpleNamespace
        from pathlib import Path
        import tempfile
        frozen=MemoFairnessTests.frozen('company:A','a'*64)
        context={'company':frozen['company'],'group':'view','section_titles':['研究观点'],
                 'questions':[],'material':[]}
        prompt=build_group_prompt(**context)
        with tempfile.TemporaryDirectory() as raw:
            path=Path(raw)/'config.json';path.write_text('{}')
            launcher=SimpleNamespace(model_config_path=path,verifier_model_config_path=path)
            before=company_signature(frozen,launcher)
            with patch.object(diagnostic,'CONTRACT_REF','investment-memo-complete-section-diagnostics:test'):
                self.assertNotEqual(company_signature(frozen,launcher),before)
                self.assertEqual(build_group_prompt(**context),prompt)
