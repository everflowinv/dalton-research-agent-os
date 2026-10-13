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
