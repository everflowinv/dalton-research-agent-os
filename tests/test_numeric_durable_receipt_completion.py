"""Expired local completion reuses one Work, result and paid prefix."""
import copy
import json
import types
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from tests import test_directed_numeric_source_unit as unit_fixture_module
from dalton_core.store import canonical_json, content_hash
from dalton_core.scheduler import LeaseExpired
from dalton_core.directed_numeric_research import DirectedNumericError
from dalton_core.directed_numeric_local_reconciliation import qualification
from dalton_core import directed_numeric_executor as numeric


class DurableReceiptCompletionTests(unittest.TestCase):
    fixture=unit_fixture_module.SourceUnitTests.fixture
    attempted=unit_fixture_module.SourceUnitTests.attempted
    call=unit_fixture_module.SourceUnitTests.call
    unit_fixture=unit_fixture_module.SourceUnitTests.unit_fixture

    def expired(self,company='wanhua'):
        fixture=self.unit_fixture(company=company)
        h,w,f,a,admission,ex,p,v,works,old,args=fixture
        real=numeric._record_numeric
        def slow(*args):
            result=real(*args)
            f.harness.clock.advance(61)
            return result
        with patch.object(numeric,'_record_numeric',side_effect=slow):
            with self.assertRaises(LeaseExpired):self.call(ex,admission)
        _,local_works,auth=qualification(ex,admission['id'],admission['content_hash'])
        row=ex.connection.execute('SELECT * FROM mission_document_numeric_results WHERE admission_ref=?',(admission['id'],)).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(ex.scheduler.formal_result(local_works[3]['id']))
        self.assertEqual(ex.scheduler.status(local_works[3]['id'])['state'],'ready')
        self.assertEqual(ex.scheduler.status(local_works[3]['id'])['attempt_number'],2)
        return fixture,local_works,auth,dict(row)

    def test_two_companies_same_work_free_completion_no_duplicate_rows_or_old_fees(self):
        for company in ('wanhua','another-company'):
            with self.subTest(company=company):
                fixture,works,auth,receipt=self.expired(company)
                h,w,f,a,admission,ex,p,v,_,old,args=fixture
                tables=('mission_document_numeric_results','mission_document_numeric_local_reconciliations',
                    'coverage_mission_document_figures','mission_document_source_unit_figures',
                    'claim_versions','evidence_versions','reviewed_candidate_commits',
                    'model_invocations','observability_usage_entries','observability_cost_entries')
                # Source-unit table name is owned by its authority contract.
                from dalton_core.directed_numeric_source_unit import TABLE
                tables=tuple(TABLE if t=='mission_document_source_unit_figures' else t for t in tables)
                before={t:h.rows(ex.connection,t) for t in tables}
                old_work=ex.scheduler.work_order_authority(works[3]['id'])
                with patch.object(numeric,'_record_numeric',side_effect=AssertionError('no second write')),\
                     patch.object(ex.draft_worker,'run_once',side_effect=AssertionError('no producer')),\
                     patch.object(ex.verifier_worker,'run_once',side_effect=AssertionError('no verifier')):
                    result=self.call(ex,admission)
                    self.assertEqual(result['model_calls'],0)
                    self.assertEqual(result['research_status'],'numeric_targets_completed')
                    self.assertEqual(result['numeric_result']['content_hash'],receipt['content_hash'])
                    self.assertEqual(self.call(ex,admission),result)
                self.assertEqual({t:h.rows(ex.connection,t) for t in tables},before)
                self.assertEqual(ex.scheduler.work_order_authority(works[3]['id']),old_work)
                formal=ex.scheduler.formal_result(works[3]['id'])
                self.assertEqual(formal['attempt_number'],2)
                self.assertEqual(formal['terminal_state'],'succeeded')
                self.assertEqual((p.calls,v.calls),(1,1))

    def test_full_current_receipt_revalidation_before_claim_and_revoked_authority_refuses(self):
        fixture,works,auth,receipt=self.expired()
        h,w,f,a,admission,ex,p,v,_,old,args=fixture
        history=ex.scheduler.attempt_history(works[3]['id'])
        with patch.object(a,'resolve_for_execution',side_effect=DirectedNumericError('withdrawn')):
            with self.assertRaisesRegex(DirectedNumericError,'withdrawn'):self.call(ex,admission)
        self.assertEqual(ex.scheduler.attempt_history(works[3]['id']),history)
        with patch.object(ex.registry,'verify_search_proof',side_effect=DirectedNumericError('source unavailable')):
            with self.assertRaisesRegex(DirectedNumericError,'source unavailable'):self.call(ex,admission)
        self.assertEqual(ex.scheduler.attempt_history(works[3]['id']),history)
        real=numeric._verified_numeric_receipt
        def slow(*args):
            result=real(*args);f.harness.clock.advance(35);return result
        with patch.object(numeric,'_verified_numeric_receipt',side_effect=slow):
            done=self.call(ex,admission)
        self.assertEqual(done['research_status'],'numeric_targets_completed')
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_rehashed_receipt_canonical_change_refuses_before_new_lease(self):
        fixture,works,auth,receipt=self.expired()
        h,w,f,a,admission,ex,p,v,_,old,args=fixture
        h.corruptible_copy(ex.connection,'mission_document_numeric_results')
        body=json.loads(receipt['record_json']);body['completed_targets'][0]['figure_hash']='f'*64
        body['content_hash']=content_hash({k:v for k,v in body.items() if k!='content_hash'})
        ex.connection.execute('UPDATE mission_document_numeric_results SET record_json=?,content_hash=?',(canonical_json(body),body['content_hash']));ex.connection.commit()
        history=ex.scheduler.attempt_history(works[3]['id'])
        with self.assertRaisesRegex(DirectedNumericError,'Figure differs'):self.call(ex,admission)
        self.assertEqual(ex.scheduler.attempt_history(works[3]['id']),history)
        self.assertIsNone(ex.scheduler.formal_result(works[3]['id']))

    def test_initial_local_claim_uses_only_frozen_maximum_and_work_budget(self):
        fixture=self.unit_fixture()
        h,w,f,a,admission,ex,p,v,works,old,args=fixture
        policy=h.rows(ex.connection,'scheduler_policy_versions')
        real=numeric._record_numeric
        def slow(*args):
            result=real(*args);f.harness.clock.advance(35);return result
        with patch.object(numeric,'_record_numeric',side_effect=slow):done=self.call(ex,admission)
        self.assertEqual(done['research_status'],'numeric_targets_completed')
        self.assertEqual(h.rows(ex.connection,'scheduler_policy_versions'),policy)
        self.assertEqual((p.calls,v.calls),(1,1))

    def lane_fixture(self):
        from dalton_core.mission_document_research_launcher import MissionDocumentResearchLauncher
        from dalton_core.mission_document_research_lane import MissionDocumentResearchCoordinator,_write_holds
        from dalton_core.document_completed_model_replay import candidate,UNIT_ERROR,UNIT_CONTRACT
        fixture=self.unit_fixture()
        h,w,f,a,admission,ex,p,v,works,old,args=fixture
        directory=f.state/'receipt-tickets';directory.mkdir()
        launcher=MissionDocumentResearchLauncher.__new__(MissionDocumentResearchLauncher)
        launcher.tickets_dir=directory
        launcher.configuration=lambda:{'test':'no physical process'}
        states={}
        launcher.status=lambda ref:states[ref]
        def save(ref,record,error):
            path=launcher._ticket_path(ref);path.parent.mkdir(exist_ok=True)
            body={'status':'failed','error':error,'admission_ref':admission['id'],'admission_hash':admission['content_hash']}
            summary={**body,'content_hash':content_hash(body)}
            path.with_name('summary.json').write_text(json.dumps(summary))
            states[ref]={'id':ref,'status':'failed','exit_code':1,'completed_at':f.harness.clock().isoformat(),
                'admission_ref':admission['id'],'admission_hash':admission['content_hash'],**record,'summary':summary}
            path.write_text(json.dumps(states[ref]))
        original='mission-document-research:old';save(original,{},UNIT_ERROR)
        c=MissionDocumentResearchCoordinator(store=f.store,launcher=launcher,clock=f.harness.clock)
        proof=candidate(c,admission,original)
        fixed=launcher.TICKET_PREFIX+':'+content_hash({k:proof[k] for k in ('contract_ref','admission_ref','admission_hash')})[:24]
        real=numeric._record_numeric
        def slow(*args):
            result=real(*args);f.harness.clock.advance(61);return result
        with patch.object(numeric,'_record_numeric',side_effect=slow):
            with self.assertRaises(LeaseExpired):self.call(ex,admission)
        from dalton_core.document_numeric_receipt_completion import ERROR
        save(fixed,{'completed_model_replay':proof},ERROR)
        from dalton_core.mission_document_research_promotion import ensure_promotion_authority
        ensure_promotion_authority(ex.connection)
        _write_holds(c.holds_path,{admission['id']:{'reason':'reentry_failed_after_automatic_rebind:controlled reentry was already attempted',
            'admission_hash':admission['content_hash'],'ticket_ref':original,'disposition':'recovery_required','retry_at':None}})
        return fixture,c,states,original,fixed

    def test_real_coordinator_cli_fixed_free_resume_then_normal_hold_retirement(self):
        from dalton_core.document_completed_model_replay import candidate
        from dalton_core.document_numeric_receipt_completion import CONTRACT
        from dalton_core.mission_document_research_cli import run_admission
        from dalton_core.mission_document_research_lane import _read_holds
        fixture,c,states,original,fixed=self.lane_fixture()
        h,w,f,a,admission,ex,p,v,works,old,args=fixture
        hint=candidate(c,admission,original)
        self.assertEqual(hint['contract_ref'],CONTRACT)
        before=h.rows(ex.connection,'mission_document_numeric_results');started=[]
        def spawn(**kw):
            self.assertEqual(kw['record']['contract_ref'],CONTRACT)
            self.assertTrue(kw['completed_models_only']);started.append(kw)
            runtime=types.SimpleNamespace(admission=admission,executor=ex)
            with patch('dalton_core.mission_document_research_cli.MissionDocumentResearchRuntime',return_value=nullcontext(runtime)),patch.object(ex,'run_once',side_effect=AssertionError('no paid loop')):
                done=run_admission(state_dir=f.state,staging=f.state/'unused',planner_scheduler_db=f.state/'unused',
                    planner_model_config=f.state/'unused',document_research_config=f.state/'unused',admission_ref=admission['id'],expected_admission_hash=admission['content_hash'],completed_models_only=True)
            ref=c.launcher.TICKET_PREFIX+':'+kw['digest'];path=c.launcher._ticket_path(ref);path.parent.mkdir()
            states[ref]={'id':ref,'status':'succeeded','summary':done};path.write_text('{}')
            return states[ref]
        c.launcher.spawn=spawn
        with patch.object(numeric,'_record_numeric',side_effect=AssertionError('no duplicate write')):
            self.assertEqual(c.dispatch_once()['reason'],'durable_numeric_receipt_completion')
            c.dispatch_once()
        self.assertEqual(len(started),1)
        self.assertIsNone(candidate(c,admission,original))
        self.assertEqual(h.rows(ex.connection,'mission_document_numeric_results'),before)
        self.assertNotIn(admission['id'],_read_holds(c.holds_path))
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_failed_receipt_gate_does_not_accept_different_summary_proof_active_or_exhausted(self):
        from dalton_core.document_completed_model_replay import candidate
        fixture,c,states,original,fixed=self.lane_fixture()
        h,w,f,a,admission,ex,p,v,works,old,args=fixture
        original_state=copy.deepcopy(states[fixed])
        for field,value in (('status','running'),('exit_code',0),('completed_model_replay',{})):
            with self.subTest(field=field):
                states[fixed]={**original_state,field:value}
                self.assertIsNone(candidate(c,admission,original))
        states[fixed]=original_state
        _,local_works,_=qualification(ex,admission['id'],admission['content_hash'])
        ex.scheduler.claim(ex.actor_ref,work_order_id=local_works[3]['id'])
        self.assertIsNone(candidate(c,admission,original))
        for _ in range(5):
            f.harness.clock.advance(1800)
            ex.scheduler.claim(ex.actor_ref,work_order_id=local_works[3]['id'])
        self.assertIsNone(candidate(c,admission,original))
        self.assertEqual((p.calls,v.calls),(1,1))
