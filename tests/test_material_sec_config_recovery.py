"""Local configuration refusals may reenter once; sent failures may not."""
import json
import sqlite3
import unittest
from unittest import mock
from dalton_core import material_acquisition as ma,material_requests as mr
from dalton_core import material_sec_config_recovery as recovery
from dalton_core import material_source_queue_recovery as queue
from tests import test_sec_filings_index_discovery as fixture
from tests import test_material_targeted_sec as targeted
START='2026-09-06T00:00:00+00:00'
NOW='2026-09-08T00:00:00+00:00'

class LocalConfigRecoveryTests(unittest.TestCase):
    setUp=fixture.SecFilingsIndexDiscoveryTests.setUp
    queued=targeted.TargetedSecTests.queued
    def setup_query(self,closed=True):
        folder=self.state/'discovery-plans';folder.mkdir(exist_ok=True)
        self.plan_path=folder/'us-it-services-sec-filings-v1.json'
        self.plan_path.write_text(json.dumps(fixture.sec_plan()))
        r,q=self.queued()
        mr.record_attempt(self.state,r['id'],source='indexed_corpus',outcome='empty',calls=1,cost_micros=3,now=START)
        ma.ensure_schema(self.state);ma._record_spend(self.state,r['id'],'indexed_corpus',1,3,now=START)
        mr.record_attempt(self.state,r['id'],source='sec_edgar',outcome='dispatched',query={'targeted_query':q['query_id']},now=START)
        ma.record_targeted_event(self.state,q['query_id'],status='refused',kind='no_spec',reason='plan has no spec annual-report-10k',now=START)
        if closed:
            mr.record_attempt(self.state,r['id'],source='sec_edgar',outcome='not_connected',detail='plan has no spec annual-report-10k',now=START)
            mr.close(self.state,r['id'],status='exhausted',reason='old local configuration refused',now=START)
        return mr.request(self.state,r['id']),q
    def test_closed_exact_local_refusal_requeues_only_sec_retains_history_spend_and_no_units_claim(self):
        r,q=self.setup_query();old=r['events'];spend=ma.request_spend(self.state,r['id'])
        proof=queue.recover(self.state,r,now=NOW);self.assertEqual(proof['rule_ref'],recovery.RULE_REF)
        fresh=mr.request(self.state,r['id']);self.assertEqual(fresh['events'][:-1],old)
        self.assertEqual(ma.request_spend(self.state,r['id']),spend)
        self.assertEqual([e['source']for e in ma._round_attempts(fresh)],['indexed_corpus'])
        indexed=mock.Mock();indexed.applies.return_value=True
        out=ma.serve(self.state,r['id'],now=NOW,rungs={'indexed_corpus':indexed,'sec_edgar':ma.TargetedSourceRung('sec_edgar')})
        indexed.run.assert_not_called();self.assertEqual(out['outcome'],'waiting')
        q2=ma._targeted_query_id(ma._round_attempts(mr.request(self.state,r['id']))[-1]);self.assertNotEqual(q2,q['query_id'])
        self.assertIn(q2,{x['query_id']for x in ma.pending_targeted_queries(self.state,'source:sec-edgar')})
        self.assertEqual(ma.targeted_query_status(self.state,q['query_id'])['kind'],'no_spec')
        self.assertFalse(mr.request(self.state,r['id'])['closed'])
    def test_open_unexpired_refusal_reenters_without_extending_deadline(self):
        r,q=self.setup_query(closed=False)
        before=r['deadline'];proof=recovery.recover(self.state,r,now=START)
        self.assertIsNotNone(proof);fresh=mr.request(self.state,r['id'])
        self.assertEqual(fresh['deadline'],before);self.assertIsNone(recovery.recover(self.state,fresh,now=START))
        self.assertEqual([e['source']for e in ma._round_attempts(fresh)],['indexed_corpus'])
    def test_same_epoch_refusal_cannot_repeat_but_changed_executable_spec_can(self):
        r,q=self.setup_query();self.assertIsNotNone(recovery.recover(self.state,r,now=NOW))
        fresh=mr.request(self.state,r['id'])
        q2=ma.enqueue_targeted_query(self.state,request_id=r['id'],source='sec_edgar',company_ref=r['subject'],
            spec_ref=q['spec_ref'],phrasings=q['phrasings'],period=r['period'],now=NOW)
        mr.record_attempt(self.state,r['id'],source='sec_edgar',outcome='dispatched',query={'targeted_query':q2['query_id']},now=NOW)
        ma.record_targeted_event(self.state,q2['query_id'],status='refused',kind='no_spec',reason='plan has no spec annual-report-10k',now=NOW)
        mr.record_attempt(self.state,r['id'],source='sec_edgar',outcome='not_connected',detail='plan has no spec annual-report-10k',now=NOW)
        mr.close(self.state,r['id'],status='exhausted',reason='local refused again',now=NOW)
        fresh=mr.request(self.state,r['id']);self.assertIsNone(recovery.recover(self.state,fresh,now=NOW))
        from dalton_core.store import content_hash
        plan=json.loads(self.plan_path.read_text());plan['specs'][0]['lookback_days']=501
        plan['content_hash']=content_hash({k:v for k,v in plan.items()if k!='content_hash'});self.plan_path.write_text(json.dumps(plan))
        self.assertIsNotNone(recovery.recover(self.state,fresh,now=NOW))
    def test_real_http_unknown_sent_and_nonlocal_refusals_never_recover(self):
        for status,kind,extra in [('failed','transport',{}),('launched',None,{'ticket_ref':'sec-filings-index:abc'}),
                ('refused','credentials',{}),('deferred','budget',{}),('refused','no_spec',{'dispatch_ref':'dispatch:unknown'})]:
            with self.subTest(status=status,kind=kind):
                r,q=self.setup_query();ma.record_targeted_event(self.state,q['query_id'],status=status,kind=kind,**extra,now=NOW)
                self.assertNotEqual(recovery.qualification(self.state,r,now=NOW)['status'],'eligible')
            self.tmp.cleanup();self.setUp()
    def test_wrong_query_projection_and_forged_request_cannot_recover(self):
        r,q=self.setup_query();fake={**r,'subject':'company:other'}
        self.assertIsNone(recovery.recover(self.state,fake,now=NOW))
        c=sqlite3.connect(mr.ledger_path(self.state));c.execute('DROP TRIGGER material_targeted_queries_no_update')
        c.execute('UPDATE material_targeted_queries SET company_ref=? WHERE query_id=?',('company:other',q['query_id']));c.commit();c.close()
        self.assertNotEqual(recovery.qualification(self.state,r,now=NOW)['status'],'eligible')
    def test_unknown_event_shape_durable_ticket_or_physical_child_refuses(self):
        r,q=self.setup_query();d=self.state/'discoveries'/'synthetic';d.mkdir(parents=True)
        (d/'ticket.json').write_text(json.dumps({'targeted_binding':{'query_ref':q['query_id']},'status':'succeeded'}))
        self.assertEqual(recovery.qualification(self.state,r,now=NOW)['reason'],'durable_targeted_ticket_exists')
        (d/'ticket.json').unlink()
        with mock.patch.object(recovery.subprocess,'run',return_value=mock.Mock(stdout='python --targeted-binding '+q['query_id']))as process_read:
            self.assertEqual(recovery.qualification(self.state,r,now=NOW)['reason'],'physical_targeted_child_exists')
            self.assertEqual(process_read.call_args.args[0],['ps','-ww','-axo','command='])
    def test_configuration_or_mission_change_between_qualification_and_cas_does_not_append(self):
        r,q=self.setup_query();old=r['events'];proof=recovery.qualification(self.state,r,now=NOW)['proof']
        with mock.patch.object(recovery,'qualification',return_value={'status':'eligible','proof':proof}),mock.patch.object(recovery,'_current',return_value={**proof['configured'],'mission_version_hash':'0'*64}):
            self.assertIsNone(recovery.recover(self.state,r,now=NOW))
        self.assertEqual(mr.request(self.state,r['id'])['events'],old)
    def test_superseded_completed_expired_and_resolved_unit_need_do_not_reopen(self):
        r,q=self.setup_query(closed=False)
        self.assertIsNone(recovery.recover(self.state,r,now=NOW)) # open deadline elapsed
        mr.close(self.state,r['id'],status='found',reason='already supplied',now=START)
        self.assertIsNone(recovery.recover(self.state,mr.request(self.state,r['id']),now=NOW))
        self.tmp.cleanup();self.setUp();r,q=self.setup_query()
        with mock.patch('dalton_core.material_model_unit_priority.unit_input_proof',return_value=None):
            # Strong current-need helper also refuses a unit marker added only
            # by the caller: canonical request comparison is unchanged.
            fake={**r,'acceptance':{**r['acceptance'],'unit_proof_rule_ref':'sec-filing-units:0.1'}}
            self.assertIsNone(recovery.recover(self.state,fake,now=NOW))
    def test_ordinary_material_budget_still_blocks_new_source_call(self):
        r,q=self.setup_query();spend=ma.request_spend(self.state,r['id'])
        (self.state/'material-acquisition-pool.json').write_text(json.dumps({'calls_per_day':0}))
        sec=ma.TargetedSourceRung('sec_edgar')
        with mock.patch.object(sec,'run',wraps=sec.run)as run:
            out=ma.serve(self.state,r['id'],now=NOW,rungs={'indexed_corpus':ma.IndexedCorpusRung(),'sec_edgar':sec})
        self.assertEqual(out['outcome'],'budget_blocked');run.assert_not_called()
        self.assertEqual(ma.request_spend(self.state,r['id']),spend)
    def test_closed_no_spec_gets_fair_queue_sweep_opportunity(self):
        r,q=self.setup_query();out=ma.retry_sweep(self.state,now=NOW)
        self.assertEqual(out,[{'request_id':r['id'],'reason':recovery.RULE_REF}])

    def test_restart_same_effective_parameters_and_irrelevant_plan_change_are_same_epoch(self):
        r,q=self.setup_query();a=recovery.qualification(self.state,r,now=NOW)['proof']
        from dalton_core.store import content_hash
        plan=json.loads(self.plan_path.read_text());plan['id']='discovery-plan:renamed-but-same-spec'
        plan['content_hash']=content_hash({k:v for k,v in plan.items()if k!='content_hash'});self.plan_path.write_text(json.dumps(plan))
        b=recovery.qualification(self.state,r,now=NOW)['proof']
        self.assertEqual(a['recovery_key'],b['recovery_key'])
        self.assertNotEqual(a['configured']['effective_plan_hash'],b['configured']['effective_plan_hash'])
        self.assertIsNotNone(recovery.recover(self.state,r,now=NOW))
        self.assertIsNone(recovery.recover(self.state,mr.request(self.state,r['id']),now=NOW))
    def test_ticket_scan_deadline_and_configuration_read_error_are_explicit(self):
        r,q=self.setup_query();d=self.state/'discoveries'/'synthetic';d.mkdir(parents=True)
        (d/'ticket.json').write_text('{}')
        with mock.patch.object(recovery,'time',mock.Mock(monotonic=mock.Mock(side_effect=[0,2]))):
            self.assertEqual(recovery.qualification(self.state,r,now=NOW)['reason'],'durable_ticket_scan_deadline')
        with mock.patch.object(recovery,'_current',side_effect=sqlite3.OperationalError('locked')):
            self.assertEqual(recovery.qualification(self.state,r,now=NOW)['status'],'unverified')

    def test_current_grant_revocation_rejects_without_editing_old_no_send_history(self):
        r,q=self.setup_query();old=r['events']
        from dalton_core.coverage_mission import CoverageMissionAuthority
        from dalton_core.store import DaltonStore
        with DaltonStore(str(self.state/'core.sqlite'))as store:
            fields={k:self.mission[k]for k in ('title','objective','industry_ref','universe','research_questions','deliverables','source_plan','bindings','autonomy','budget')}
            fields['autonomy']={**fields['autonomy'],'may_write':[x for x in fields['autonomy']['may_write']if x!='source_discovery']}
            CoverageMissionAuthority(store).create_mission(self.mission['mission_ref'],**fields,
                actor_ref=fixture.OWNER,version_id='coverage-mission-version:revoked',prior_version_ref=self.mission['id'],idempotency_key='fixture-revoke-source')
        self.assertNotEqual(recovery.qualification(self.state,r,now=NOW)['status'],'eligible')
        self.assertEqual(mr.request(self.state,r['id'])['events'],old)
    def test_different_company_quarterly_request_uses_same_exact_local_no_send_contract(self):
        from dalton_core.store import content_hash
        folder=self.state/'discovery-plans';folder.mkdir(exist_ok=True);plan=fixture.sec_plan()
        company='company:sec-cik:0001058290';cik='0001058290';plan['companies'][company]={'cik':cik}
        plan['specs'].append({**plan['specs'][0],'spec_ref':'quarterly-report-10q','form':'10-Q','lookback_days':1095})
        plan['content_hash']=content_hash({k:v for k,v in plan.items()if k!='content_hash'})
        (folder/'us-it-services-sec-filings-v1.json').write_text(json.dumps(plan))
        r=mr.open_request(self.state,requester={'lane':'research_planner'},subject=company,
            need={'kind':'document','doc_kinds':['sec_10q']},period='2025-06-30',
            acceptance={'cik':cik,'accession':cik+'-25-000123','document_ref':'sec:filing:'+cik+'-25-000123',
                'date_from':'2025-08-04','date_to':'2025-08-04','min_passages':1},source_priority=['sec_edgar'],now=START)
        q=ma.enqueue_targeted_query(self.state,request_id=r['id'],source='sec_edgar',company_ref=company,
            spec_ref='quarterly-report-10q',phrasings=ma.build_queries(r,ma.identity_for(self.state,company),now=START),period=r['period'],now=START)
        mr.record_attempt(self.state,r['id'],source='sec_edgar',outcome='dispatched',query={'targeted_query':q['query_id']},now=START)
        ma.record_targeted_event(self.state,q['query_id'],status='refused',kind='no_spec',reason='plan has no spec quarterly-report-10q',now=START)
        mr.record_attempt(self.state,r['id'],source='sec_edgar',outcome='not_connected',detail='plan has no spec quarterly-report-10q',now=START)
        mr.close(self.state,r['id'],status='exhausted',reason='old no spec',now=START)
        self.assertIsNotNone(recovery.recover(self.state,mr.request(self.state,r['id']),now=NOW))

