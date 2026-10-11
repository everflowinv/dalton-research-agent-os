"""Synthetic durable-boundary stops; no signal or external model transport."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from dalton_core.extraction_quiesce import (
    ExtractionQuiesced, RunQuiesce, request_quiesce, quiesced_summary_matches)
from dalton_core.store import content_hash
from dalton_core.document_extraction_cli import _secondary_sweep
from dalton_core.document_extraction_launcher import _quiesced_child
from dalton_core.claim_support_verification import ClaimSupportVerifier
from tests import test_claim_support_verification as support_fixture


class ExtractionCooperativeQuiesceTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.config=self.root/'model.json'
        self.ticket={'id':'document-extraction:'+'1'*24,'pid':os.getpid(),
                     'started_at':'2026-10-11T03:00:00+00:00',
                     'model_config_path':str(self.config),'model_config_fingerprint':'a'*64,
                     'status':'running','exit_code':None,'completed_at':None}
        (self.root/'ticket.json').write_text(json.dumps(self.ticket))
        self.control=RunQuiesce(self.root,self.config)

    def request(self):
        return request_quiesce(self.root,expected_ticket_hash=content_hash(self.ticket))

    def test_bound_request_is_atomic_idempotent_and_does_not_mutate_ticket(self):
        before=(self.root/'ticket.json').read_bytes()
        first=self.request();self.assertEqual(self.request(),first)
        self.assertEqual((self.root/'ticket.json').read_bytes(),before)
        with self.assertRaises(ExtractionQuiesced) as stopped:self.control.checkpoint()
        self.assertEqual(stopped.exception.proof,first)
        self.assertEqual(list(self.root.glob('.quiesce-*')),[])
        self.assertEqual((self.root/'quiesce-request.json').stat().st_mode & 0o777,0o600)

    def test_wrong_ticket_hash_pid_config_and_tampered_request_fail_closed(self):
        with self.assertRaises(ValueError):request_quiesce(self.root,expected_ticket_hash='0'*64)
        request=self.request()
        for field,value in (('pid',os.getpid()+1),('model_config_path','other.json')):
            with self.subTest(field=field):
                ticket={**self.ticket,field:value};(self.root/'ticket.json').write_text(json.dumps(ticket))
                with self.assertRaises(ValueError):self.control.checkpoint()
        (self.root/'ticket.json').write_text(json.dumps(self.ticket))
        request['ticket_binding']['started_at']='another launch'
        (self.root/'quiesce-request.json').write_text(json.dumps(request))
        with self.assertRaises(ValueError):self.control.checkpoint()

    def test_no_request_is_exact_default_noop(self):
        self.control.checkpoint()
        self.assertFalse((self.root/'quiesce-request.json').exists())

    def test_secondary_finishes_current_window_before_stopping_without_exhausting_review(self):
        calls=[];summary={'numeric':[],'figures':0,'numeric_fresh':0};review={'review_id':'review:one','document_ref':'document:one','source_ref':'source:fixture'}
        def view(**p):return {'context':{'content_hash':'a'*64,'next_offset':12000}}
        def generate(**p):
            calls.append(p['offset']);self.request()
            return {'status':'read','verified':[],'refused':[],'recorded':[],'replayed':False}
        service=SimpleNamespace(view=view,generate_numeric=generate)
        with self.assertRaises(ExtractionQuiesced):
            _secondary_sweep(service,[('automation:test',[review],{})],summary,
                limit=3,entries='numeric',wanted=lambda *args:True,call='generate_numeric',
                counts={'verified':'verified'},total=('figures','recorded'),spent_key='numeric_fresh',
                checkpoint=self.control.checkpoint)
        self.assertEqual(calls,[0])
        self.assertEqual(len(summary['numeric']),1)
        self.assertEqual(summary['numeric_fresh'],1)
        self.assertNotIn('exhausted_reviews',summary)

    def verifier(self, model, *, second_opinion=False):
        store=support_fixture._store();self.addCleanup(store.connection.close)
        verifier=ClaimSupportVerifier(store=store,model_call=model,daily_cap_micros=1_000_000,
                                      max_items_per_call=1,checkpoint=self.control.checkpoint,
                                      second_opinion=second_opinion)
        return verifier

    def test_support_current_chunk_verdict_is_durable_before_next_chunk_stop(self):
        model=support_fixture.FakeModel(support_fixture._reply(('supported','about_subject',None)))
        original=model.__call__
        def completed(**kwargs):
            result=original(**kwargs);self.request();return result
        verifier=self.verifier(completed)
        items=[support_fixture._item(1),support_fixture._item(2)]
        with self.assertRaises(ExtractionQuiesced):verifier.verify(mission=support_fixture.MISSION,items=items)
        self.assertEqual(len(model.calls),1)
        saved=verifier.verdicts([i['item_key']for i in items])
        self.assertIn(items[0]['item_key'],saved);self.assertNotIn(items[1]['item_key'],saved)
        (self.root/'quiesce-request.json').unlink()
        verifier.model_call=support_fixture.FakeModel(support_fixture._reply(('supported','about_subject',None)))
        result=verifier.verify(mission=support_fixture.MISSION,items=items)
        self.assertEqual(result['status'],'verified');self.assertEqual(result['calls'],1)
        self.assertEqual(len(result['verdicts']),2)

    def test_exact_partial_receipt_adopts_quiesced_not_full_success(self):
        request=self.request();summary={'status':'quiesced','stop_reason':'cooperative_quiesce',
                                      'resumable':True,'quiesce':request,'reviews_complete':0}
        (self.root/'summary.json').write_text(json.dumps(summary))
        ticket=dict(self.ticket)
        self.assertTrue(_quiesced_child(ticket,self.root/'summary.json',now='2026-10-11T03:05:00+00:00'))
        self.assertEqual(ticket['status'],'quiesced');self.assertEqual(ticket['exit_code'],0)
        self.assertTrue(quiesced_summary_matches(summary,ticket))
        summary['quiesce']['ticket_binding']['pid']+=1
        (self.root/'summary.json').write_text(json.dumps(summary))
        self.assertFalse(_quiesced_child(dict(self.ticket),self.root/'summary.json',now='later'))

    def test_second_opinion_waits_after_durable_first_opinion_and_resumes_without_repaying_first(self):
        from tests import test_claim_support_second_opinion as second_fixture
        model=second_fixture.RoutedModel(support_fixture._reply(second_fixture.NO),support_fixture._reply(second_fixture.YES))
        def first_finishes(**kwargs):
            answer=model(**kwargs);self.request();return answer
        verifier=self.verifier(first_finishes,second_opinion=True)
        verifier.producer_family=lambda ref:second_fixture.FAMILIES[ref]
        verifier.second_opinion_route=lambda families:None
        item=support_fixture._item(1)
        with self.assertRaises(ExtractionQuiesced):verifier.verify(mission=support_fixture.MISSION,items=[item])
        self.assertEqual(len(model.calls),1)
        self.assertIn(item['item_key'],verifier.records.first_opinions([item['item_key']]))
        self.assertNotIn(item['item_key'],verifier.verdicts([item['item_key']]))
        (self.root/'quiesce-request.json').unlink()
        verifier.model_call=model
        result=verifier.verify(mission=support_fixture.MISSION,items=[item])
        self.assertEqual(result['status'],'verified');self.assertEqual(result['calls'],1)
        self.assertEqual(len(model.calls),2)
        self.assertIn('second',model.calls[1]['request_id'])

    def test_real_run_persists_current_formal_then_quiesces_and_next_run_only_executes_remaining_window(self):
        from tests import test_document_extraction_automation as automation_fixture
        from dalton_core.document_extraction import HermeticExtractionAdapter
        from dalton_core.document_extraction_cli import run_extraction
        fixture=automation_fixture.AutomationDraftingTests('test_empty_offset_zero_rendering_is_parked_without_a_read_or_claim')
        fixture.setUp();self.addCleanup(fixture.doCleanups);fixture._grant_automation()
        model_config=fixture._model_config();output=fixture.root/'empty-suggestions.json'
        output.write_text('{"schema_version":"0.1","suggestions":[]}')
        first_dir=fixture.root/'quiesce-run';first_dir.mkdir()
        ticket={**self.ticket,'model_config_path':str(model_config)}
        (first_dir/'ticket.json').write_text(json.dumps(ticket))
        original=HermeticExtractionAdapter.execute;calls=[]
        def model_finishes(adapter,work,route,profile):
            result=original(adapter,work,route,profile);calls.append(work.id)
            if len(calls)==1:request_quiesce(first_dir,expected_ticket_hash=content_hash(ticket))
            return result
        params=dict(state_dir=fixture.root,model_config_path=model_config,
                    spool_dir=fixture.root/'spool',scheduler_db=fixture.root/'scheduler.sqlite',
                    requested_by=None,max_windows=2,max_numeric_windows=0,max_discovery_windows=0,
                    connector_governance=None,web_fetch_governance=None,hermetic_fixture=output)
        with patch.object(HermeticExtractionAdapter,'execute',model_finishes):
            first=run_extraction(**params,summary_dir=first_dir)
            self.assertEqual(first['status'],'quiesced');self.assertTrue(first['resumable'])
            self.assertEqual(first['reviews_complete'],0)
            self.assertEqual(len(first['drafted']),1)
            self.assertEqual(first['formal_authority_writes'],0)
            formal=fixture.h.h.scheduler.formal_result(calls[0]);self.assertEqual(formal['terminal_state'],'succeeded')
            second=run_extraction(**params,summary_dir=fixture.root/'resume-run')
        self.assertEqual(second['status'],'succeeded');self.assertEqual(second['reviews_complete'],1)
        self.assertEqual(len(calls),2)
        self.assertEqual(fixture.h.h.scheduler.formal_result(calls[0]),formal)
        self.assertEqual(first,json.loads((first_dir/'summary.json').read_text()))
