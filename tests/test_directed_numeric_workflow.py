import hashlib,json,unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.document_research import DocumentResearchRegistry,CoreAcquiredDocumentSourceAdapter,PublicWebDocumentSourceAdapter,build_document_research_policy
from dalton_core.mission_document_research import MissionDocumentResearchAuthority,PURPOSE
from dalton_core.mission_document_research_admission import admit_directed_inquiry
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.directed_numeric_research import CONTRACT_REF,normalize_targets
from dalton_core.directed_numeric_output import candidate_ref,quotes
from dalton_core.store import canonical_json,content_hash
from dalton_core.research_verification import MISSION_VERIFIED_FIGURE_RULE_REF
from tests.test_mission_annual_research import MissionAnnualFixture,COMPANY,NOW
from tests import test_mission_document_research as docfixtures
from tests.test_mission_document_research import RouteBoundCountingFakeAdapter
from tests.test_document_research import FakeLauncher
from tests import test_research_plan_annual_report as annual_support

TEXT='Synthetic issuer FY2025: Remaining performance obligations were $12.3 billion. The weighted average remaining contract life was 4.1 years. Uncommenced leases were $2.5 billion.'

class NumericWorkflowTests(unittest.TestCase):
    _record_plan=docfixtures.MissionDocumentResearchTests._record_plan
    _executor=docfixtures.MissionDocumentResearchTests._executor
    def fixture(self,*,missing=False,old_rejection=False,extra_text="",
                source_spec="annual-report-10k", strategy_version=CONTRACT_REF, basis="gaap-reported"):
        from tests import test_mission_annual_research as mission_fixture
        original_params=mission_fixture.mission_params
        def with_discovery(method):
            params=original_params(method)
            params['autonomy']={**params['autonomy'],'may_write':[*params['autonomy']['may_write'],'source_discovery']}
            return params
        from dalton_core.store import DaltonStore
        original_create=DaltonStore.create_policy
        def signed_numeric_policy(store,body=None,**kwargs):
            if kwargs.get('policy_version_id')=='governance-policy-version:mission-annual:2':
                body={**body,'research_candidate_auto_commit':{'enabled':True,'max_records':20,
                    'rules':[mission_fixture.DOCUMENT_QUALITATIVE_RULE_REF,MISSION_VERIFIED_FIGURE_RULE_REF]}}
            return original_create(store,body,**kwargs)
        with patch.object(mission_fixture,'mission_params',with_discovery), patch.object(annual_support,'TEXT',(TEXT.replace('Uncommenced leases were $2.5 billion.','The amount of uncommenced leases is not disclosed in this excerpt.') if missing else TEXT)+extra_text), patch.object(DaltonStore,'create_policy',signed_numeric_policy):
            f=MissionAnnualFixture(self,company_in_mandate=True,additional_connected_source='source:web-search',auto_commit=True)
        coverage=CoverageMissionAuthority(f.store)
        envelope=f.source.reader.get_source_envelope(f.source.manifest['discovery_source_envelope_ref'])
        invocation=f.source.reader.get_invocation(envelope['connector_invocation_ref'])
        authorization=coverage.authorize_source_discovery(company_ref=COMPANY,source_ref='source:web-search',requested_by='automation:test',mission_version_ref=f.mission['id'])
        discovery=coverage.record_source_discovery(authorization=authorization,
            discovery_plan_ref='discovery-plan:synthetic-numeric',discovery_plan_hash='d'*64,
            spec_ref=source_spec,query_hash='e'*64,parameters={'query':'annual report','date_after':'2025-01-01','date_before':'2026-01-01'},
            connector_invocation_ref=invocation['id'],connector_invocation_hash=invocation['content_hash'],
            source_envelope_ref=envelope['id'],source_envelope_hash=envelope['content_hash'],
            document_refs=envelope['source_record_refs'],in_authority_document_refs=[])
        document_ref=f.source.manifest['url_ref']
        discovered=next(d for d in coverage.discovered_documents(mission_version_ref=f.mission['id']) if d['document_ref']==document_ref)
        coverage.mark_discovered_document_launched(discovered['record_id'],f.source.ticket_ref)
        coverage.settle_discovered_document(discovered['record_id'],status='acquired')
        review=coverage.register_document_review(discovered['record_id'],requested_by='automation:test')
        adapter=PublicWebDocumentSourceAdapter(source_ref='source:public-web',
            launcher=FakeLauncher(f.source.manifest,f.source.ticket_ref,lookup_ref=document_ref,state_dir=f.state),
            core=f.store,spool=f.source.spool,receipt_reader=f.source.reader,
            max_source_chars=100000,max_pdf_pages=100,max_decompressed_bytes=1000000)
        adapters={'source:public-web':adapter}
        policy=build_document_research_policy(policy_ref='policy:synthetic-numeric',allowed_purposes=[PURPOSE],
            allowed_access_policy_refs=['policy:access:public-web'],max_question_chars=2000,
            max_query_terms=8,max_query_term_chars=160,max_results=8,max_context_before_chars=200,max_context_after_chars=500,max_read_chars=10000)
        registry=DocumentResearchRegistry(adapters=adapters,policy=policy,
            acquired_fetched_adapter=CoreAcquiredDocumentSourceAdapter(core=f.store,adapters=adapters,source_aliases={'source:web-search':'source:public-web'}))
        registration=registry.register_acquired_document(record_id=discovered['record_id'],purpose=PURPOSE)
        specs=[('rpo','Remaining performance obligations','Issuer consolidated remaining performance obligations','currency','USD','12.3','billion'),
               ('duration','weighted average remaining contract life','RPO weighted average remaining contract life at year-end','years',None,'4.1',None),
               ('leases','Uncommenced leases','Issuer total leases not yet commenced at year-end','currency','USD','2.5','billion')]
        targets=normalize_targets([{'target_ref':'target:'+ref,'metric_ref':'metric:synthetic-'+ref,
            'metric_definition':{'label':label,'meaning':meaning},'period':'FY2025',
            'unit':unit,'currency':currency,'basis':basis} for ref,label,meaning,unit,currency,value,scale in specs])
        inquiry={'rank':0,'company_ref':COMPANY,'question':'What are RPO total, recognition duration and uncommenced leases at FY2025?',
            'wants':'All three exact numeric facts from the original, each with explicit period and unit.',
            'because':'The specified original contains these issuer disclosure lines.',
            'directed_document':{'strategy_version':strategy_version,'document_ref':document_ref,
            'document_version_hash':registration['content_hash'],'query_terms':['Remaining performance obligations'],
            'query_rationale':'Read exactly these filed notes.','numeric_targets':targets}}
        plan={'schema_version':'0.1','task_ref':'task:research-plan-directives:0.1','created_at':f.mission['created_at'],
            'state_hash':'9'*64,'mission_version_ref':f.mission['id'],'assessment':'Read required typed numeric facts.',
            'directives':[],'inquiries':[inquiry],'sufficiency':[]};plan['content_hash']=content_hash(plan)
        authority=MissionDocumentResearchAuthority(f.store,registry=registry,
            registration_resolver=lambda ref:registration if ref==registration['id'] else None,
            model_execution_resolver=f.authority._model_authority,
            planner_scheduler_connection=f.harness.scheduler().connection,
            planner_router_connection=f.router.connection,clock=f.harness.clock)
        if old_rejection:
            import copy
            old_inquiry=copy.deepcopy(inquiry);old_inquiry['directed_document'].pop('numeric_targets')
            old_inquiry['directed_document']['strategy_version']='directed-document:0.1'
            old_plan={**plan,'state_hash':'8'*64,'inquiries':[old_inquiry]};old_plan.pop('content_hash');old_plan['content_hash']=content_hash(old_plan)
            for old_index in range(int(old_rejection)):
                old_inquiry['wants']=inquiry['wants']+(' Please give exact source citations.' if old_index else '')
                old_plan['state_hash']=str(8-old_index)*64
                old_plan.pop('content_hash');old_plan['content_hash']=content_hash(old_plan)
                stored_old=self._record_plan(f,old_plan)
                with patch.object(authority,'equivalent_open_admission',return_value=None):
                    # Historical repeats already present; today's ordinary old strategy stays blocked.
                    old_admission=admit_directed_inquiry(authority=authority,registrations={registration['id']:registration},
                        backlog=ResearchQuestionBacklog(f.store),mission=f.mission,plan=stored_old,inquiry=old_inquiry)
                wire=authority.resolve_for_execution(old_admission['admission_ref'])
                statement='Synthetic issuer reported remaining performance obligations of $12.3 billion at FY2025.'
                old_draft={'schema_version':'0.1','status':'answered','answer':statement,
                    'candidate':{'normalized_statement':statement,'metric_or_aspect':'remaining performance obligations',
                        'period':'FY2025','basis':'reported','cited_match_indexes':[0]},'missing':[]}
                old_executor,old_producer,old_verifier=self._executor(f,authority,draft_adapter=RouteBoundCountingFakeAdapter(old_draft))
                outcome=self.drive(old_executor,wire)
                self.assertEqual(outcome['research_status'],'candidate_rejected')
                from dalton_core.directed_numeric_research import numeric_rejection
                proof=numeric_rejection(f.store.connection,wire['id'])
            inquiry['directed_document']['replaces_numeric_rejection']={k:proof[k] for k in ('admission_ref','admission_hash','rejection_ref','rejection_hash')}
            plan['inquiries']=[inquiry];plan.pop('content_hash');plan['content_hash']=content_hash(plan)
        stored=self._record_plan(f,plan)
        admitted=admit_directed_inquiry(authority=authority,registrations={registration['id']:registration},
            backlog=ResearchQuestionBacklog(f.store),mission=f.mission,plan=stored,inquiry=inquiry)
        admission=authority.resolve_for_execution(admitted['admission_ref'])
        sourceproof=registry.search(admission['request']);quote_id=next(iter(quotes(sourceproof)))
        rows=[]
        for target,(_,label,meaning,unit,currency,value,scale) in zip(targets,specs):
            if missing and target['target_ref']=='target:leases':
                rows.append({'target_ref':target['target_ref'],'status':'unavailable','figure':None,'reason':'No explicit uncommenced lease amount in this excerpt.'})
            else:
                rows.append({'target_ref':target['target_ref'],'status':'supported','figure':{'quote_id':quote_id,
                    'metric_ref':target['metric_ref'],'subject_as_named':'Synthetic issuer','as_reported_label':label,
                    'value':value,'unit':unit,'currency':currency,'period':'FY2025','basis':basis,'scale':scale},'reason':None})
        draft={'schema_version':CONTRACT_REF,'targets':rows}
        verdict={'schema_version':'0.1','candidate_ref':candidate_ref(draft),'verdict':'pass',
                 'targets':[{'target_ref':r['target_ref'],'verdict':'accept' if r['status']=='supported' else 'unavailable',
                             'reason':None if r['status']=='supported' else 'The required disclosure is missing.'} for r in rows]}
        executor,producer,verifier=self._executor(f,authority,draft_adapter=RouteBoundCountingFakeAdapter(draft),
            verifier_adapter=RouteBoundCountingFakeAdapter(verdict))
        return f,authority,admission,executor,producer,verifier

    def drive(self,executor,admission):
        for _ in range(30):
            result=executor.run_once(admission['id'])
            if result['status']=='complete':return result
        self.fail('numeric execution did not complete')

    def test_three_required_figures_formal_claims_and_zero_call_replay(self):
        f,authority,admission,executor,producer,verifier=self.fixture()
        result=self.drive(executor,admission)
        self.assertEqual(result['research_status'],'numeric_targets_completed')
        self.assertEqual(len(result['numeric_result']['canonical_claims']),3)
        self.assertEqual(len(result['numeric_result']['completed_targets']),3)
        self.assertEqual(result,executor.run_once(admission['id']))
        self.assertEqual((producer.calls,verifier.calls),(1,1))
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM mission_document_research_outcomes').fetchone()[0],0)
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM mission_document_numeric_results').fetchone()[0],1)

    def test_partial_numeric_claims_do_not_complete_question_and_open_exact_request(self):
        f,authority,admission,executor,producer,verifier=self.fixture(missing=True)
        result=self.drive(executor,admission)
        self.assertEqual(result['research_status'],'numeric_waiting_evidence')
        self.assertEqual(len(result['numeric_result']['canonical_claims']),2)
        self.assertEqual(result['numeric_result']['pending_targets'][0]['target_ref'],'target:leases')
        self.assertEqual(len(result['numeric_result']['material_requests']),1)
        from dalton_core.material_requests import request
        from dalton_core.material_acquisition import evaluate_acceptance, serve
        request_ref=result['numeric_result']['material_requests'][0]['request_ref']
        held=request(f.state,request_ref)
        self.assertFalse(evaluate_acceptance(f.state,held,identity={})['met'])
        forbidden=unittest.mock.Mock(side_effect=AssertionError('numeric material rung must not purchase or close'))
        self.assertEqual(serve(f.state,request_ref,rungs={'source:web-search':forbidden})['outcome'],'waiting')
        self.assertEqual(request(f.state,request_ref)['status'],'open')
        forbidden.assert_not_called()
        self.assertEqual(result,executor.run_once(admission['id']))
        self.assertEqual((producer.calls,verifier.calls),(1,1))

    def test_exact_old_numeric_rejection_allows_one_new_typed_admission(self):
        f,authority,admission,executor,producer,verifier=self.fixture(old_rejection=2)
        self.assertEqual(len(admission['numeric_authority']['capability_upgrade_proofs']),2)
        result=self.drive(executor,admission)
        self.assertEqual(result['research_status'],'numeric_targets_completed')
        self.assertEqual((producer.calls,verifier.calls),(1,1))
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM mission_document_research_candidate_rejections').fetchone()[0],2)
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM mission_document_research_admissions').fetchone()[0],3)
        self.assertEqual(result,executor.run_once(admission['id']))
        self.assertEqual((producer.calls,verifier.calls),(1,1))
        old=f.store.connection.execute("SELECT admission_id FROM mission_document_research_admissions WHERE admission_id!=?",(admission['id'],)).fetchone()[0]
        from dalton_core.document_research_feedback import read_document_research_feedback
        feedback=read_document_research_feedback(f.store,f.mission)
        self.assertEqual(feedback['status'],'available')
        self.assertFalse(any(x.get('numeric_strategy_required') for x in feedback['by_company'][COMPANY]))
        self.assertTrue(any(x['outcome']=='numeric_targets_completed' for x in feedback['by_company'][COMPANY]))

    def test_partial_replacement_keeps_the_exact_old_numeric_requirement(self):
        f,authority,admission,executor,producer,verifier=self.fixture(old_rejection=1,missing=True)
        self.drive(executor,admission)
        from dalton_core.document_research_feedback import read_document_research_feedback
        feedback=read_document_research_feedback(f.store,f.mission)
        self.assertEqual(feedback['status'],'available')
        self.assertTrue(any(x.get('numeric_strategy_required') for x in feedback['by_company'][COMPANY]))
        self.assertTrue(any(x['outcome']=='numeric_waiting_evidence' for x in feedback['by_company'][COMPANY]))

    def test_missing_final_formal_result_cannot_clear_the_old_requirement(self):
        f,authority,admission,executor,producer,verifier=self.fixture(old_rejection=1)
        self.drive(executor,admission)
        work=f.store.connection.execute('SELECT work_order_ref FROM mission_document_numeric_results WHERE admission_ref=?',(admission['id'],)).fetchone()[0]
        connection=f.store.connection
        from types import SimpleNamespace
        class MissingFinal:
            def execute(self,sql,params=()):
                if sql.startswith('SELECT result_envelope_json,result_envelope_hash,terminal_state FROM scheduler_formal_results') and params==(work,):
                    return SimpleNamespace(fetchone=lambda:None)
                return connection.execute(sql,params)
        from dalton_core.document_research_feedback import read_document_research_feedback
        feedback=read_document_research_feedback(SimpleNamespace(connection=MissingFinal()),f.mission)
        self.assertEqual(feedback['status'],'available')
        self.assertTrue(any(x.get('numeric_strategy_required') for x in feedback['by_company'][COMPANY]))
        self.assertFalse(any(x['outcome']=='numeric_targets_completed' for x in feedback['by_company'][COMPANY]))

    def test_actual_window_and_typed_definition_drift_refuse_before_model_call(self):
        f,authority,admission,executor,producer,verifier=self.fixture()
        import copy
        from dalton_core.directed_numeric_output import parse_draft
        # Model output proves no right to re-label the same real digits.
        for field,replacement in (('period','FY2024'),('unit','count'),('currency','EUR'),('metric_ref','metric:other')):
            changed=copy.deepcopy(producer.candidate_wire)
            changed['targets'][0]['figure'][field]=replacement
            with self.subTest(field=field),self.assertRaises(ValueError):
                parse_draft(canonical_json(changed),metadata={'numeric_targets':admission['numeric_authority']['targets'],
                    'retrieval_proof':executor.registry.search(admission['request'])})
        self.assertEqual((producer.calls,verifier.calls),(0,0))

    def test_independent_reject_is_missing_evidence_not_a_qualitative_escalation(self):
        f,authority,admission,executor,producer,verifier=self.fixture()
        changed=verifier.candidate_wire
        changed['verdict']='reject';changed['targets'][0]={'target_ref':'target:rpo','verdict':'reject','reason':'The figure maps to a different economic scope.'}
        result=self.drive(executor,admission)
        self.assertEqual(result['research_status'],'numeric_waiting_evidence')
        self.assertEqual(result['canonical_claim_count'],0)
        self.assertEqual(len(result['numeric_result']['pending_targets']),3)
        self.assertEqual(result,executor.run_once(admission['id']))
        self.assertEqual((producer.calls,verifier.calls),(1,1))

    def test_failed_local_completion_after_receipt_keeps_dispatch_pending_and_replays_paid_stages(self):
        f,authority,admission,executor,producer,verifier=self.fixture()
        original=executor.scheduler.complete
        def crash(work_ref,*args,**kwargs):
            if kwargs.get('idempotency_key','').startswith('mission-document-numeric:'):
                raise RuntimeError('local result lost before formal settlement')
            return original(work_ref,*args,**kwargs)
        with patch.object(executor.scheduler,'complete',side_effect=crash):
            with self.assertRaisesRegex(RuntimeError,'local result lost'):
                self.drive(executor,admission)
        from dalton_core.mission_document_research import _settlement_instants
        self.assertNotIn(admission['id'],_settlement_instants(f.store.connection,[admission['id']]))
        # The durable local lease must expire normally before the next worker can finish.
        from datetime import timedelta
        f.harness.clock.value += timedelta(seconds=601)
        result=self.drive(executor,admission)
        self.assertEqual(result['research_status'],'numeric_targets_completed')
        self.assertEqual((producer.calls,verifier.calls),(1,1))

    def test_formal_claim_tamper_never_returns_numeric_completion(self):
        f,authority,admission,executor,producer,verifier=self.fixture()
        result=self.drive(executor,admission)
        claim_ref=result['numeric_result']['canonical_claims'][0]['claim_version_ref']
        triggers=f.store.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='claim_versions'").fetchall()
        for row in triggers:f.store.connection.execute('DROP TRIGGER '+row[0])
        row=f.store.connection.execute('SELECT claim_json FROM claim_versions WHERE claim_version_id=?',(claim_ref,)).fetchone()
        claim=json.loads(row[0]);claim['normalized_statement']='Tampered semantic assertion.'
        f.store.connection.execute('UPDATE claim_versions SET claim_json=? WHERE claim_version_id=?',(canonical_json(claim),claim_ref));f.store.connection.commit()
        with self.assertRaises(ValueError):executor.run_once(admission['id'])
        self.assertEqual((producer.calls,verifier.calls),(1,1))

    def test_missing_target_resumes_only_after_actual_new_registered_source_bytes(self):
        f,authority,admission,executor,producer,verifier=self.fixture(missing=True)
        result=self.drive(executor,admission)
        prior=result['numeric_result']
        import copy
        from dalton_core.mission_document_research import MissionDocumentResearchError
        next_inquiry=copy.deepcopy(admission['planner_inquiry'])
        next_inquiry['directed_document']['resumes_numeric_result']={
            'admission_ref':admission['id'],'admission_hash':admission['content_hash'],'numeric_result_hash':prior['content_hash']}
        def plan_for(inquiry,state):
            body={'schema_version':'0.1','task_ref':'task:research-plan-directives:0.1','created_at':f.mission['created_at'],
                  'state_hash':state,'mission_version_ref':f.mission['id'],'assessment':'Resume the exact missing numeric evidence.',
                  'directives':[],'inquiries':[inquiry],'sufficiency':[]};body['content_hash']=content_hash(body)
            return self._record_plan(f,body)
        same=plan_for(next_inquiry,'6'*64)
        with self.assertRaisesRegex(MissionDocumentResearchError,'actual new source bytes'):
            admit_directed_inquiry(authority=authority,registrations={admission['document_authority_ref']:admission['request']['registration']},
                backlog=ResearchQuestionBacklog(f.store),mission=f.mission,plan=same,inquiry=next_inquiry)
        self.assertEqual((producer.calls,verifier.calls),(1,1))
        # Normal, authorized local connector harness creates a different original; no external transport.
        from dalton_core.public_web_core_search import PublicWebCoreSearch,WebSearchConnectorGovernance,build_web_search_governance_record
        from dalton_core.public_web_core_fetch import PublicWebCoreFetch,WebFetchConnectorGovernance,build_web_fetch_governance_record,url_authority_from_discovery
        from dalton_core.public_http_transport import PublicHttpTransport
        from tests.test_research_plan_annual_report import FakeWebSearchHandle,_Response
        url=f.source.url+'?amended=synthetic'
        search=PublicWebCoreSearch(store=f.store,connectors=f.source.connectors,observability=f.source.observability,
            journal=f.source.journal,scheduler=f.harness.scheduler(),catalog=f.source.search_catalog,spool=f.source.spool,
            governance=WebSearchConnectorGovernance(build_web_search_governance_record(approved_by='human:test-owner',status='approved')),
            host_handle=FakeWebSearchHandle([{'url':url,'title':'Synthetic amended report'}]),clock=f.harness.clock)
        found=search.search(search.build_request({'query':'synthetic amended annual report','date_after':'2025-01-01','date_before':'2026-01-01'}))
        url_auth=url_authority_from_discovery(f.store.connection,f.source.spool,url_ref=found['document_refs'][0],source_envelope_ref=found['source_envelope_ref'])
        fetch=PublicWebCoreFetch(store=f.store,connectors=f.source.connectors,observability=f.source.observability,
            journal=f.source.journal,scheduler=f.harness.scheduler(),catalog=f.source.fetch_catalog,spool=f.source.spool,
            governance=WebFetchConnectorGovernance(build_web_fetch_governance_record(approved_by='human:test-owner',status='approved')),
            transport=PublicHttpTransport(resolver=lambda *_:('23.33.29.153',),exchange=lambda *_:_Response(('<html><body><p>'+TEXT+'</p></body></html>').encode())),clock=f.harness.clock)
        manifest=fetch.manifest(fetch.fetch(fetch.build_request(url_auth)))
        coverage=CoverageMissionAuthority(f.store);envelope=f.source.reader.get_source_envelope(found['source_envelope_ref']);invocation=f.source.reader.get_invocation(envelope['connector_invocation_ref'])
        grant=coverage.authorize_source_discovery(company_ref=COMPANY,source_ref='source:web-search',requested_by='automation:test',mission_version_ref=f.mission['id'])
        coverage.record_source_discovery(authorization=grant,discovery_plan_ref='discovery-plan:synthetic-amendment',discovery_plan_hash='4'*64,
            spec_ref='annual-report-10k',query_hash='5'*64,parameters={'query':'synthetic amended annual report','date_after':'2025-01-01','date_before':'2026-01-01'},
            connector_invocation_ref=invocation['id'],connector_invocation_hash=invocation['content_hash'],source_envelope_ref=envelope['id'],source_envelope_hash=envelope['content_hash'],
            document_refs=envelope['source_record_refs'],in_authority_document_refs=[])
        discovered=next(d for d in coverage.discovered_documents(mission_version_ref=f.mission['id']) if d['document_ref']==manifest['url_ref'])
        ticket='public-web-fetch:synthetic-numeric-resume'
        coverage.mark_discovered_document_launched(discovered['record_id'],ticket);coverage.settle_discovered_document(discovered['record_id'],status='acquired')
        coverage.register_document_review(discovered['record_id'],requested_by='automation:test')
        adapter=authority.registry.adapters['source:public-web'];adapter.launcher.add_version(ticket,manifest,lookup_ref=manifest['url_ref'])
        registration=authority.registry.register_acquired_document(record_id=discovered['record_id'],purpose=PURPOSE)
        old_resolver=authority.registration_resolver
        authority.registration_resolver=lambda ref:registration if ref==registration['id'] else old_resolver(ref)
        next_inquiry['directed_document'].update({'document_ref':registration['document_ref'],'document_version_hash':registration['content_hash']})
        next_plan=plan_for(next_inquiry,'7'*64)
        admitted=admit_directed_inquiry(authority=authority,registrations={registration['id']:registration},backlog=ResearchQuestionBacklog(f.store),mission=f.mission,plan=next_plan,inquiry=next_inquiry)
        next_admission=authority.resolve_for_execution(admitted['admission_ref'])
        self.assertNotEqual(next_admission['id'],admission['id'])
        self.assertEqual(next_admission['numeric_authority']['resume_proof']['numeric_result_hash'],prior['content_hash'])
        # All original required targets remain; no source gap becomes an owner task.
        self.assertEqual(next_admission['numeric_authority']['targets'],admission['numeric_authority']['targets'])
        self.assertEqual((producer.calls,verifier.calls),(1,1))
        resumed_draft=copy.deepcopy(producer.candidate_wire)
        next_quote=next(iter(quotes(authority.registry.search(next_admission['request']))))
        for row in resumed_draft['targets']:
            if row['status']=='supported':row['figure']['quote_id']=next_quote
            else:
                row.update({'status':'supported','reason':None,'figure':{'quote_id':next_quote,
                    'metric_ref':'metric:synthetic-leases','subject_as_named':'Synthetic issuer','as_reported_label':'Uncommenced leases',
                    'value':'2.5','unit':'currency','currency':'USD','period':'FY2025','basis':'gaap-reported','scale':'billion'}})
        resumed_verifier={'schema_version':'0.1','candidate_ref':candidate_ref(resumed_draft),'verdict':'pass',
            'targets':[{'target_ref':r['target_ref'],'verdict':'accept','reason':None} for r in resumed_draft['targets']]}
        next_executor,next_producer,next_verifier=self._executor(f,authority,draft_adapter=RouteBoundCountingFakeAdapter(resumed_draft),verifier_adapter=RouteBoundCountingFakeAdapter(resumed_verifier))
        final=self.drive(next_executor,next_admission)
        self.assertEqual(final['research_status'],'numeric_targets_completed')
        self.assertEqual(final['numeric_result']['resolved_material_requests'][0]['status'],'found')
        from dalton_core.material_requests import request
        fulfilled=request(f.state,prior['material_requests'][0]['request_ref'])
        self.assertEqual(fulfilled['status'],'found')
        self.assertEqual((next_producer.calls,next_verifier.calls),(1,1))
        self.assertEqual(final,next_executor.run_once(next_admission['id']))


    def test_same_original_new_verified_passages_can_resume_without_new_file(self):
        f,authority,admission,executor,producer,verifier=self.fixture(missing=True,
            extra_text='\n'+('Synthetic unrelated background. '*100)+'\nUncommenced leases were $2.5 billion at FY2025.')
        result=self.drive(executor,admission)
        self.assertEqual(result['research_status'],'numeric_waiting_evidence')
        import copy
        inquiry=copy.deepcopy(admission['planner_inquiry'])
        inquiry['directed_document']['resumes_numeric_result']={
            'admission_ref':admission['id'],'admission_hash':admission['content_hash'],
            'numeric_result_hash':result['numeric_result']['content_hash']}
        inquiry['directed_document']['query_terms'].append('Uncommenced leases')
        plan={'schema_version':'0.1','task_ref':'task:research-plan-directives:0.1','created_at':f.mission['created_at'],
            'state_hash':'5'*64,'mission_version_ref':f.mission['id'],'assessment':'Read genuinely new source passages.',
            'directives':[],'inquiries':[inquiry],'sufficiency':[]};plan['content_hash']=content_hash(plan)
        stored=self._record_plan(f,plan)
        next_admitted=admit_directed_inquiry(authority=authority,registrations={admission['document_authority_ref']:admission['request']['registration']},
            backlog=ResearchQuestionBacklog(f.store),mission=f.mission,plan=stored,inquiry=inquiry)
        next_wire=authority.resolve_for_execution(next_admitted['admission_ref'])
        self.assertEqual(next_wire['document_authority_hash'],admission['document_authority_hash'])
        self.assertEqual(next_wire['numeric_authority']['targets'],admission['numeric_authority']['targets'])
        def identity(proof):return {(m['source_start'],m['source_end'],m['excerpt_sha256']) for m in proof['matches']}
        self.assertTrue(identity(authority.registry.search(next_wire['request']))-identity(authority.registry.search(admission['request'])))
        self.assertEqual((producer.calls,verifier.calls),(1,1))
        self.assertEqual(authority.resolve_for_execution(next_wire['id']),next_wire)

    def test_bounded_query_miss_registers_missing_targets_without_any_model_call(self):
        f,authority,admission,executor,producer,verifier=self.fixture()
        # A fresh exact planner inquiry binds a query absent from the original.
        import copy
        inquiry=copy.deepcopy(admission['planner_inquiry'])
        inquiry['question']='Read separately scoped FY2025 lease contractual commitment details.'
        inquiry['directed_document']['query_terms']=['a literal query not contained in the original']
        plan={'schema_version':'0.1','task_ref':'task:research-plan-directives:0.1','created_at':f.mission['created_at'],
            'state_hash':'3'*64,'mission_version_ref':f.mission['id'],'assessment':'Perform a bounded independent query.',
            'directives':[],'inquiries':[inquiry],'sufficiency':[]};plan['content_hash']=content_hash(plan)
        stored=self._record_plan(f,plan)
        registration=admission['request']['registration']
        admitted=admit_directed_inquiry(authority=authority,registrations={registration['id']:registration},
            backlog=ResearchQuestionBacklog(f.store),mission=f.mission,plan=stored,inquiry=inquiry)
        current=authority.resolve_for_execution(admitted['admission_ref'])
        result=self.drive(executor,current)
        self.assertEqual(result['research_status'],'numeric_query_miss')
        self.assertEqual(len(result['numeric_material_requests']),3)
        self.assertEqual((producer.calls,verifier.calls),(0,0))
        self.assertEqual(result,executor.run_once(current['id']))