class CurrentUnitNeedTests(unittest.TestCase):
    def setUp(self):
        from tests import test_material_model_unit_priority as units
        self.fx=units.ModelUnitPriorityTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
    def test_real_current_unresolved_descriptor_and_changed_pointer_boundaries(self):
        from dalton_core.material_model_unit_priority import unit_input_proof
        from dalton_core.store import canonical_json
        from dalton_core.statement_context import StatementContextAuthority
        fx=self.fx;c=fx.store.connection
        mission=json.loads(c.execute('SELECT record_json FROM coverage_mission_versions ORDER BY version_number DESC LIMIT 1').fetchone()[0])
        request=mr.request(fx.state,fx.request['id'])
        p=unit_input_proof(c,request,mission,contract_ref=recovery.RULE_REF,cache={});self.assertIsNotNone(p)
        foreign={**mission,'universe':[]}
        self.assertIsNone(unit_input_proof(c,request,foreign,contract_ref=recovery.RULE_REF,cache={}))
        with StatementContextAuthority(fx.store)._transaction()as cursor:
            cursor.execute('INSERT INTO statement_context_versions VALUES(?,?,?,?,?,?)',
                ('context:new',fx.filing['ingest_id'],'available',canonical_json({'status':'available','lines':[],'unresolved_unit_refs':[]}),'f'*64,'2100-01-01'))
        # Latest pointer changes/fulfilled context cannot authorize old need.
        self.assertIsNone(unit_input_proof(c,request,mission,contract_ref=recovery.RULE_REF,cache={}))
        # Matching a malformed current context still fails strong verification.
        from dalton_core.statement_context import context_record
        with self.assertRaisesRegex(ValueError, 'hash drifted'):
            context_record(c, 'context:new')
