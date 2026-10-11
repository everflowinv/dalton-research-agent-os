"""Real stored Work/formal/route/paid-proof diagnostics; no provider/network."""
import copy
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock
from tests import test_cockpit_model_fallback as fixture
from tests.synthetic_company import material_pack, strl_worksheet, strl_document, COMPANY
from dalton_core import initial_screen_cli as cli
from dalton_core import initial_screen_failure_feedback as feedback
from dalton_core.material_pack import _sha
from dalton_core.product_contract import normalise_pack
from dalton_core.initial_screen import build_worksheet_prompt, build_document_prompt
from dalton_core.store import DaltonStore, content_hash, canonical_json
from dalton_core.mission_deliverable import MissionDeliverableAuthority
from dalton_core.research_state import company_state, planning_core_hash, build_research_state
from dalton_core.research_planner import build_prompt

class JsonAdapter(fixture.ChainAdapter):
    def __init__(self, value):super().__init__({});self.value=value
    def execute(self, work, route, profile):
        invocation,result=super().execute(work,route,profile)
        return invocation,replace(result,outputs={'text':json.dumps(self.value,ensure_ascii=False)})

class InitialScreenFailureFeedbackTests(unittest.TestCase):
    def setUp(self):
        fixture.CockpitChainTests.setUp(self)
        self.store=DaltonStore(self.root/'core.sqlite');self.addCleanup(self.store.close)
        from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
        from dalton_core.coverage_mission import CoverageMissionAuthority
        self.methods=bootstrap_method_authorities(self.store)
        params=mission_params(self.methods);ref=params.pop('mission_ref')
        params['universe']=[{'company_ref':COMPANY,'ticker':'SYNW','coverage_tier':'A','bootstrap_priority':'P0'}]
        self.mission=CoverageMissionAuthority(self.store).create_mission(ref,**params)
        self.adapter=JsonAdapter({})
        self.model=fixture.CockpitChainTests._model(self,self.adapter,
            policy_version_ref=self.chain_policy,initial_screen_output_tokens=16000)
        self.model.config['purpose_call_budgets']['initial_screen'].update(max_cost_usd=1, timeout_seconds=180)
        self.authority=MissionDeliverableAuthority(self.store)
        self.pack=normalise_pack(material_pack())
        self.pack['content_hash']=_sha({'recipe':self.pack['recipe_version'],'text':self.pack['text'],'tags':self.pack['tags']})
        self.company={'company_ref':COMPANY,'ticker':'SYNW'}
        self.context={'stage':'worksheet','company':self.company,'pack':self.pack,'prior_reference':None,'worksheet_repair':[],'document_repair':[],'worksheet_bound':True,'document_bound':True,'source_bindings':{}}
        prompt=feedback.validation_prompt(build_worksheet_prompt(company=self.company,pack=self.pack),self.context)
        self.call=self.model.call(purpose='initial_screen',request_id='one-worksheet',prompt=prompt,mission=self.mission)
        self.directory=self.root/'initial-screens'/('a'*24);self.directory.mkdir(parents=True,mode=0o700)
        self.ticket(self.directory)

    def ticket(self,directory,status='failed'):
        value={'id':'initial-screen:'+directory.name,'status':status,'exit_code':1 if status=='failed' else None,'completed_at':(fixture.NOW+timedelta(minutes=2)).isoformat()}
        feedback._write(directory/'ticket.json',value)
        feedback._write(directory/'summary.json',{'status':'failed','drafted':self.company})

    def save(self, directory=None, context=None, calls=None):
        return feedback.save_failure(state_dir=self.root,summary_dir=directory or self.directory,
            model=self.model,mission=self.mission,context=context or self.context,
            calls=calls or {'worksheet':self.call},authority=self.authority)

    def read(self):
        return feedback.read_feedback(self.store.connection,state_dir=self.root,
            mission=self.mission,company_ref=COMPANY,config=self.model.config)

    def mutate(self, change):
        p=self.directory/'validation-feedback.json';v=json.loads(p.read_text());change(v)
        v['content_hash']=content_hash({k:x for k,x in v.items()if k!='content_hash'})
        feedback._write(p,v)

    def test_real_formal_failure_receipt_replays_and_reaches_final_prompt(self):
        self.save();result=self.read();self.assertEqual(result['status'],'verified')
        item=result['items'][0];self.assertEqual(item['findings'][0]['code'],'worksheet_shape')
        self.assertFalse(item['financial_evidence']);self.assertFalse(item['retry_authority'])
        state=build_research_state(mission=self.mission,checklist=[self.company],
            initial_screen_feedback_by_company={COMPANY:result},as_of=fixture.NOW.isoformat())
        prompt=build_prompt(state);self.assertIn('worksheet_shape',prompt);self.assertIn(item['semantic_hash'],prompt)
        self.assertEqual(len(self.adapter.served),1)

    def test_weak_old_summary_never_self_authenticates(self):
        self.assertEqual(self.read(),{'status':'not_recorded','items':[],'legacy_summary_authority':'unverified'})
        self.assertEqual(len(self.adapter.served),1)

    def test_same_failure_across_tickets_keeps_material_identity_and_no_new_call(self):
        self.save();one=self.read()
        other=self.directory.with_name('b'*24);other.mkdir(mode=0o700);self.ticket(other)
        self.save(directory=other);two=self.read()
        self.assertEqual(one['items'][0]['semantic_hash'],two['items'][0]['semantic_hash'])
        self.assertNotEqual(one['items'][0]['receipt_hash'],two['items'][0]['receipt_hash'])
        def state(result):return build_research_state(mission=self.mission,checklist=[self.company],initial_screen_feedback_by_company={COMPANY:result},as_of=fixture.NOW.isoformat())
        self.assertEqual(planning_core_hash(state(one)),planning_core_hash(state(two)))
        self.assertEqual(len(self.adapter.served),1)

    def test_rehashed_finding_or_company_or_pack_tampering_is_unverified(self):
        self.save();path=self.directory/'validation-feedback.json';original=path.read_bytes()
        for change in (lambda v:v['findings'][0].update(detail='invented missing evidence'),
                       lambda v:v['context']['company'].update(company_ref='company:ticker:other'),
                       lambda v:v['context']['pack'].update(text='changed original')):
            with self.subTest(change=change):
                self.mutate(change);self.assertEqual(self.read()['status'],'unverified');path.write_bytes(original)

    def test_missing_or_wrong_formal_route_binding_is_unverified(self):
        self.save();self.mutate(lambda v:v['calls']['worksheet'].update(route_decision_ref='route:not-real'))
        self.assertEqual(self.read()['status'],'unverified')
        self.assertEqual(len(self.adapter.served),1)

    def test_running_new_candidate_keeps_previous_terminal_failure(self):
        self.save();old=self.read()['items'][0]
        other=self.directory.with_name('c'*24);other.mkdir(mode=0o700);self.ticket(other,status='running')
        self.save(directory=other);result=self.read()
        self.assertEqual(result['items'][0]['receipt_hash'],old['receipt_hash'])

    def test_non_private_or_symlinked_receipt_is_refused(self):
        self.save();p=self.directory/'validation-feedback.json';p.chmod(0o644)
        self.assertEqual(self.read()['status'],'unverified');p.chmod(0o600)
        data=p.read_bytes();p.unlink();target=self.root/'other.json';target.write_bytes(data);target.chmod(0o600);p.symlink_to(target)
        self.assertEqual(self.read()['status'],'unverified')

    def test_document_receipt_recomputes_exact_formal_draft_findings(self):
        from dalton_core.product_contract import tag_entry
        held={tag_entry(self.pack,tag)['ref']:tag_entry(self.pack,tag)['text'] for tag in self.pack['tags'] if tag.startswith('P')}
        self.context['source_bindings']={ref:{'document_ref':ref,'document_sha256':content_hash(text),'start':0,'end':len(text)} for ref,text in held.items()}
        self.adapter.value=strl_worksheet()
        prompt=feedback.validation_prompt(build_worksheet_prompt(company=self.company,pack=self.pack),self.context)
        worksheet_call=self.model.call(purpose='initial_screen',request_id='accepted-worksheet',prompt=prompt,mission=self.mission)
        value=strl_document();value['sections'][0]['numbers']=[{'tag':'P1','quote':'999999 not in this source','metric':'unsupported','period':'FY2025','unit':'USD'}]
        self.adapter.value=value
        doc_call=self.model.call(purpose='initial_screen',request_id='refused-document',
            prompt=feedback.validation_prompt(build_document_prompt(company=self.company,pack=self.pack,worksheet=strl_worksheet()),self.context),mission=self.mission)
        context={**self.context,'stage':'document'}
        from dalton_core.product_contract import tag_entry
        held={tag_entry(self.pack,tag)['ref']:tag_entry(self.pack,tag)['text'] for tag in self.pack['tags'] if tag.startswith('P')}
        originals={ref:{'document_ref':ref,'document_sha256':content_hash(text),'passage_hash':content_hash(text),'start':0,'end':len(text),'text':text} for ref,text in held.items()}
        with mock.patch.object(feedback,'_checked_originals',return_value=originals):
            self.save(context=context,calls={'worksheet':worksheet_call,'document':doc_call})
            result=self.read()
        self.assertEqual(result['status'],'verified')
        self.assertIn('quote_not_in_passage',{r['code']for r in result['items'][0]['findings']})
        self.assertGreaterEqual(result['items'][0]['omitted'],0)
        self.assertEqual(len(self.adapter.served),3)

    def test_actual_cli_failures_record_formal_proof_without_changing_attempt_limit(self):
        args=SimpleNamespace(dry_run=False,as_of=None,summary_dir=str(self.directory),timeout_seconds=180)
        summary={'created_at':fixture.NOW.isoformat()}
        with mock.patch.object(cli,'check_sufficiency',return_value={'drafting_allowed':True,'status':'sufficient'}), \
             mock.patch.object(cli,'build_pack',return_value=self.pack),mock.patch.object(cli,'record_pack_build'):
            result=cli._run_strl(args,state=self.root,store=self.store,mission=self.mission,
                playbook={},entry=self.company,summary=summary,deliverable_authority=self.authority,
                missions=None,claims={},model=self.model)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['validation_feedback']['status'],'recorded')
        self.assertEqual(len(self.adapter.served),3) # setup1 + originalworksheet1 + its existingrepair1
        self.assertEqual(self.read()['status'],'verified')

    def test_old_paid_work_and_failure_replay_without_binding_marker_or_new_charge(self):
        prompt=build_worksheet_prompt(company=self.company,pack=self.pack)
        old=self.model.call(purpose='initial_screen',request_id='legacy-worksheet',prompt=prompt,mission=self.mission)
        marked,bound=feedback.prepare_prompt(self.model,prompt=prompt,context=self.context,request_id='legacy-worksheet',mission=self.mission)
        self.assertEqual(marked,prompt);self.assertFalse(bound)
        again=self.model.call(purpose='initial_screen',request_id='legacy-worksheet',prompt=marked,mission=self.mission)
        self.assertEqual(again['work_order_ref'],old['work_order_ref']);self.assertTrue(again['replayed'])
        self.assertEqual(len(self.adapter.served),2)
        with self.assertRaisesRegex(ValueError,'legacy worksheet'):
            self.save(context={**self.context,'worksheet_bound':False},calls={'worksheet':old})
        self.assertEqual(self.read()['status'],'not_recorded')

    def test_unverified_or_pending_without_valid_feedback_does_not_wake_planner(self):
        def state(result):return build_research_state(mission=self.mission,checklist=[self.company],initial_screen_feedback_by_company={COMPANY:result},as_of=fixture.NOW.isoformat())
        baseline=planning_core_hash(state({'status':'not_recorded','items':[]}))
        for status in ('pending_terminal','unverified','cleared_by_published_product'):
            self.assertEqual(planning_core_hash(state({'status':status,'items':[]})),baseline)
        self.save();verified=self.read()
        self.assertNotEqual(planning_core_hash(state(verified)),baseline)
        self.assertEqual(planning_core_hash(state({'status':'cleared_by_published_product','items':[]})),baseline)

    def test_real_metadata_change_changes_semantic_identity_but_not_ticket_only(self):
        findings=[{'code':'worksheet_shape','detail':'same missing field'}]
        before=feedback._semantic(self.mission,self.context,findings)[0]
        changed=copy.deepcopy(self.context);changed['pack']['json']['number_tags']['N1']['value']='1234'
        self.assertEqual(changed['pack']['content_hash'],self.context['pack']['content_hash'])
        self.assertNotEqual(before,feedback._semantic(self.mission,changed,findings)[0])

    def test_rehashed_pack_metadata_cannot_certify_the_frozen_formal_input(self):
        self.save();self.mutate(lambda v:v['context']['pack']['json'].update(number_tags={'N1':{'value':'1234'}}))
        self.assertEqual(self.read()['status'],'unverified')

    def test_core_mission_wrong_hash_or_missing_binding_is_unverified(self):
        self.save()
        for bad in ({**self.mission,'content_hash':'0'*64},{**self.mission,'id':'coverage-mission-version:missing:1'}):
            with self.subTest(mission=bad['id']):
                result=feedback.read_feedback(self.store.connection,state_dir=self.root,mission=bad,company_ref=COMPANY,config=self.model.config)
                self.assertEqual(result['status'],'unverified')
        self.assertEqual(self.read()['status'],'verified')

    def test_missing_or_other_mission_admission_cannot_authenticate_formal_success(self):
        self.save()
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
        original=ThesisImpactBudgetStore.admission
        for binding in (None,{'mission_version_ref':self.mission['id'],'mission_version_hash':'0'*64},
                        {'mission_version_ref':'coverage-mission-version:other:1','mission_version_hash':self.mission['content_hash']}):
            def admission(instance,**kwargs):
                result=copy.deepcopy(original(instance,**kwargs));result['mission_binding']=binding;return result
            with mock.patch.object(ThesisImpactBudgetStore,'admission',admission):
                self.assertEqual(self.read()['status'],'unverified')
        def unsettled(instance,**kwargs):
            result=copy.deepcopy(original(instance,**kwargs));result['settlement']=None;return result
        with mock.patch.object(ThesisImpactBudgetStore,'admission',unsettled):
            self.assertEqual(self.read()['status'],'unverified')
        self.assertEqual(self.read()['status'],'verified')

    def test_registered_original_not_index_or_replacement_authenticates_passage(self):
        sections=[{'claim_refs':[],'source_refs':[{'ref':'doc-passage:original'}]}]
        pack=copy.deepcopy(self.pack)
        pack['tags']={'P1':{'ref':'doc-passage:original','text':'exact frozen quote'}}
        original={'text':'exact frozen quote','document_ref':'document:original','document_sha256':'a'*64,'start':0,'end':18}
        with mock.patch.object(feedback,'_checked_originals',return_value={'doc-passage:original':original}):
            _,_,_,proof=feedback._source_resolvers(self.authority,sections,pack,{'doc-passage:original':{k:original[k] for k in ('document_ref','document_sha256','start','end')}})
            self.assertEqual(proof['doc-passage:original']['document_sha256'],'a'*64)
        with mock.patch.object(feedback,'_checked_originals',side_effect=ValueError('original passage unavailable')):
            with self.assertRaisesRegex(ValueError,'original passage'):
                feedback._source_resolvers(self.authority,sections,pack,{'doc-passage:original':{k:original[k] for k in ('document_ref','document_sha256','start','end')}})
        replacement={**original,'text':'new replacement quote','document_sha256':'b'*64}
        with mock.patch.object(feedback,'_checked_originals',return_value={'doc-passage:original':replacement}):
            with self.assertRaisesRegex(ValueError,'frozen call'):
                feedback._source_resolvers(self.authority,sections,pack,{'doc-passage:original':{k:original[k] for k in ('document_ref','document_sha256','start','end')}})

    def test_real_local_spool_proves_exact_passage_and_tampering_is_refused(self):
        from tests import test_evidence_index as evidence_fixture
        from dalton_core import evidence_index as index
        import hashlib
        state=self.root/'originals';state.mkdir()
        builder=evidence_fixture.CorpusBuilder(state)
        self.addCleanup(builder.close)
        text=evidence_fixture.TRANSCRIPT
        document=builder.alphaengine('original','100001',text,spec='earnings-call-transcripts',
            title='Synthetic transcript',broker=None,published='2026-10-01',named=['Synthetic Widgets'])
        builder.db.commit()
        result=index.index_acquired_manifest(state,state/'acquisitions/original/manifest.json')
        self.assertEqual(result['indexed'],1)
        found=index.passages_for_document(state,document)[0]
        pack={'tags':{'P1':{'ref':found['passage_ref'],'text':found['text']}},'text':'','json':{}}
        authority=SimpleNamespace(connection=self.store.connection,store=SimpleNamespace(path=str(state/'core.sqlite')),cell_resolver=self.authority.cell_resolver)
        _,_,_,proof=feedback._source_resolvers(authority,[{'claim_refs':[],'source_refs':[{'ref':found['passage_ref']}]}],pack,{found['passage_ref']:{k:found[k] for k in ('document_ref','document_sha256','start','end')}})
        self.assertEqual(proof[found['passage_ref']]['document_sha256'],found['document_sha256'])
        digest=hashlib.sha256(text.encode()).hexdigest();obj=state/'connector-spool/objects'/digest[:2]/digest
        obj.write_text('tampered original')
        with self.assertRaisesRegex(ValueError,'original passage'):
            feedback._source_resolvers(authority,[{'claim_refs':[],'source_refs':[{'ref':found['passage_ref']}]}],pack,{found['passage_ref']:{k:found[k] for k in ('document_ref','document_sha256','start','end')}})

    def test_only_used_passages_are_checked_and_reading_is_time_bounded(self):
        with mock.patch.object(feedback,'_checked_originals') as checked:
            feedback._source_resolvers(self.authority,[{'claim_refs':[],'source_refs':[]}],self.pack,{})
            checked.assert_not_called()
        import subprocess
        with mock.patch('subprocess.run',side_effect=subprocess.TimeoutExpired('local-source-check',4)) as run:
            with self.assertRaisesRegex(ValueError,'verification_timeout'):
                feedback._checked_originals(self.root,{'doc-passage:one'})
            self.assertEqual(run.call_args.kwargs['timeout'],4)
        with mock.patch('subprocess.run') as run:
            with self.assertRaisesRegex(ValueError,'original_passage_limit'):
                feedback._checked_originals(self.root,{f'doc-passage:{n}'for n in range(65)})
            run.assert_not_called()

    def test_repeated_reads_preserve_every_budget_row_and_fee(self):
        self.save()
        from dalton_core.planner_paid_output_recovery import _authority_connection
        def snapshot():
            with _authority_connection(self.root/'budget.sqlite') as connection:
                names=[row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'thesis_impact_%' ORDER BY name")]
                return {name:[tuple(row) for row in connection.execute('SELECT * FROM '+name+' ORDER BY rowid')]for name in names}
        before=snapshot()
        for _ in range(3):self.assertEqual(self.read()['status'],'verified')
        self.assertEqual(snapshot(),before);self.assertEqual(len(self.adapter.served),1)

    def test_source_version_change_with_identical_quote_is_refused(self):
        pack={'tags':{'P1':{'ref':'doc-passage:original','text':'same quote'}},'text':'','json':{}}
        frozen={'document_ref':'document:original','document_sha256':'a'*64,'start':0,'end':10}
        changed={**frozen,'document_sha256':'b'*64,'text':'same quote'}
        with mock.patch.object(feedback,'_checked_originals',return_value={'doc-passage:original':changed}):
            with self.assertRaisesRegex(ValueError,'version differs'):
                feedback._source_resolvers(self.authority,[{'claim_refs':[],'source_refs':[{'ref':'doc-passage:original'}]}],pack,{'doc-passage:original':frozen})

    def test_many_passages_share_one_original_load_and_bounded_spool(self):
        from tests import test_evidence_index as evidence_fixture
        from dalton_core import evidence_index as index, evidence_corpus
        state=self.root/'shared-original';state.mkdir()
        builder=evidence_fixture.CorpusBuilder(state);self.addCleanup(builder.close)
        document=builder.alphaengine('same','100002',evidence_fixture.TRANSCRIPT,spec='earnings-call-transcripts',
            title='Synthetic',broker=None,published='2026-10-01',named=['Synthetic Widgets'])
        builder.db.commit();index.index_acquired_manifest(state,state/'acquisitions/same/manifest.json')
        found=index.passages_for_document(state,document)[0]
        refs=[index.make_passage_ref(document,found['start'],found['start']+n,found['text'][:n])for n in range(30,94)]
        with mock.patch('dalton_core.evidence_corpus.load_document',wraps=evidence_corpus.load_document) as load:
            result=feedback._original_passages(state,refs)
            self.assertEqual(len(result),64);self.assertEqual(load.call_count,1)
        with mock.patch.object(feedback,'MAX_SOURCE_BYTES',32):
            with self.assertRaisesRegex(ValueError,'original_raw_byte_limit'):
                feedback._original_passages(state,refs)

    def test_document_count_limit_is_checked_before_loading_any_original(self):
        from contextlib import contextmanager
        @contextmanager
        def connection(path):yield SimpleNamespace()
        def span(index,ref):return {'document_ref':ref,'document_sha256':'a'*64,'text':'one','start':0,'end':3}
        with mock.patch('dalton_core.planner_paid_output_recovery._authority_connection',connection), \
             mock.patch('dalton_core.evidence_index._resolve',side_effect=span), \
             mock.patch('dalton_core.evidence_corpus.load_document') as load:
            with self.assertRaisesRegex(ValueError,'original_document_limit'):
                feedback._original_passages(self.root,[f'doc-passage:{n}'for n in range(17)])
            load.assert_not_called()

    def test_actual_timeout_reader_child_is_reaped(self):
        import subprocess,sys,os
        real_run=subprocess.run;pids=[]
        def sleeping_child(*args,**kwargs):
            try:return real_run([sys.executable,'-c','import os,time;print(os.getpid(),flush=True);time.sleep(5)'],**kwargs)
            except subprocess.TimeoutExpired as exc:
                pids.append(int(exc.stdout.decode() if isinstance(exc.stdout,bytes) else exc.stdout));raise
        with mock.patch.object(feedback,'SOURCE_TIMEOUT_SECONDS',0.2),mock.patch('subprocess.run',side_effect=sleeping_child):
            with self.assertRaisesRegex(ValueError,'original_verification_timeout'):
                feedback._checked_originals(self.root,{'doc-passage:one'})
        self.assertEqual(len(pids),1)
        with self.assertRaises(ProcessLookupError):os.kill(pids[0],0)

    def test_exact_claim_retirement_scope_matches_shared_rule_and_withdrawal(self):
        import sqlite3
        from dalton_core.claim_retirement import retired_claim_version_refs
        connection=sqlite3.connect(':memory:');self.addCleanup(connection.close)
        connection.executescript("CREATE TABLE claim_retirement_decisions(claim_version_ref TEXT,decision TEXT); CREATE TABLE claim_retirement_reinstatements(claim_version_ref TEXT,reinstatement_id TEXT); CREATE TABLE claim_retirement_reinstatement_withdrawals(reinstatement_ref TEXT);")
        connection.executemany('INSERT INTO claim_retirement_decisions VALUES (?,?)',[('used','retired'),('other','retired')])
        for stage in range(3):
            self.assertEqual(feedback._retired_refs(connection,{'used'}),retired_claim_version_refs(connection)&{'used'})
            if stage==0:connection.execute("INSERT INTO claim_retirement_reinstatements VALUES ('used','reinstate-used')")
            elif stage==1:connection.execute("INSERT INTO claim_retirement_reinstatement_withdrawals VALUES ('reinstate-used')")
        self.assertEqual(feedback._retired_refs(connection,set()),set())

    def test_successful_later_published_product_clears_failure_without_call(self):
        self.save()
        # Real Core publication, imported version only to keep this fixture
        # focused on the public pointer/hash read rather than drafting quality.
        params={'kind':'initial_screen','subject_ref':COMPANY,'mission':self.mission,
                'playbook':self.methods['playbook'],'template_ref':'template:test',
                'sections':[{'title':'已有研究','body':'原有结论。','claim_refs':[],'numbers':[],'gaps':[]}],
                'summary':'原有结论。','actor_ref':'human:coverage-owner','as_version_zero':True,
                'revision':{'change_reason':'imported_prior','evidence_refs':['source:synthetic-import']}}
        self.authority.clock=lambda:(fixture.NOW+timedelta(minutes=3)).isoformat()
        self.authority.publish(**params)
        self.assertEqual(self.read()['status'],'cleared_by_published_product')
        self.assertEqual(len(self.adapter.served),1)

    def test_each_failure_code_survives_bounded_diagnostic_projection(self):
        rows=[{'code':code,'section':str(n),'detail':'缺因'} for code in ('appendix_request_not_cited','citation_or_contract','forecast_gap_without_expression','machine_precision_number','quote_not_in_passage') for n in range(3)]
        compact,omitted=feedback._compact_findings(rows)
        self.assertEqual(len(compact),8);self.assertEqual(len(omitted),7)
        self.assertEqual({row['code'] for row in compact},{row['code'] for row in rows})

    def test_authority_conflict_is_unverified_without_changing_original_failure(self):
        from dalton_core.model_router import ModelRouter,ModelRouterConflict
        self.save()
        with mock.patch.object(ModelRouter,'get_decision',side_effect=ModelRouterConflict('synthetic authority drift')):
            self.assertEqual(self.read()['status'],'unverified')
        self.assertEqual(self.read()['status'],'verified')
        self.assertEqual(len(self.adapter.served),1)

    def test_all_companies_and_failure_bindings_survive_bounded_prompt(self):
        self.save();result=self.read();entries=[];feedbacks={}
        for i in range(5):
            company={'company_ref':f'company:ticker:synthetic{i}','ticker':f'SYN{i}','items':[]}
            entries.append(company);record=copy.deepcopy(result);record['items'][0]['company_ref']=company['company_ref']
            feedbacks[company['company_ref']]=record
        state=build_research_state(mission=self.mission,checklist=entries,initial_screen_feedback_by_company=feedbacks,as_of=fixture.NOW.isoformat())
        prompt=build_prompt(state);self.assertLessEqual(len(prompt.encode()),64000)
        for company in entries:self.assertIn(company['company_ref'],prompt)
        projected=json.loads(prompt.split('RESEARCH_STATE=',1)[1])
        for company in projected['companies']:
            self.assertEqual(company['initial_screen_validation_feedback']['items'][0]['findings'][0]['code'],'worksheet_shape')
