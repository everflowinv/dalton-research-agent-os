"""Exact formal failures and fees decide secondary isolation, never summaries."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import tempfile
from dalton_core.contracts import WorkOrder
from dalton_core.document_extraction import DocumentExtractionService
from dalton_core.document_extraction_cli import _secondary_sweep, CONSECUTIVE_CERTAIN_FAILURE_LIMIT
from dalton_core.document_numeric_extraction import NumericExtractionError, parse_response
from dalton_core.scheduler import Scheduler
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from tests.test_scheduler import work_order, result

class SecondaryIsolationTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scheduler = Scheduler(self.root/'scheduler.sqlite', max_attempts=1)
        self.addCleanup(self.scheduler.close)
        self.budget_path = self.root/'budget.sqlite'
        self.budget = ThesisImpactBudgetStore(self.budget_path, clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc))
        self.addCleanup(self.budget.close)
        self.budget.register_policy(policy_version_id='policy:one', day_cap_micros=1_000_000)
        self.service = DocumentExtractionService(SimpleNamespace(
            _scheduler=self.scheduler, _document_extraction_model_config={'budget_db':str(self.budget_path)}))

    def formal_failure(self, *, code='MODEL_OUTPUT_CONTRACT_REJECTED', settled=True, formal=True, reserve=True, not_sent=False, work=None):
        work = work or WorkOrder.from_dict(work_order('work:secondary:one'))
        self.scheduler.enqueue(work)
        if reserve:
            admission=self.budget.admit(policy_version_id='policy:one', day='2026-08-22', work_order_ref=work.id,
                attempt_number=1, phase='assessment', route_decision_ref='route:one', reserved_micros=20_000)
            if settled:self.budget.settle(admission['admission_id'], actual_micros=4630, usage_entry_ref='usage:one')
        if formal:
            lease=self.scheduler.claim('worker:one', work_order_id=work.id)
            envelope=result('result:one', work_ref=work.id, status='failed')
            envelope['error']={'code':code}
            if not_sent:
                envelope['metadata']={'dispatch_proof': {'authority':'openclaw-model-adapter',
                    'state':'definitely_not_sent','version':'0.1'}}
            self.scheduler.complete(work.id,1,'worker:one',lease['lease_token'],envelope,idempotency_key='completion:one')
        return work

    def failed_answer(self, **kwargs):
        work=self.formal_failure(**kwargs)
        return {'status':'no_result','verified':[],'refused':[],'recorded':[],
            'replayed':False,'work_order_ref':work.id,**self.service._secondary_failure_details(work,'numeric')}

    def sweep(self, answers, *, entries='numeric'):
        calls=[]
        reviews=[{'review_id':f'review:{i}','source_ref':'source:fixture','document_ref':f'doc:{i}',
                  'company_ref':'company:fixture'} for i in range(len(answers))]
        def generate(**kw):
            calls.append(kw['review_id']);return answers[len(calls)-1]
        service=SimpleNamespace(view=lambda **kw:{'context':{'content_hash':'a'*64,'next_offset':None}},generate_numeric=generate)
        summary={entries:[],'figures':0,'numeric_fresh':0,'isolated_windows':[]}
        failure=_secondary_sweep(service,[('actor:fixture',reviews,{})],summary,limit=len(answers),entries=entries,
            wanted=lambda *a:True,call='generate_numeric',counts={'verified':'verified','refused':'refused','recorded':'recorded'},
            total=('figures','recorded'),spent_key='numeric_fresh',require_open=False)
        return failure,summary,calls

    def test_fenced_json_still_rejected_but_settled_window_does_not_stop_next_document(self):
        with self.assertRaises(NumericExtractionError):parse_response('```json\n{"figures":[],"schema_version":"0.1"}\n```')
        failure,summary,calls=self.sweep([self.failed_answer(),{'status':'read','verified':[], 'refused':[], 'recorded':[], 'replayed':False}])
        self.assertIsNone(failure);self.assertEqual(len(calls),2)
        isolated=summary['isolated_windows'][0]
        self.assertEqual(isolated['error_code'],'MODEL_OUTPUT_CONTRACT_REJECTED')
        self.assertEqual(isolated['budget_status'],'settled');self.assertTrue(isolated['cost_certain'])
        self.assertTrue(isolated['formal_result_hash']);self.assertEqual(summary['numeric_fresh'],2)

    def test_unknown_send_stops_even_if_a_ledger_row_is_settled(self):
        failure,summary,calls=self.sweep([self.failed_answer(code='POST_SEND_RESULT_UNKNOWN'),{}])
        self.assertIsNotNone(failure);self.assertEqual(len(calls),1)
        self.assertFalse(failure['cost_certain']);self.assertEqual(summary['isolated_windows'],[])

    def test_open_reservation_stops_and_is_not_claimed_settled(self):
        failure,summary,calls=self.sweep([self.failed_answer(settled=False),{}])
        self.assertIsNotNone(failure);self.assertEqual(failure['budget_status'],'reserved')
        self.assertEqual(len(calls),1);self.assertEqual(summary['isolated_windows'],[])

    def test_no_formal_result_cannot_be_claimed_settled_from_a_budget_row(self):
        answer=self.failed_answer(formal=False)
        self.assertFalse(answer['formal_terminal']);self.assertIsNone(answer['model_budget'])
        failure,summary,calls=self.sweep([answer,{}])
        self.assertIsNotNone(failure);self.assertFalse(failure['cost_certain']);self.assertEqual(len(calls),1)

    def test_budget_refusal_stops_even_before_any_reservation(self):
        answer=self.failed_answer(code='DAY_BUDGET_EXCEEDED',reserve=False)
        failure,summary,calls=self.sweep([answer,{}])
        self.assertIsNotNone(failure);self.assertEqual(len(calls),1);self.assertEqual(summary['isolated_windows'],[])

    def test_explicit_pre_send_rejection_can_continue_without_reserving(self):
        answer=self.failed_answer(code='MODEL_ADAPTER_REJECTED',reserve=False,not_sent=True)
        failure,summary,calls=self.sweep([answer,{'status':'read','verified':[],'recorded':[],'replayed':False}])
        self.assertIsNone(failure);self.assertEqual(len(calls),2)
        self.assertEqual(summary['isolated_windows'][0]['budget_status'],'not_reserved')

    def test_consecutive_settled_failures_preserve_existing_limit(self):
        answer=self.failed_answer()
        failure,summary,calls=self.sweep([answer]*(CONSECUTIVE_CERTAIN_FAILURE_LIMIT+1))
        self.assertIsNotNone(failure);self.assertEqual(len(calls),CONSECUTIVE_CERTAIN_FAILURE_LIMIT)
        self.assertEqual(len(summary['isolated_windows']),CONSECUTIVE_CERTAIN_FAILURE_LIMIT)

    def test_success_resets_consecutive_failure_count(self):
        answer=self.failed_answer();success={'status':'read','verified':[],'recorded':[],'replayed':False}
        answers=[answer,success,answer] if CONSECUTIVE_CERTAIN_FAILURE_LIMIT>1 else [success]
        failure,summary,calls=self.sweep(answers)
        self.assertIsNone(failure);self.assertEqual(len(calls),len(answers))

    def test_no_reservation_alone_is_not_proof_a_send_never_happened(self):
        answer=self.failed_answer(code='MODEL_ADAPTER_REJECTED',reserve=False)
        self.assertFalse(answer['not_sent_proved'])
        failure,summary,calls=self.sweep([answer,{}])
        self.assertIsNotNone(failure);self.assertEqual(len(calls),1)
        self.assertEqual(summary['isolated_windows'],[])

    def test_generate_numeric_no_result_keeps_real_formal_error_and_exact_fee(self):
        from tests.test_document_numeric_extraction import WorkerDriftTests, SLOTS
        from dalton_core.document_numeric_extraction import build_work
        context=WorkerDriftTests().context()
        work=build_work(context,SLOTS,model_config=self.service.writer._document_extraction_model_config)
        self.formal_failure(work=work)
        self.service.writer._document_extraction_worker_factory=None
        with patch.object(self.service,'context',return_value=context), \
             patch.object(self.service,'_document_spec_ref',return_value='sell-side-reports'), \
             patch.object(self.service,'document_names_subject',return_value={'names_subject':True}), \
             patch.object(self.service,'numeric_slots',return_value=SLOTS), \
             patch.object(self.service,'_run_secondary',return_value=(None,False)):
            answer=self.service.generate_numeric(review_id='review:one',expected_review_hash='a'*64,
                offset=0,expected_context_hash=context['content_hash'],actor_ref='actor:fixture',source_grade='high')
        self.assertEqual(answer['status'],'no_result')
        self.assertEqual(answer['work_order_ref'],work.id)
        self.assertEqual(answer['error_code'],'MODEL_OUTPUT_CONTRACT_REJECTED')
        self.assertTrue(answer['formal_terminal'])
        self.assertEqual(answer['model_budget']['status'],'settled')
        self.assertEqual(answer['model_budget']['actual_micros'],4630)
