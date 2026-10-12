"""Queue delay is not proof that an authorized SEC connector is absent."""
import json
import sqlite3
import unittest
from unittest import mock
from dalton_core import material_acquisition as ma, material_requests as mr
from dalton_core import material_source_queue_recovery as recovery
from tests import test_material_targeted_sec as targeted
from tests import test_sec_filings_index_discovery as fixture

START='2026-09-06T00:00:00+00:00'
NOW='2026-09-08T00:00:00+00:00'

class QueueRecoveryTests(unittest.TestCase):
    setUp=fixture.SecFilingsIndexDiscoveryTests.setUp
    queued=targeted.TargetedSecTests.queued
    def plan(self):
        directory=self.state/'discovery-plans';directory.mkdir(exist_ok=True)
        (directory/'us-it-services-sec-filings-v1.json').write_text(json.dumps(fixture.sec_plan()))
    def setup_query(self,closed=False):
        self.plan();r,q=self.queued()
        mr.record_attempt(self.state,r['id'],source='indexed_corpus',outcome='empty',cost_micros=3,calls=1,now=START)
        ma.ensure_schema(self.state)
        ma._record_spend(self.state,r['id'],'indexed_corpus',1,3,now=START)
        mr.record_attempt(self.state,r['id'],source='sec_edgar',outcome='dispatched',query={'targeted_query':q['query_id']},calls=1,now=START)
        ma._record_spend(self.state,r['id'],'sec_edgar',1,0,now=START)
        if closed:
            mr.record_attempt(self.state,r['id'],source='sec_edgar',outcome='not_connected',detail=recovery.LEGACY_TIMEOUT_DETAIL,now=NOW)
            mr.close(self.state,r['id'],status='exhausted',reason='source ladder exhausted',now=NOW)
        return mr.request(self.state,r['id']),q
    def settle(self,r,q):
        previous=next(e for e in reversed(r['events'])if e.get('outcome')=='dispatched')
        return ma.TargetedSourceRung('sec_edgar').settle(ma.RungContext(self.state,r,{},[],NOW,previous))
    def test_authorized_queue_older_than_three_hours_waits_without_new_attempt(self):
        r,q=self.setup_query();before=mr.request(self.state,r['id'])['events']
        self.assertEqual(self.settle(r,q).outcome,'waiting')
        self.assertEqual(mr.request(self.state,r['id'])['events'],before)
    def test_absent_plan_retains_not_connected_but_unreadable_plan_waits_unverified(self):
        r,q=self.setup_query();path=self.state/'discovery-plans'/'us-it-services-sec-filings-v1.json'
        path.unlink();self.assertEqual(self.settle(r,q).outcome,'not_connected')
        path.write_text('{');result=self.settle(r,q)
        self.assertEqual(result.outcome,'waiting');self.assertIn('暂未核实',result.detail)
    def test_busy_queue_waits_but_budget_denial_remains_rate_limited(self):
        r,q=self.setup_query()
        ma.record_targeted_event(self.state,q['query_id'],status='deferred',kind='busy',now=NOW)
        self.assertEqual(self.settle(r,q).outcome,'waiting')
        ma.record_targeted_event(self.state,q['query_id'],status='deferred',kind='budget',reason='source call budget exhausted',now=NOW)
        self.assertEqual(self.settle(r,q).outcome,'rate_limited')
    def test_closed_queue_recovers_once_and_carries_other_source_spend(self):
        r,q=self.setup_query(closed=True)
        ma.record_targeted_event(self.state,q['query_id'],status='refused',kind='request_binding',reason='material request is no longer active',now=NOW)
        old_events=r['events'];old_spend=ma.request_spend(self.state,r['id'])
        proof=recovery.recover(self.state,r,now=NOW);self.assertIsNotNone(proof)
        fresh=mr.request(self.state,r['id']);self.assertFalse(fresh['closed'])
        self.assertEqual(fresh['events'][:-1],old_events)
        self.assertEqual(ma.request_spend(self.state,r['id']),old_spend)
        self.assertEqual([e['source']for e in ma._round_attempts(fresh)],['indexed_corpus'])
        mr.close(self.state,r['id'],status='exhausted',reason='still missing',now=NOW)
        self.assertIsNone(recovery.recover(self.state,mr.request(self.state,r['id']),now=NOW))
    def test_normal_sweep_recovers_without_waiting_seven_days(self):
        r,q=self.setup_query(closed=True)
        result=ma.retry_sweep(self.state,now=NOW)
        self.assertEqual(result,[{'request_id':r['id'],'reason':recovery.RULE_REF}])
    def test_actual_launch_failure_or_credentials_never_qualify(self):
        for status,kind in [('launched',None),('failed','transport'),('refused','credentials'),('deferred','budget')]:
            r,q=self.setup_query(closed=True)
            ma.record_targeted_event(self.state,q['query_id'],status=status,kind=kind,now=NOW)
            self.assertIsNone(recovery.candidate(self.state,r,now=NOW))
            # Repeated exact request fixture joins; reset only synthetic state.
            self.tmp.cleanup();self.setUp()
    def test_durable_ticket_without_targeted_event_refuses_recovery(self):
        r,q=self.setup_query(closed=True)
        directory=self.state/'discoveries'/'synthetic';directory.mkdir(parents=True)
        (directory/'ticket.json').write_text(json.dumps({'targeted_binding':{'query_ref':q['query_id']}}))
        self.assertIsNone(recovery.candidate(self.state,r,now=NOW))
    def test_cas_changed_close_or_competing_reopen_does_not_append(self):
        r,q=self.setup_query(closed=True);proof=recovery.candidate(self.state,r,now=NOW)
        self.assertIsNotNone(proof)
        mr.reopen(self.state,r['id'],reason='normal competing maintenance',now=NOW)
        with mock.patch.object(recovery,'candidate',return_value=proof):
            self.assertIsNone(recovery.recover(self.state,r,now=NOW))
        self.assertEqual(sum(e['event_kind']=='opened'for e in mr.request(self.state,r['id'])['events']),2)
    def test_wrong_exact_query_projection_and_invalid_marker_fail_closed(self):
        r,q=self.setup_query(closed=True)
        c=sqlite3.connect(mr.ledger_path(self.state));c.execute('DROP TRIGGER material_targeted_queries_no_update')
        c.execute('UPDATE material_targeted_queries SET company_ref=? WHERE query_id=?',('company:wrong',q['query_id']));c.commit();c.close()
        self.assertIsNone(recovery.candidate(self.state,r,now=NOW))
        with self.assertRaises(ValueError):
            recovery.carried_attempts([{'source_queue_recovery':{'rule_ref':recovery.RULE_REF}}],0)
    def test_old_other_connector_or_real_missing_configuration_cannot_recover(self):
        r,q=self.setup_query(closed=True)
        (self.state/'discovery-plans'/'us-it-services-sec-filings-v1.json').unlink()
        self.assertIsNone(recovery.candidate(self.state,r,now=NOW))
        r={**r,'status':'found'};self.assertIsNone(recovery.candidate(self.state,r,now=NOW))

    def test_normal_serve_requeues_only_sec_and_keeps_old_spend_budget_gate(self):
        r,q=self.setup_query(closed=True)
        old_spend=ma.request_spend(self.state,r['id'])
        (self.state/'material-acquisition-pool.json').write_text(json.dumps({'calls_per_day':0}))
        sec=ma.TargetedSourceRung('sec_edgar')
        with mock.patch.object(sec,'run',wraps=sec.run) as run:
            result=ma.serve(self.state,r['id'],now=NOW,rungs={'indexed_corpus':ma.IndexedCorpusRung(),'sec_edgar':sec})
        self.assertEqual(result['outcome'],'budget_blocked');run.assert_not_called()
        self.assertEqual(ma.request_spend(self.state,r['id']),old_spend)

    def test_wait_read_failure_is_not_absence_and_closed_failure_never_reopens(self):
        r,q=self.setup_query()
        with mock.patch.object(recovery,'_configured_sec',side_effect=sqlite3.OperationalError('database locked')):
            self.assertEqual(self.settle(r,q).outcome,'waiting')
        mr.record_attempt(self.state,r['id'],source='sec_edgar',outcome='not_connected',detail='real absent connector',now=NOW)
        mr.close(self.state,r['id'],status='exhausted',reason='real missing connection',now=NOW)
        self.assertIsNone(recovery.candidate(self.state,mr.request(self.state,r['id']),now=NOW))

    def test_ticket_scan_deadline_is_explicit_not_a_success_or_free_delivery(self):
        r,q=self.setup_query(closed=True)
        directory=self.state/'discoveries'/'synthetic';directory.mkdir(parents=True)
        (directory/'ticket.json').write_text(json.dumps({'targeted_binding':{}}))
        with mock.patch.object(recovery,'time',mock.Mock(monotonic=mock.Mock(side_effect=[0,2]))):
            result=recovery.qualification(self.state,r,now=NOW)
        self.assertEqual(result,{'status':'ineligible','reason':'durable_ticket_scan_deadline'})

    def test_another_company_quarterly_exact_source_uses_same_recovery_contract(self):
        company='company:sec-cik:0001058290';cik='0001058290'
        self.plan();plan=fixture.sec_plan();plan['companies'][company]={'cik':cik}
        plan['specs'].append({**plan['specs'][0],'spec_ref':'quarterly-report-10q','form':'10-Q'})
        from dalton_core.store import content_hash
        plan['content_hash']=content_hash({k:v for k,v in plan.items()if k!='content_hash'})
        (self.state/'discovery-plans'/'us-it-services-sec-filings-v1.json').write_text(json.dumps(plan))
        r=mr.open_request(self.state,requester={'lane':'statement_context','section':'exact_xml_units'},subject=company,
            need={'kind':'document','doc_kinds':['sec_10q']},period='2025-06-30',
            acceptance={'cik':cik,'accession':cik+'-25-000123','document_ref':'sec:filing:'+cik+'-25-000123',
                'date_from':'2025-08-04','date_to':'2025-08-04','min_passages':1},source_priority=['sec_edgar'],now=START)
        phrases=ma.build_queries(r,ma.identity_for(self.state,company),now=START)
        q=ma.enqueue_targeted_query(self.state,request_id=r['id'],source='sec_edgar',company_ref=company,
            spec_ref='quarterly-report-10q',phrasings=phrases,period=r['period'],now=START)
        mr.record_attempt(self.state,r['id'],source='sec_edgar',outcome='dispatched',query={'targeted_query':q['query_id']},now=START)
        mr.record_attempt(self.state,r['id'],source='sec_edgar',outcome='not_connected',detail=recovery.LEGACY_TIMEOUT_DETAIL,now=NOW)
        mr.close(self.state,r['id'],status='exhausted',reason='legacy queue mistake',now=NOW)
        self.assertIsNotNone(recovery.candidate(self.state,mr.request(self.state,r['id']),now=NOW))

    def test_forged_caller_attempts_do_not_authorize_recovery(self):
        r,q=self.setup_query(closed=True)
        fake={**r,'events':[dict(e)for e in r['events']]}
        fake['events'][-2]['detail']='caller changed diagnostic'
        self.assertIsNone(recovery.candidate(self.state,fake,now=NOW))

    def test_normal_serve_creates_new_sec_query_without_rebuying_prior_rungs_or_claiming_units(self):
        r,q=self.setup_query(closed=True);old_spend=ma.request_spend(self.state,r['id'])
        ma.record_targeted_event(self.state,q['query_id'],status='refused',kind='request_binding',
            reason='material request is no longer active',now=NOW)
        indexed=mock.Mock();indexed.applies.return_value=True
        result=ma.serve(self.state,r['id'],now=NOW,rungs={'indexed_corpus':indexed,'sec_edgar':ma.TargetedSourceRung('sec_edgar')})
        self.assertEqual(result['outcome'],'waiting');indexed.run.assert_not_called()
        fresh=mr.request(self.state,r['id']);attempts=ma._round_attempts(fresh)
        self.assertEqual([e['source']for e in attempts],['indexed_corpus','indexed_corpus','sec_edgar'])
        # The existing once-only local lexical recheck is free and preserved.
        self.assertEqual(attempts[1].get('cost_micros'),0)
        self.assertNotIn('calls',attempts[1])
        self.assertNotEqual(ma._targeted_query_id(attempts[-1]),q['query_id'])
        self.assertEqual(ma.request_spend(self.state,r['id']),{'calls':old_spend['calls']+1,'cost_micros':old_spend['cost_micros']})
        self.assertFalse(fresh['closed']);self.assertNotEqual(fresh['status'],'found')

    def test_unanswered_old_query_is_reused_after_normal_reopen_without_new_queue_charge(self):
        r,q=self.setup_query(closed=True);old_spend=ma.request_spend(self.state,r['id'])
        result=ma.serve(self.state,r['id'],now=NOW,rungs={'indexed_corpus':ma.IndexedCorpusRung(),'sec_edgar':ma.TargetedSourceRung('sec_edgar')})
        self.assertEqual(result['outcome'],'waiting')
        self.assertEqual(ma._targeted_query_id(ma._round_attempts(mr.request(self.state,r['id']))[-1]),q['query_id'])
        self.assertEqual(ma.request_spend(self.state,r['id']),old_spend)
