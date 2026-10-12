import hashlib,json,os,unittest
from unittest.mock import patch
from tests import test_deep_insight_gate_accounting as fixture
from dalton_core.deep_insight_gate_quiesce import RunQuiesce,GateQuiesced,GateQuiesceUnproved,request_quiesce,settled_call,quiesced_summary_matches
from dalton_core.store import content_hash
from dalton_core.cockpit_model import CockpitModelError


class DeepGatePauseTests(unittest.TestCase):
    def setUp(self):
        fixture.DeepGateAccountingTests.setUp(self)
        self.directory=self.root/'deep-run';self.directory.mkdir()
        self.producer=self.root/'producer.json';self.verifier=self.root/'verifier.json'
        self.producer.write_text('{}');self.verifier.write_text('{}')
        self.account=self.accounting()
        self.ticket={'id':'deep-insight-gate-run:'+'a'*24,'pid':os.getpid(),'started_at':'2026-10-12T11:00:00+00:00',
            'command':['python','-m','dalton_core.deep_insight_gate_cli'],
            'company_ref':'company:one','source_fingerprint':'f'*64,
            'model_config_path':str(self.producer),'model_config_fingerprint':hashlib.sha256(b'{}').hexdigest(),
            'verifier_model_config_path':str(self.verifier),'verifier_model_config_fingerprint':hashlib.sha256(b'{}').hexdigest(),
            'status':'running','exit_code':None,'completed_at':None}
        self.write_ticket();self.q=self.quiesce()

    kw=fixture.DeepGateAccountingTests.kw
    accounting=fixture.DeepGateAccountingTests.accounting

    def write_ticket(self):
        (self.directory/'ticket.json').write_text(json.dumps(self.ticket))

    def quiesce(self,**kwargs):
        return RunQuiesce(self.directory,self.producer,self.verifier,producer_bytes=kwargs.get('producer_bytes',b'{}'),
            verifier_bytes=b'{}',accounting=self.account,company_ref=kwargs.get('company_ref','company:one'),
            source_fingerprint=kwargs.get('source_fingerprint','f'*64))

    def request(self):
        with patch('dalton_core.lane_child_launcher.ticket_identity',return_value=True):
            return request_quiesce(self.directory,expected_ticket_hash=content_hash(self.ticket))

    def test_request_already_waiting_pauses_before_a_provider_call(self):
        request=self.request()
        with self.assertRaises(GateQuiesced)as caught:self.q.checkpoint()
        self.assertEqual(caught.exception.request,request);self.assertEqual(caught.exception.completed,[])
        self.assertEqual(self.adapter.served,[])

    def test_paid_success_boundary_and_free_original_formal_replay(self):
        call=self.model.call(**self.kw('industry'))
        self.q.returned(self.model,call,self.mission,purpose='deep_insight_gate')
        fees=fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self)
        self.request()
        with self.assertRaises(GateQuiesced)as caught:self.q.checkpoint()
        self.assertEqual(caught.exception.completed[0]['work_ref'],call['work_order_ref'])
        replay=self.account.replay(self.kw('industry'))
        self.assertEqual(replay['work_order_ref'],call['work_order_ref']);self.assertEqual(replay['cost_micros'],0)
        self.assertEqual(len(self.adapter.served),1);self.assertEqual(fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self),fees)

    def test_wrong_pid_start_config_company_or_source_cannot_prove_pause(self):
        self.request()
        for kwargs in ({'producer_bytes':b'changed'},{'company_ref':'company:other'},{'source_fingerprint':'e'*64}):
            with self.assertRaises(GateQuiesceUnproved):self.quiesce(**kwargs).checkpoint()
        self.ticket['pid']+=1;self.write_ticket()
        with self.assertRaises(GateQuiesceUnproved):self.q.checkpoint()
        self.ticket['pid']-=1;self.ticket['started_at']='later';self.write_ticket()
        with self.assertRaises(GateQuiesceUnproved):self.q.checkpoint()

    def test_unknown_failure_cannot_be_relabelled_resumable_pause(self):
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'timeout'}}
        with self.assertRaises(CockpitModelError):self.model.call(**self.kw('industry'))
        self.request()
        with self.assertRaisesRegex(GateQuiesceUnproved,'whole frozen-input'):
            self.q.checkpoint()
        self.assertEqual(len(self.adapter.served),1)

    def test_original_formal_and_admission_mission_hash_still_required(self):
        call=self.model.call(**self.kw('industry'))
        with self.assertRaises(ValueError):
            settled_call(self.model,{**call,'result_envelope_ref':'result:wrong'},self.mission,purpose='deep_insight_gate')
        with self.assertRaises(ValueError):
            settled_call(self.model,call,{**self.mission,'content_hash':'e'*64},purpose='deep_insight_gate')

    def test_terminal_ticket_projection_preserves_exact_pause_binding(self):
        request=self.request();summary={'status':'quiesced','resumable':True,'stop_reason':'cooperative_quiesce','quiesce':request}
        ticket={**self.ticket,'status':'succeeded','exit_code':0,'completed_at':'later','summary':summary}
        self.assertTrue(quiesced_summary_matches(summary,ticket))
        ticket['source_fingerprint']='e'*64
        self.assertFalse(quiesced_summary_matches(summary,ticket))
