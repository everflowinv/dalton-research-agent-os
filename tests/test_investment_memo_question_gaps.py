import copy
import unittest
from dalton_core.investment_memo_question_gaps import build,validate,CONTRACT_REF
from dalton_core.investment_memo_draft import GROUPS
from dalton_core.store import content_hash

class QuestionGapTests(unittest.TestCase):
    def inputs(self,company='company:synthetic-a'):
        questions=[f'Question {i}'for i in range(12)]
        frozen={'company':{'company_ref':company},'mission':{'id':'coverage-mission-version:x:1','content_hash':'a'*64},'playbook':{'id':'playbook-version:x:1','content_hash':'b'*64,'key_questions':questions},'material':[{'ref':'market-block:exact','text':'held','hash':'c'*64}]}
        answers=[{'question_ref':f'memo_q{i+1:02d}','question':q,'answer':'Supported answer','refs':['market-block:exact'],'unknown':False,'falsifier':'Contrary evidence'}for i,q in enumerate(questions)]
        calls=[{'group':g,'work_order_ref':'work:'+g,'route_decision_ref':'route:'+g,'result_envelope_ref':'result:'+g,'invocation_ref':'invocation:'+g}for g,_,_ in GROUPS]
        return frozen,answers,calls
    def test_exact_unknown_answers_parent_group_and_material_two_companies(self):
        for company in ('company:synthetic-a','company:synthetic-b'):
            f,a,c=self.inputs(company)
            for i in (3,4,5,10):a[i].update(answer='',refs=[],unknown=True,falsifier='Get exact missing historical source or current permission')
            before=copy.deepcopy((f,a,c));p=build(f,a,c)
            self.assertEqual((f,a,c),before);self.assertEqual(p['contract_ref'],CONTRACT_REF);self.assertEqual(p['company_ref'],company)
            self.assertEqual(p['material_hash'],content_hash(f['material']));self.assertEqual(len(p['items']),4)
            self.assertEqual(p['items'][0]['parent_call'],c[1]);self.assertEqual(p['items'][-1]['parent_call'],c[3])
            self.assertEqual(p['items'][-1]['refs'],[]);self.assertEqual(p['items'][-1]['answer'],'')
    def test_resolved_answers_have_no_material_gap_and_unknown_never_gains_evidence(self):
        f,a,c=self.inputs();self.assertEqual(build(f,a,c)['items'],[])
        a[0].update(unknown=True,answer='',refs=[],falsifier='Need original source')
        for change in ({'answer':'invented'},{'refs':['market-block:exact']},{'unknown':1},{'falsifier':''},{'question_ref':'memo_q02'},{'question':'Different current question'}):
            bad=copy.deepcopy(a);bad[0].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):build(f,bad,c)
    def test_missing_parent_group_incomplete_answers_extra_fields_refuse(self):
        f,a,c=self.inputs()
        for bad_calls in (c[:3],list(reversed(c)),[{**c[0],'unknown_field':'x'},*c[1:]]):
            with self.assertRaises(ValueError):build(f,a,bad_calls)
        with self.assertRaises(ValueError):build(f,a[:11],c)
        a[0]['extra']='x'
        with self.assertRaises(ValueError):build(f,a,c)

    def test_summary_must_equal_strong_read_parents_not_caller_self_hash(self):
        f,a,c=self.inputs();a[0].update(answer='',refs=[],unknown=True,falsifier='Need original source')
        proof=build(f,a,c);self.assertEqual(validate(proof,frozen=f,answers=a,calls=c),proof)
        for mutate in (lambda v:v.update(company_ref='company:foreign'),
                lambda v:v['items'][0]['parent_call'].update(work_order_ref='work:foreign'),
                lambda v:v['items'][0].update(falsifier='Invent different need'),
                lambda v:v.update(items=[]),lambda v:v.update(material_hash='f'*64)):
            bad=copy.deepcopy(proof);mutate(bad)
            with self.assertRaises(ValueError):validate(bad,frozen=f,answers=a,calls=c)
