import copy
from types import SimpleNamespace
from unittest import TestCase,mock
from tests import test_investment_memo_draft as f
from dalton_core.investment_memo_cli import run_memo
from dalton_core.investment_memo_draft import GROUPS

class EarlyMemoValidationTests(TestCase):
    def frozen(self):
        return {'status':'ready','mission':{},'playbook':{'deliverable_templates':{'investment_memo':f.TITLES},'key_questions':f.QUESTIONS},
            'company':{'company_ref':'company:ticker:synth'},'material':[{'ref':'claim-version:1','kind':'claim','text':'收入增长已核对。'}], 'input_bindings':[]}
    def payload(self,idx=0):
        _,si,qi=GROUPS[idx]
        return f.group_payload([f.TITLES[i]for i in si],[{'question_ref':f'memo_q{i+1:02d}','question':f.QUESTIONS[i]}for i in qi])
    def bad(self):
        value=self.payload();value['sections'][1]['body']+=' 收入 999.3。';value['sections'][2]['body']+=' 利润 777.2。'
        return value
    def test_all_first_group_errors_reach_the_existing_one_repair(self):
        model=f.FakeModel([self.bad(),self.bad()]);verifier=f.FakeModel([])
        result=run_memo(store=SimpleNamespace(),frozen=self.frozen(),model=model,verifier_model=verifier,max_repairs=1)
        self.assertEqual(result['status'],'refused');self.assertEqual(len(model.calls),2);self.assertEqual(verifier.calls,[])
        self.assertEqual(len(result['findings']),2)
        self.assertIn('999.3',model.calls[1]['prompt']);self.assertIn('777.2',model.calls[1]['prompt'])
        self.assertEqual(len(result['repairs']),1);self.assertEqual(result['cost_micros'],20)
    def test_run_budget_refusal_does_not_buy_repair_or_later_groups(self):
        model=f.FakeModel([self.bad()]);budget=mock.Mock();budget.can_repair.return_value=False
        result=run_memo(store=SimpleNamespace(),frozen=self.frozen(),model=model,verifier_model=f.FakeModel([]),max_repairs=1,repair_budget=budget)
        self.assertEqual(result['status'],'refused');self.assertEqual(len(model.calls),1);self.assertNotIn('repairs',result)
        budget.can_repair.assert_called_once_with(remaining_groups=3)
    def test_zero_repair_policy_preserved_and_no_four_group_spend(self):
        model=f.FakeModel([self.bad()]);result=run_memo(store=SimpleNamespace(),frozen=self.frozen(),model=model,verifier_model=f.FakeModel([]),max_repairs=0)
        self.assertEqual(result['status'],'refused');self.assertEqual(len(model.calls),1)
    def test_successful_first_repair_consumes_the_only_slot_for_entire_run(self):
        second=self.payload(1);second['sections'][0]['body']+=' 收入 123.4。'
        model=f.FakeModel([self.bad(),self.payload(),second])
        result=run_memo(store=SimpleNamespace(),frozen=self.frozen(),model=model,verifier_model=f.FakeModel([]),max_repairs=1)
        self.assertEqual(result['status'],'refused');self.assertEqual(len(model.calls),3);self.assertEqual(len(result['repairs']),1)
    def test_missing_original_never_buys_model_repair(self):
        value=self.payload();value['sections'][1]['numbers']=[{'text':'22.2','passage_ref':'doc-passage:alphaengine-doc:123:0-8:000000000000'}]
        frozen=self.frozen();frozen['material'].append({'ref':'doc-passage:alphaengine-doc:123:0-8:000000000000','kind':'pack_passage','text':'22.2'})
        model=f.FakeModel([value]);result=run_memo(store=SimpleNamespace(),frozen=frozen,model=model,verifier_model=f.FakeModel([]),max_repairs=1)
        self.assertIn('memo_source_proof_unavailable',result['reason']);self.assertEqual(len(model.calls),1)

    def test_real_seventy_dollar_floor_first_paid_group_has_no_repair_headroom(self):
        from dalton_core.investment_memo_repair_budget import MemoRepairBudget
        model=f.FakeModel([self.bad()])
        budget=MemoRepairBudget(model,run_micros=70_000_000,producer_micros=14_000_000,verifier_micros=14_000_000)
        with mock.patch('dalton_core.investment_memo_repair_budget.settled_call',return_value={'accounted_micros':100_000}):
            result=run_memo(store=SimpleNamespace(),frozen=self.frozen(),model=budget,verifier_model=f.FakeModel([]),max_repairs=1,repair_budget=budget)
        self.assertEqual(result['status'],'refused');self.assertEqual(len(model.calls),1)
        self.assertNotIn('repairs',result);self.assertEqual(budget.accounted_micros,100_000)
        self.assertFalse(budget.can_repair(remaining_groups=3))
