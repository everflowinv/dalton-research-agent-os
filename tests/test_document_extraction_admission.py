"""Mission-budgeted broker boundary and human staging; all model outputs synthetic."""
import copy
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dalton_core.document_extraction import DocumentExtractionService, build_work, HermeticExtractionAdapter
from dalton_core.openclaw_model_adapter import OpenClawModelAdapter, BrokerConnectionError
from dalton_core.research_verification import CandidateStagingStore, ResearchVerificationError, ResearchVerificationConflict
from dalton_core.store import content_hash
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore, ThesisImpactDayBudgetExceeded, ThesisImpactBudgetConflict
from dalton_core.writer_server import Principal, HUMAN_GOVERNANCE_OPERATIONS, WriterServer
from tests.test_document_extraction import ExtractionHarness, OWNER, WINDOW_CHARS
from tests.test_transcript_polish_model_worker import profile, policy


def staging_params(h, result=None):
    result = result or h.generate()
    s = result['suggestions'][0]; q = s['citation']
    return {**h.params, 'expected_context_hash':result['context']['content_hash'],
        'suggestion_ref':s['id'], 'suggestion_hash':s['content_hash'], 'request_id':'human-stage-test',
        'normalized_statement':'Management described cautious decisions, subject to uncertainty.',
        'metric_or_aspect':s['metric_or_aspect'],'period':s['period'],'basis':s['basis'],
        'source_start':q['source_start'],'source_end':q['source_end'],'raw_text':q['raw_text'],
        'rationale':'Human checked the original, attribution and uncertainty.', 'confirm_citation':True,
        'correction_set_version_ref':None,'correction_set_version_hash':None}


class HumanStagingTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
        self.h=ExtractionHarness(Path(t.name));self.addCleanup(self.h.close)
        self.staging=CandidateStagingStore(Path(t.name)/'staging.sqlite');self.addCleanup(self.staging.close)
        self.h.writer._candidate_staging=self.staging
        self.h.enable_fixture()

    def test_stage_edited_citation_and_replay_never_accepts_or_resolves(self):
        h=self.h; before=h.counts();p=staging_params(h)
        p['source_end']=100;p['raw_text']=p['raw_text'][:100]
        r=h.service.stage(**p)
        self.assertEqual(r['status'],'staged');self.assertFalse(r['claim_accepted'])
        replay=h.service.stage(**p);self.assertEqual(replay['write_status'],'duplicate')
        self.assertEqual(r['candidate_claim_ref'],replay['candidate_claim_ref'])
        self.assertEqual(h.counts()['claim_versions'],before['claim_versions'])
        self.assertEqual(h.counts()['evidence_versions'],before['evidence_versions'])
        self.assertEqual(h.counts()['connector_invocations'],before['connector_invocations'])
        self.assertEqual(h.missions.document_review(h.review['review_id'])['state'],'awaiting_human_extraction')
        request=json.loads(h.h.core.connection.execute('SELECT request_json FROM coverage_mission_document_staging_requests').fetchone()[0])
        self.assertEqual(request['actor_ref'],OWNER)
        with self.assertRaises(ResearchVerificationConflict):h.service.stage(**{**p,'rationale':'changed'})

    def test_missing_confirm_stale_foreign_numeric_or_modified_original_write_nothing(self):
        h=self.h;p=staging_params(h);before=h.counts()
        for changes in ({'confirm_citation':False},{'confirm_citation':1},{'rationale':''},
                        {'suggestion_hash':'0'*64},{'raw_text':'forged'}, {'source_start':True},
                        {'source_end':999999},{'expected_context_hash':'0'*64},
                        {'normalized_statement':'Revenue increased 30%'}, {'actor_ref':'automation:coverage-mission'}):
            with self.subTest(changes=changes),self.assertRaises(ResearchVerificationError):h.service.stage(**{**p,**changes})
        self.assertEqual(h.counts(),before)

    def test_staging_crash_replays_same_correction_and_citation(self):
        h=self.h;p=staging_params(h)
        with patch.object(self.staging,'stage',side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):h.service.stage(**p)
        partial=h.counts();result=h.service.stage(**p)
        self.assertEqual(result['status'],'staged');self.assertEqual(h.counts(),partial)

    def test_writer_auth_closed_shape_and_unconfigured_staging(self):
        h=self.h;p=staging_params(h)
        bot=Principal('bot','fixture',HUMAN_GOVERNANCE_OPERATIONS,actor_ref='automation:coverage-mission')
        with self.assertRaises(PermissionError):h.writer._authorized_params(bot,'stage_document_extraction',p)
        human=h.writer.principals['human']
        with self.assertRaises(PermissionError):h.writer._authorized_params(human,'stage_document_extraction',{**p,'actor_ref':'human:forged'})
        h.writer._candidate_staging=None
        before=h.counts()
        with self.assertRaises(Exception):h.service.stage(**p)
        self.assertEqual(h.counts(),before)

    def test_raw_review_cannot_bind_outside_explicitly_reviewed_span(self):
        h=self.h;p=staging_params(h);p['source_end']=100;p['raw_text']=p['raw_text'][:100]
        result=h.service.stage(**p)
        authority,_=h.writer._transcript_corrections(h.manifest)
        citation=authority.claim_citation_binding(result['citation_ref'])
        with self.assertRaises(Exception):
            authority.bind_claim_citation(citation['correction_set_version_ref'],citation['correction_set_version_hash'],source_start=0,source_end=101)
        with self.assertRaises(Exception):
            authority.publish('invalid-empty',source_manifest_ref=h.manifest['id'],source_manifest_hash=h.manifest['content_hash'],
                source_content_hash=h.manifest['declared_content_sha256'],review_scope='targeted_flags',corrections=[],actor_ref=OWNER)

    def test_existing_unresolved_correction_cannot_be_bypassed_by_raw_review(self):
        import hashlib
        h=self.h;p=staging_params(h);authority,_=h.writer._transcript_corrections(h.manifest)
        flag={'source_start':0,'source_end':10,'source_sha256':hashlib.sha256(p['raw_text'][:10].encode()).hexdigest(),
            'correction_kind':'semantic','disposition':'unresolved',
            'replacement_text':None,'rationale':'Unresolved original wording.','evidence_bindings':[]}
        correction=authority.publish('existing-unresolved',source_manifest_ref=h.manifest['id'],source_manifest_hash=h.manifest['content_hash'],
            source_content_hash=h.manifest['declared_content_sha256'],review_scope='targeted_flags',corrections=[flag],actor_ref=OWNER)
        before=h.counts()
        for changes in ({},{'correction_set_version_ref':correction['id'],'correction_set_version_hash':correction['content_hash']}):
            with self.assertRaises(ResearchVerificationError):h.service.stage(**{**p,**changes})
        self.assertEqual(h.counts(),before)



class MissionBudgetTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.path=Path(t.name)/'budget.sqlite'
        self.b=ThesisImpactBudgetStore(self.path);self.addCleanup(self.b.close)
        self.b.register_policy(policy_version_id='budget:owner:1',day_cap_micros=1000000)
        self.scope={'mission_ref':'mission:test','mission_version_ref':'mission:test:1','mission_version_hash':'1'*64,
                    'max_daily_paid_calls':1,'max_daily_cost_micros':100000}

    def admit(self,work,**kw):
        return self.b.admit(policy_version_id='budget:owner:1',day='2026-09-06',work_order_ref=work,
            attempt_number=1,phase='assessment',route_decision_ref='route:'+work,reserved_micros=50000,
            mission_binding=kw.get('scope',self.scope))

    def test_mission_count_is_atomic_durable_and_not_reset_by_mission_version(self):
        first=self.admit('work:a');self.b.settle(first['admission_id'],actual_micros=1000)
        self.assertEqual(self.admit('work:a')['status'],'duplicate')
        with self.assertRaises(ThesisImpactDayBudgetExceeded):self.admit('work:b',scope={**self.scope,'mission_version_ref':'mission:test:2'})
        with self.assertRaises(ThesisImpactBudgetConflict):self.admit('work:b',scope={**self.scope,'max_daily_paid_calls':100})
        with self.assertRaises(ThesisImpactBudgetConflict):self.admit('work:a',scope={**self.scope,'max_daily_paid_calls':100})

    def test_parallel_budget_connections_allow_only_one_reservation(self):
        barrier=threading.Barrier(2);out=[]
        def reserve(n):
            with ThesisImpactBudgetStore(self.path) as b:
                barrier.wait()
                try:b.admit(policy_version_id='budget:owner:1',day='2026-09-06',work_order_ref='work:'+str(n),
                    attempt_number=1,phase='assessment',route_decision_ref='route:'+str(n),reserved_micros=50000,mission_binding=self.scope)
                except ThesisImpactDayBudgetExceeded:out.append('rejected')
                else:out.append('admitted')
        threads=[threading.Thread(target=reserve,args=(n,)) for n in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(10)
        self.assertEqual(sorted(out),['admitted','rejected'])

    def test_unsettled_reservation_survives_day_rollover(self):
        self.admit('work:a')
        with self.assertRaises(ThesisImpactDayBudgetExceeded):
            self.b.admit(policy_version_id='budget:owner:1',day='2026-09-07',work_order_ref='work:nextday',attempt_number=1,
                phase='assessment',route_decision_ref='route:nextday',reserved_micros=50000,mission_binding=self.scope)

    def test_zero_mission_budget_or_calls_reject_before_reservation(self):
        for i,scope in enumerate(({**self.scope,'max_daily_paid_calls':0},{**self.scope,'max_daily_cost_micros':0})):
            with self.assertRaises(ThesisImpactDayBudgetExceeded):self.admit('zero:'+str(i),scope=scope)
        self.assertEqual(self.b.connection.execute('SELECT count(*) FROM thesis_impact_day_admissions').fetchone()[0],0)

    def test_unscoped_shared_spend_is_conservatively_counted_against_mission(self):
        self.b.admit(policy_version_id='budget:owner:1',day='2026-09-06',work_order_ref='other:lane',attempt_number=1,
            phase='verification',route_decision_ref='other:route',reserved_micros=50000)
        with self.assertRaises(ThesisImpactDayBudgetExceeded):self.admit('new:mission')


    def outer(self, **changes):
        return {"mandate_ref":"mandate:test","mandate_version_ref":"mandate:test:1","mandate_version_hash":"1"*64,
            "governance_policy_ref":"governance:test","governance_policy_version_ref":"governance:test:1",
            "governance_policy_version_hash":"2"*64,"max_daily_paid_calls":1,"max_daily_cost_micros":100000,**changes}

    def test_different_missions_share_outer_call_cap_even_after_settlement(self):
        a=self.admit('outer:a',scope={**self.scope,'outer_budget':self.outer()})
        self.b.settle(a['admission_id'],actual_micros=1000)
        with self.assertRaises(ThesisImpactDayBudgetExceeded) as caught:
            self.admit('outer:b',scope={**self.scope,'mission_ref':'mission:other','outer_budget':self.outer()})
        self.assertEqual(caught.exception.rejection['reason'],'outer_research_budget_exceeded')

    def test_parallel_different_missions_share_outer_reservation_atomically(self):
        barrier=threading.Barrier(2);results=[]
        def reserve(n):
            with ThesisImpactBudgetStore(self.path) as b:
                barrier.wait()
                try:
                    b.admit(policy_version_id='budget:owner:1',day='2026-09-06',work_order_ref='outer:'+str(n),attempt_number=1,
                        phase='assessment',route_decision_ref='route:'+str(n),reserved_micros=50000,
                        mission_binding={**self.scope,'mission_ref':'mission:'+str(n),'outer_budget':self.outer()})
                except ThesisImpactDayBudgetExceeded:results.append('rejected')
                else:results.append('admitted')
        threads=[threading.Thread(target=reserve,args=(n,)) for n in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(10)
        self.assertEqual(sorted(results),['admitted','rejected'])

    def test_unknown_prior_day_call_still_consumes_outer_budget_for_another_mission(self):
        self.admit('outer:unknown',scope={**self.scope,'outer_budget':self.outer()})
        with self.assertRaises(ThesisImpactDayBudgetExceeded):
            self.b.admit(policy_version_id='budget:owner:1',day='2026-09-07',work_order_ref='outer:tomorrow',attempt_number=1,
                phase='assessment',route_decision_ref='outer:tomorrow',reserved_micros=50000,
                mission_binding={**self.scope,'mission_ref':'mission:other','outer_budget':self.outer()})

    def test_outer_cost_cap_cannot_be_reset_by_other_mission_or_policy_version(self):
        outer=self.outer(max_daily_paid_calls=10)
        a=self.admit('outer:cost:a',scope={**self.scope,'outer_budget':outer})
        self.b.settle(a['admission_id'],actual_micros=50000)
        self.admit('outer:cost:b',scope={**self.scope,'mission_ref':'mission:second','outer_budget':outer})
        with self.assertRaises(ThesisImpactDayBudgetExceeded):
            self.admit('outer:cost:c',scope={**self.scope,'mission_ref':'mission:third',
                'outer_budget':{**outer,'governance_policy_version_ref':'governance:test:2'}})



class BrokerAdmissionTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
        self.h=ExtractionHarness(Path(t.name), paid_budget_authorized=True);self.addCleanup(self.h.close)
        h=self.h
        # Register synthetic route in a separate local router, never touch installed model permissions.
        from dalton_core.model_router import ModelRouter
        self.router=ModelRouter(h.root/'paid-router.sqlite');self.addCleanup(self.router.close)
        pr=profile();pr['availability']['valid_until']='2099-01-01T00:00:00+00:00'
        self.pr=pr;self.router.register_profile(pr);self.router.register_policy(policy())
        self.b=ThesisImpactBudgetStore(h.root/'budget.sqlite');self.addCleanup(self.b.close)
        self.b.register_policy(policy_version_id='budget:owner:1',day_cap_micros=1000000)
        config={'routing_policy_ref':policy()['policy_version_ref'],'credential_slot_refs':[pr['credential_slot_ref']],
                'model_router_db':str(h.root/'paid-router.sqlite'),'broker_socket':str(h.root/'never-connect.sock'),
                'broker_auth_key':str(h.root/'no-key'),'broker_client_id':'client:dalton-core','expected_agent_id':'chem',
                'budget_db':str(h.root/'budget.sqlite'),'budget_policy_ref':'budget:owner:1'}
        # Validate via real constructor, then use the existing harness's writer.
        checked=WriterServer(h.root/'not-opened',h.root/'no-socket',h.writer.principals,document_extraction_model_config=config)
        h.writer._document_extraction_model_config=checked._document_extraction_model_config
        self.calls=0;self.outputs={};self.actual_cost=0.001;self.invalid=False

    def execute(self,adapter,work,route,pr):
        self.calls+=1
        self.assertEqual(self.b.connection.execute('SELECT count(*) FROM thesis_impact_day_admissions').fetchone()[0],1)
        context=work.metadata['context']
        wire={'schema_version':'0.1','suggestions':[{'quote_id':context['quotes'][0]['quote_id'],
            'normalized_statement':'Management described cautious decisions.','metric_or_aspect':'aspect:decisions',
            'period':'current commentary','basis':'management'}]}
        if self.invalid:wire['suggestions'][0]['actor_ref']='human:forged'
        fixture=HermeticExtractionAdapter(wire,created_at=self.h.h.clock().isoformat())
        fp=copy.deepcopy(pr);fp['provider']='hermetic-fixture';fp['cost']['input_per_million_usd']=fp['cost']['output_per_million_usd']=0
        inv,res=fixture.execute(work,route,fp)
        inv=replace(inv,provider=pr['provider'],actor_ref='runtime:openclaw-model-broker',
            usage={**inv.usage,'raw_provider_telemetry':{'cost':{'available':True,'usd':self.actual_cost}}})
        res=replace(res,metadata={k:v for k,v in res.metadata.items() if k!='hermetic_fixture'})
        self.outputs[work.id]=(inv,res)
        return inv,res

    def patch_execute(self):
        return patch.object(OpenClawModelAdapter,'execute',autospec=True,side_effect=self.execute)

    def test_real_adapter_socket_protocol_with_synthetic_provider_and_budget(self):
        from tests.test_openclaw_model_adapter import FakeBroker, success_response, seal, AUTH_SECRET
        socket_temp=tempfile.TemporaryDirectory(prefix='d-ext-');self.addCleanup(socket_temp.cleanup)
        h=self.h
        # Known synthetic fixture key; no installed credential is read.
        key=h.root/'no-key';key.write_bytes(AUTH_SECRET);key.chmod(0o600)
        # Responder thread must not access the owning thread's SQLite objects.
        context=h.context()
        def thread_respond(request):
            import sqlite3
            with sqlite3.connect(h.root/'budget.sqlite') as con:
                if con.execute('SELECT count(*) FROM thesis_impact_day_admissions').fetchone()[0]!=1:raise AssertionError('no reservation')
            output={'schema_version':'0.1','suggestions':[{'quote_id':context['quotes'][0]['quote_id'],
                'normalized_statement':'Management described cautious client decisions.',
                'metric_or_aspect':'aspect:decisions','period':'current commentary','basis':'management'}]}
            wire=success_response(request,text=json.dumps(output),cost={'available':True,'usd':0.001})
            wire.pop('contentHash');wire.update(provider='test',model='transcript',canonicalModel='test/transcript',agentId='chem')
            return seal(wire)
        broker=FakeBroker(Path(socket_temp.name),thread_respond);self.addCleanup(broker.close)
        h.writer._document_extraction_model_config['broker_socket']=str(broker.path)
        result=h.generate();self.assertEqual(result['status'],'succeeded',result.get('error_code'))
        self.assertEqual(result['model_budget']['actual_micros'],1000)
        self.assertEqual(h.generate()['suggestions'],result['suggestions'])
        self.assertEqual(len(broker.requests),1)
        self.assertIn('auth',broker.requests[0]);self.assertEqual(broker.requests[0]['model'],'test/transcript')

    def test_broker_composition_reserves_then_accounts_once_and_replays(self):
        with self.patch_execute():
            result=self.h.generate();self.assertEqual(result['status'],'succeeded',result)
            self.assertFalse(result['hermetic_fixture']);self.assertEqual(self.h.generate()['suggestions'],result['suggestions'])
        self.assertEqual(self.calls,1)
        row=self.b.connection.execute('SELECT actual_micros,usage_entry_ref FROM thesis_impact_day_settlements').fetchone()
        self.assertEqual(row['actual_micros'],1000);self.assertTrue(row['usage_entry_ref'])
        self.assertEqual(self.b.connection.execute('SELECT count(*) FROM model_mission_budget_bindings').fetchone()[0],1)

    def test_invalid_provider_output_accounts_failure_and_never_retries(self):
        self.invalid=True
        with self.patch_execute():
            result=self.h.generate();self.assertEqual(result['status'],'failed',result)
            self.assertEqual(self.h.generate()['status'],'failed')
        self.assertEqual(self.calls,1)
        self.assertEqual(self.b.connection.execute('SELECT actual_micros FROM thesis_impact_day_settlements').fetchone()[0],1000)

    def test_disconnect_keeps_full_reservation_and_no_automatic_paid_retry(self):
        with patch.object(OpenClawModelAdapter,'execute',side_effect=BrokerConnectionError('synthetic disconnect')) as call:
            result=self.h.generate();self.assertEqual(result['status'],'failed',result)
            self.h.generate();self.assertEqual(call.call_count,1)
        self.assertEqual(self.b.connection.execute('SELECT reserved_micros FROM thesis_impact_day_admissions').fetchone()[0],50000)
        self.assertEqual(self.b.connection.execute('SELECT count(*) FROM thesis_impact_day_settlements').fetchone()[0],0)

    def test_owner_budget_exhausted_blocks_before_adapter(self):
        self.b.admit(policy_version_id='budget:owner:1',day=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).date().isoformat(),
            work_order_ref='other:work',attempt_number=1,phase='verification',route_decision_ref='other:route',reserved_micros=1000000)
        with self.patch_execute():self.assertEqual(self.h.generate()['status'],'failed')
        self.assertEqual(self.calls,0)
        self.assertEqual(self.b.connection.execute('SELECT count(*) FROM thesis_impact_day_rejections').fetchone()[0],1)

    def test_provider_cost_overrun_is_accounted_terminal_and_blocks_further_calls(self):
        self.actual_cost=0.1
        with self.patch_execute():
            result=self.h.generate()
            self.assertEqual(result['status'],'failed')
            self.assertEqual(result['error_code'],'MODEL_COST_EXCEEDED_RESERVATION')
        self.assertEqual(self.calls,1)
        self.assertEqual(self.b.connection.execute('SELECT count(*) FROM thesis_impact_day_settlements').fetchone()[0],0)
        self.assertEqual(self.b.connection.execute('SELECT count(*) FROM thesis_impact_alerts').fetchone()[0],1)
        with self.assertRaises(ThesisImpactBudgetConflict):
            self.b.admit(policy_version_id='budget:owner:1',day='2026-09-06',work_order_ref='new:work',attempt_number=1,
                phase='assessment',route_decision_ref='new:route',reserved_micros=50000)

    def test_outer_authority_missing_or_smaller_than_mission_blocks_before_any_call(self):
        cap={"max_daily_paid_calls":40,"max_daily_cost_usd":5.0,"max_alphaengine_calls_24h":30}
        cases=[({'mandate':None},'mandate lacks'),({'governance':None},'governance lacks'),
               ({'mandate':{**cap,'max_daily_cost_usd':0.01}},'mission exceeds mandate'),
               ({'governance':{**cap,'max_daily_paid_calls':0}},'mission exceeds governance'),
               ({'governance':{**cap,'max_alphaengine_calls_24h':0}},'mission exceeds governance')]
        for n,(overrides,reason) in enumerate(cases):
            (self.h.root/('outer-case-'+str(n))).mkdir()
            h=ExtractionHarness(self.h.root/('outer-case-'+str(n)),paid_budget_authorized=True,paid_budget_overrides=overrides)
            try:
                h.writer._document_extraction_model_config=self.h.writer._document_extraction_model_config
                with self.subTest(overrides=overrides),self.assertRaisesRegex(ResearchVerificationError,reason):h.generate()
            finally:h.close()
        self.assertEqual(self.b.connection.execute('SELECT count(*) FROM thesis_impact_day_admissions').fetchone()[0],0)

    def test_governance_pointer_change_invalidates_frozen_extraction_context(self):
        h=self.h;context=h.context()
        h.h.core.create_policy({**h.h.core.active_policy_version().policy},policy_version_id='policy:synthetic:3',
            actor_ref=OWNER,change_reason='Synthetic governance version rollover')
        with self.patch_execute(),self.assertRaises(ResearchVerificationConflict):
            h.service.generate(**h.params,expected_context_hash=context['content_hash'])
        self.assertEqual(self.calls,0)

    def test_missing_config_authority_fails_without_creating_new_budget(self):
        path=self.h.root/'missing-budget.sqlite';self.h.writer._document_extraction_model_config['budget_db']=str(path)
        with self.assertRaises(ResearchVerificationError):self.h.context()
        self.assertFalse(path.exists())


if __name__=='__main__':unittest.main()
