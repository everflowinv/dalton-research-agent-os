"""Legacy parser failures reenter once through the governed, budgeted ladder."""
import json
import sqlite3
import unittest
from datetime import date
from unittest import mock
from dalton_core import material_requests as mr,material_acquisition as ma
from dalton_core import material_sec_amendment_recovery as recovery
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.mission_source_discovery import discovery_query_hash
from dalton_core.sec_public_adapter import SecPublicAdapterError
from dalton_core.store import DaltonStore,canonical_json,content_hash
from tests import test_sec_filings_index_discovery as fixture,test_material_targeted_sec as targeted

START='2026-09-06T00:00:00+00:00';NOW='2026-09-08T00:00:00+00:00'

class AmendmentRecoveryTests(unittest.TestCase):
    setUp=fixture.SecFilingsIndexDiscoveryTests.setUp
    queued=targeted.TargetedSecTests.queued
    params=targeted.TargetedSecTests.params
    run_once=fixture.SecFilingsIndexDiscoveryTests.run_once

    def failed(self,closed=True):
        folder=self.state/'discovery-plans';folder.mkdir(exist_ok=True)
        self.plan=fixture.sec_plan();(folder/'us-it-services-sec-filings-v1.json').write_text(json.dumps(self.plan))
        self.r,self.q=self.queued(unit_proof_rule_ref='sec-filing-units:exact-definition-and-fact:1')
        p,b=self.params(self.q);self.binding=b
        self.ticket_ref='sec-filings-index:'+'a'*24
        self.directory=self.state/'discoveries'/('a'*24);self.directory.mkdir(parents=True)
        with mock.patch('dalton_core.sec_public_adapter.normalize_sec_submissions',side_effect=SecPublicAdapterError(recovery.ERROR['message'])):
            summary=self.run_once(summary_dir=self.directory,targeted_binding=b)
        self.assertEqual(summary['status'],'failed')
        (self.directory/'ticket.json').write_text(json.dumps({'id':self.ticket_ref,'status':'failed','exit_code':1,'targeted_binding':b}))
        with DaltonStore(str(self.state/'core.sqlite'))as store:
            auth=CoverageMissionAuthority(store);grant=auth.authorize_source_discovery(company_ref=fixture.ACN,
                source_ref='source:sec-edgar',requested_by=self.principal,mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'])
            d=auth.record_discovery_dispatch(authorization=grant,discovery_plan_ref=self.plan['id'],discovery_plan_hash=self.plan['content_hash'],
                spec_ref='annual-report-10k',query_hash=discovery_query_hash(self.plan,p),ticket_ref=self.ticket_ref)
            auth.settle_discovery_dispatch(d['dispatch_id'],status='failed',reason='index outcome failed')
        ma.record_targeted_event(self.state,self.q['query_id'],status='launched',ticket_ref=self.ticket_ref,dispatch_ref=d['dispatch_id'],extra={'targeted_binding':b},now=START)
        mr.record_attempt(self.state,self.r['id'],source='indexed_corpus',outcome='empty',calls=1,cost_micros=3,now=START)
        ma._record_spend(self.state,self.r['id'],'indexed_corpus',1,3,now=START)
        mr.record_attempt(self.state,self.r['id'],source='sec_edgar',outcome='dispatched',query={'targeted_query':self.q['query_id']},calls=1,now=START)
        ma._record_spend(self.state,self.r['id'],'sec_edgar',1,0,now=START)
        if closed:
            mr.record_attempt(self.state,self.r['id'],source='sec_edgar',outcome='error',detail='检索失败',now=START)
            mr.close(self.state,self.r['id'],status='exhausted',reason='old exact parser failure',now=START)
        self.current_need=mock.patch('dalton_core.material_model_unit_priority.unit_input_proof',return_value={'strong_fixture_need':True})
        self.current_need.start();self.addCleanup(self.current_need.stop)
        self.core=DaltonStore(str(self.state/'core.sqlite'));self.addCleanup(self.core.close)
        self.scheduler=sqlite3.connect(self.state/'scheduler.sqlite');self.scheduler.execute('PRAGMA journal_mode=WAL');self.scheduler.execute('PRAGMA user_version=0');self.scheduler.commit();self.addCleanup(self.scheduler.close)
        self.summary=summary
        return mr.request(self.state,self.r['id'])

    def test_normal_retry_sweep_reopens_once_preserving_history_unknowns_and_spend(self):
        view=self.failed();before=view['events'];spend=ma.request_spend(self.state,view['id'])
        with sqlite3.connect(self.state/'core.sqlite')as c:old=list(c.execute('SELECT * FROM connector_quota_settlements'))
        restored=ma.retry_sweep(self.state,now=NOW)
        self.assertEqual(restored,[{'request_id':view['id'],'reason':recovery.RULE_REF}])
        fresh=mr.request(self.state,view['id']);self.assertEqual(fresh['events'][:-1],before)
        self.assertGreater(fresh['deadline'],NOW);self.assertEqual(ma.request_spend(self.state,view['id']),spend)
        self.assertIsNone(recovery.recover(self.state,fresh,now=NOW))
        with sqlite3.connect(self.state/'core.sqlite')as c:self.assertEqual(list(c.execute('SELECT * FROM connector_quota_settlements')),old)
        sec=ma.TargetedSourceRung('sec_edgar');out=ma.serve(self.state,view['id'],now=NOW,rungs={'sec_edgar':sec})
        self.assertEqual(out['outcome'],'waiting')
        q2=ma._targeted_query_id(ma._round_attempts(mr.request(self.state,view['id']))[-1]);self.assertNotEqual(q2,self.q['query_id'])
        self.assertIn(q2,{q['query_id']for q in ma.pending_targeted_queries(self.state,'source:sec-edgar')})
        # The new ordinary fetch can settle normally, without changing the
        # failed Work or manufacturing success from its missing raw object.
        from dalton_core.material_targeted_sec import targeted_sec_parameters
        _,binding=targeted_sec_parameters(self.state,plan=self.plan,company_ref=view['subject'],spec_ref='annual-report-10k',
            as_of=date(2026,9,8),mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'],query_ref=q2)
        result=self.run_once(summary_dir=self.state/'new-original',targeted_binding=binding)
        self.assertEqual((result['status'],result['provider_calls']),('succeeded',1))
        self.assertEqual(json.loads((self.directory/'summary.json').read_text())['status'],'failed')

    def test_open_valid_window_is_not_extended_and_expired_window_is_not_reopened(self):
        v=self.failed(closed=False);old=v['deadline']
        self.assertIsNone(recovery.recover(self.state,v,now=NOW)) # expired open round
        self.assertIsNotNone(recovery.recover(self.state,v,now=START));self.assertEqual(mr.request(self.state,v['id'])['deadline'],old)

    def test_current_gap_resolved_or_current_grant_revoked_refuses_without_history_change(self):
        v=self.failed();old=v['events']
        with mock.patch('dalton_core.material_model_unit_priority.unit_input_proof',return_value=None):
            self.assertIsNone(recovery.recover(self.state,v,now=NOW))
        with mock.patch('dalton_core.material_source_queue_recovery._configured_sec',side_effect=ValueError('permission revoked')):
            self.assertIsNone(recovery.recover(self.state,v,now=NOW))
        self.assertEqual(mr.request(self.state,v['id'])['events'],old)

    def test_existing_pool_budget_still_refuses_before_new_query(self):
        v=self.failed();(self.state/'material-acquisition-pool.json').write_text(json.dumps({'calls_per_day':0}))
        sec=ma.TargetedSourceRung('sec_edgar')
        with mock.patch.object(sec,'run',wraps=sec.run)as run:out=ma.serve(self.state,v['id'],now=NOW,rungs={'sec_edgar':sec})
        self.assertEqual(out['outcome'],'budget_blocked');run.assert_not_called()
        self.assertEqual(len(ma.pending_targeted_queries(self.state,'source:sec-edgar')),0)

    def test_missing_formal_or_tampered_formal_receipt_blocks(self):
        v=self.failed()
        with sqlite3.connect(self.state/'scheduler.sqlite')as c:
            names=[r[0]for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='scheduler_result_envelopes'")]
            for name in names:c.execute('DROP TRIGGER '+name)
            c.execute('UPDATE scheduler_result_envelopes SET content_hash=?',('f'*64,))
        self.assertIsNone(recovery.recover(self.state,v,now=NOW))

    def test_ticket_query_company_hash_and_unknown_parent_tampering_blocks(self):
        v=self.failed();p=self.directory/'ticket.json';body=json.loads(p.read_text())
        for key,value in [('query_ref','material-targeted-query:foreign'),('company_ref','company:other'),('request_hash','f'*64)]:
            changed=json.loads(json.dumps(body));changed['targeted_binding'][key]=value;p.write_text(json.dumps(changed))
            with self.subTest(key=key):self.assertIsNone(recovery.recover(self.state,v,now=NOW))
        p.write_text(json.dumps(body));self.assertIsNotNone(recovery.qualification(self.state,v,now=NOW).get('proof'))

    def test_rehashed_generic_provider_failure_is_not_the_repaired_parser_error(self):
        v=self.failed()
        with sqlite3.connect(self.state/'core.sqlite')as c:
            c.row_factory=sqlite3.Row;c.execute('DROP TRIGGER runner_attempt_journal_no_update')
            rows=c.execute('SELECT * FROM runner_attempt_journal_events ORDER BY event_seq').fetchall()
            for n,row in enumerate(rows,1):
                if row['state']!='observed':continue
                b=json.loads(row['payload_json']);b['error']['message']='provider timeout';b['observation']['error']['message']='provider timeout'
                obs=b['observation'];obs['content_hash']=content_hash({k:v for k,v in obs.items()if k!='content_hash'})
                base={'runner_request_ref':row['runner_request_ref'],'request_ordinal':n,'state':row['state'],
                    'reservation_ref':row['reservation_ref'],'event_at':row['event_at'],'recorded_at':row['created_at'],'payload':b}
                digest=content_hash(base);event='runner-journal-event:'+content_hash({'runner_request_ref':row['runner_request_ref'],'request_ordinal':n,'content_hash':digest})
                c.execute('UPDATE runner_attempt_journal_events SET payload_json=?,content_hash=?,event_id=? WHERE event_seq=?',
                    (canonical_json(b),digest,event,row['event_seq']))
        self.assertIsNone(recovery.recover(self.state,v,now=NOW))

    def test_inflight_ticket_and_incomplete_journal_are_not_terminal_proof(self):
        v=self.failed();p=self.directory/'ticket.json';body=json.loads(p.read_text());body['status']='running';p.write_text(json.dumps(body))
        self.assertIsNone(recovery.recover(self.state,v,now=NOW))
        body['status']='failed';p.write_text(json.dumps(body))
        with sqlite3.connect(self.state/'core.sqlite')as c:
            c.execute('DROP TRIGGER runner_attempt_journal_no_delete')
            c.execute("DELETE FROM runner_attempt_journal_events WHERE state='responded'")
        self.assertIsNone(recovery.recover(self.state,v,now=NOW))

    def test_new_failure_or_unrelated_plan_revision_does_not_reset_once_key(self):
        v=self.failed();self.assertIsNotNone(recovery.recover(self.state,v,now=NOW))
        mr.record_attempt(self.state,v['id'],source='sec_edgar',outcome='error',detail='another failure',now=NOW)
        mr.close(self.state,v['id'],status='exhausted',reason='same source remains failed',now=NOW)
        plan=dict(self.plan);plan['budget']={'max_calls_24h':200};plan['content_hash']=content_hash({k:v for k,v in plan.items()if k!='content_hash'})
        (self.state/'discovery-plans'/'us-it-services-sec-filings-v1.json').write_text(json.dumps(plan))
        self.assertIsNone(recovery.recover(self.state,mr.request(self.state,v['id']),now=NOW))

    def test_cas_source_change_appends_nothing(self):
        v=self.failed();proof=recovery.qualification(self.state,v,now=NOW)['proof'];old=v['events']
        with mock.patch.object(recovery,'qualification',side_effect=[{'status':'eligible','proof':proof},{'status':'ineligible'}]):
            self.assertIsNone(recovery.recover(self.state,v,now=NOW))
        self.assertEqual(mr.request(self.state,v['id'])['events'],old)
