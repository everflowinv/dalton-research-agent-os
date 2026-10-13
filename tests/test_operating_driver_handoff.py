import copy,json,unittest
from pathlib import Path
from dalton_core.store import content_hash
from dalton_core.scheduler import Scheduler
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.company_model_state import build_company_model_state,model_spec_numeric_context_config,model_spec_prompt_byte_limit
from dalton_core.model_spec_joint_field_diagnostics import open_source_request
from dalton_core.operating_driver_source_plan import derive_current_plan,admit_found_materials,replay_plan
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core import material_requests
from tests import test_directed_numeric_workflow as numeric_fixture
from tests.test_company_model_state import _line
from tests import test_model_spec_joint_field_diagnostics as model_fixture


class StrongOperatingHandoffTests(unittest.TestCase):
    def setup_origin(self, *, twelve=False, native=False):
        support=numeric_fixture.NumericWorkflowTests('test_three_required_figures_formal_claims_and_zero_call_replay')
        self.addCleanup(support.doCleanups)
        periods=[('2026-01-01','2026-03-31')] if not twelve else [('2023-07-01','2023-09-30'),('2024-07-01','2024-09-30'),('2025-07-01','2025-09-30')]
        extra=''.join('</p><p>'+('Neutral source separator. '*70)+'</p><p>'+start+'/'+end+
            ' Units sold (tons) | 40 Average selling price (USD per ton) | 12 Natural gas cost (USD per MMBtu) | 3 Plant utilization (percent) | 82' for start,end in periods)
        if native:
            import calendar
            from datetime import date
            tables=[]
            for start,end in periods:
                day=date.fromisoformat(end)
                months=calendar.month_name[day.month]
                header=f'Three months ended {months} {day.day},'
                for label,values,caption in [('Units sold (tons) (000s)',('40','38'),'Physical operating volumes'),
                        ('Average selling price per ton',('12','11'),'Dollars per ton')]:
                    tables.append('</p><table><tr><td colspan="3">'+caption+'</td></tr>'+
                        '<tr><td></td><td colspan="2">'+header+'</td></tr>'+
                        '<tr><td></td><td>'+str(day.year)+'</td><td>'+str(day.year-1)+'</td></tr>'+
                        '<tr><td>'+label+'</td><td>'+values[0]+'</td><td>'+values[1]+'</td></tr></table><p>')
            extra=''.join(tables)
            old_response=numeric_fixture.annual_support._Response
            def native_response(body):
                xml=body.decode().replace('<html>', '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:dei="http://xbrl.sec.gov/dei/2026">')
                xml=xml.replace('<body>', '<body><ix:header><xbrli:unit id="u"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit></ix:header><ix:nonNumeric name="dei:CurrentFiscalYearEndDate">--12-31</ix:nonNumeric>')
                return old_response(xml.encode())
            from unittest.mock import patch
            with patch.object(numeric_fixture.annual_support,'_Response',native_response):
                f,owner,old,_,_,_=support.fixture(extra_text=extra)
        else:
            f,owner,old,_,_,_=support.fixture(extra_text=extra)
        missions=owner._missions
        f.mission=missions.mission(f.mission['id'])
        authorization=missions.authorize_sec_lane(company_ref=old['company_ref'],ticker='TEST',
            actor_ref=f.mission['autonomy']['automation_principal'],mission_version_ref=f.mission['id'],
            mission_version_hash=f.mission['content_hash'])
        dispatch=missions.queue_statement_dispatch(authorization=authorization,form='10-Q',attempt=0)
        missions.mark_statement_dispatch_launched(dispatch['dispatch_id'],'sec-financials-run:'+'1'*24)
        missions.record_statement_observation(dispatch_id=dispatch['dispatch_id'],observation={
            'schema_version':'0.1','cik':'0001467373','entity_name':'Synthetic issuer',
            'filings':[{'accession':'0001467373-26-'+f'{31+i:06d}','form':'10-Q','filed':'2026-04-30',
                'report_date':end,'lines':[_line(c,period_start=start,period_end=end,value=v) for c,v in [('us-gaap:Revenues','480'),('us-gaap:IncomeBeforeTax','100'),('us-gaap:IncomeTaxExpenseBenefit','20'),('us-gaap:NetIncomeLoss','80')]]} for i,(start,end) in enumerate(periods)],
            'source_record_refs':['raw-sink:'+'c'*64],'next_cursor':None,'provider_status':200},
            governance_ref='g',governance_hash='b'*64)
        missions.settle_statement_dispatch(dispatch['dispatch_id'],outcome='succeeded')
        state=build_company_model_state(missions,old['company_ref'],ticker='TEST')
        _,bad=model_fixture.source_case(old['company_ref'])
        if twelve:
            bad['operating_metrics'] += [{'ref':ref,'label':label,'unit':unit,'periodicity':'quarterly','disclosed':False,'because':'A source-required metric; no values in the filed statement input.'} for ref,label,unit in [('gas','Natural gas cost','usd'),('utilization','Plant utilization','percent')]]
        root_fixture=model_fixture.JointAdmissionTests('runTest');root_fixture.addCleanup=self.addCleanup
        from unittest.mock import patch
        original_model=model_fixture.runtime.CockpitChainTests._model
        def model_in_state(instance,*args,**kwargs):
            model=original_model(instance,*args,**kwargs)
            model.scheduler_db=str(f.state/'scheduler.sqlite')
            return model
        with patch.object(model_fixture.runtime.CockpitChainTests,'_model',model_in_state):
            runtime,_,_,_,model,adapter,call=root_fixture.fixture(state_body=(state,bad),outputs=[bad],mission=f.mission)
        # Rebuild under exactly the configured call's numeric projection limits.
        state=build_company_model_state(missions,old['company_ref'],ticker='TEST',
            numeric_context_policy=model_spec_numeric_context_config(model.config),
            prompt_byte_limit=model_spec_prompt_byte_limit(model.config))
        # The default policy is the same one the first root used.
        root_scheduler=Scheduler(model.scheduler_db);self.addCleanup(root_scheduler.close)
        owner.planner_scheduler_connection=root_scheduler.connection
        owner.model_source_plan_resolver=lambda plan,mission: replay_plan(owner,plan,mission,state_dir=f.state,model_config=model.config)
        opened=open_source_request(missions=missions,model=model,state=state,mission=f.mission,call=call,state_dir=f.state)
        registration=old['request']['registration']
        for item in opened:
            material_requests.close(f.state,item['id'],status='found',reason='Held exact registered operating original.',
                items=[{'document_ref':registration['document_ref']}])
        return f,owner,model,call,adapter,opened,registration

    def completed_context(self, *, native=False):
        f,owner,model,call,adapter,opened,registration=self.setup_origin(native=native)
        if native: self.readonly_files(f,owner,model)
        results=admit_found_materials(authority=owner,registrations={registration['id']:registration},
            backlog=ResearchQuestionBacklog(f.store),mission=f.mission,state_dir=f.state,model_config=model.config)
        self.assertEqual([r['status'] for r in results],['fresh','fresh'],results)
        for item in results:
            admission=owner.resolve_for_execution(item['admission_ref'])
            self.assertEqual(admission['numeric_authority']['contract_ref'],'directed-document-operating-numeric:0.1')
            row=f.store.connection.execute('SELECT work_order_ref,model_profile_ref FROM coverage_mission_research_plans WHERE plan_id=?',(admission['plan_ref'],)).fetchone()
            self.assertEqual(tuple(row),(None,None))
        again=admit_found_materials(authority=owner,registrations={registration['id']:registration},
            backlog=ResearchQuestionBacklog(f.store),mission=f.mission,state_dir=f.state,model_config=model.config)
        self.assertEqual([r['status'] for r in again],['duplicate','duplicate'],again)
        self.assertEqual(len(adapter.served),1)
        from dalton_core.directed_numeric_output import quotes,candidate_ref
        from tests.test_mission_document_research import RouteBoundCountingFakeAdapter
        from tests import test_directed_numeric_workflow as numeric_fixture
        from dalton_core.mission_document_research_executor import effective_mission_document_work_orders
        from dalton_core.operating_driver_context import project_result,CONTEXT_REF,metric_history
        records=[]
        self._completed_executors=[]
        source_support=numeric_fixture.NumericWorkflowTests('test_three_required_figures_formal_claims_and_zero_call_replay')
        for item in results:
            admission=owner.resolve_for_execution(item['admission_ref'])
            target=admission['numeric_authority']['targets'][0]
            proof=owner.registry.search(admission['request']);qid=next(iter(quotes(proof)))
            native_label=target['metric_definition']['label']
            native_scale=None
            if native:
                from dalton_core.operating_source_measurements import registered_measurements,matching_measurements
                source_proof=registered_measurements(owner.registry,registration,purpose=admission['request']['purpose'],search_proof=proof,periods=[target['period']])
                measurements=matching_measurements(source_proof,period=target['period'],terms=admission['request']['query_terms'])
                exact=next(r for r in measurements if r['unit']==target['unit'] and r['value']==('40' if target['unit']=='tons' else '12'))
                qid=exact['quote_id'];native_label=exact['native_label'];native_scale=exact['scale']
            amount='40' if target['unit']=='tons' else '12'
            draft={'schema_version':admission['numeric_authority']['contract_ref'],'targets':[{
                'target_ref':target['target_ref'],'status':'supported','reason':None,'figure':{
                    'quote_id':qid,'metric_ref':target['metric_ref'],'subject_as_named':'Synthetic issuer',
                    'as_reported_label':native_label,'value':amount,'scale':native_scale,
                    **{k:target[k] for k in ('period','unit','currency','basis')}}}]}
            verdict={'schema_version':'0.1','candidate_ref':candidate_ref(draft),'verdict':'pass',
                'targets':[{'target_ref':target['target_ref'],'verdict':'accept','reason':None}]}
            executor,producer,verifier=source_support._executor(f,owner,draft_adapter=RouteBoundCountingFakeAdapter(draft),
                verifier_adapter=RouteBoundCountingFakeAdapter(verdict))
            from dalton_core.mission_document_research_runtime import ACTOR_REF
            executor.actor_ref=ACTOR_REF
            completed=source_support.drive(executor,admission)
            self._completed_executors.append((executor,admission))
            self.assertEqual(completed['research_status'],'numeric_targets_completed')
            works=effective_mission_document_work_orders(owner,executor.scheduler,admission['id'],
                draft_worker=executor.draft_worker,verifier_worker=executor.verifier_worker)
            records.extend(project_result(executor=executor,admission=admission,works=works))
            self.assertEqual((producer.calls,verifier.calls),(1,1))
            self.assertEqual(completed,executor.run_once(admission['id']))
        body={'contract_ref':CONTEXT_REF,'company_ref':f.mission['universe'][0]['company_ref'],'records':sorted(records,key=lambda r:r['ref'])}
        context={**body,'content_hash':content_hash(body)}
        from dalton_core.company_model_inputs import build_model_inputs
        from tests.test_company_model_inputs import _spec
        drivers=[]
        for record in records:
            series=metric_history(context,company_ref=context['company_ref'],metric_ref=record['metric_ref'])
            drivers.append({'ref':'volume' if record['unit']=='tons' else 'price','label':record['metric_definition']['label'],
                'kind':'volume' if record['unit']=='tons' else 'price','basis_concept':None,
                'basis_operating_metric_ref':record['metric_ref'],'basis_operating_context_hash':context['content_hash'],
                'basis_operating_context_evidence_refs':series['context_evidence_refs'],
                'unit':series['unit'],'because':'Explicit reported quantity choice from the verified source.'})
        spec={**_spec(drivers=drivers),'company_ref':context['company_ref']}
        table=build_model_inputs(owner._missions,spec,operating_driver_context=context)
        self.assertEqual(len(table['operating_driver_lines']),2)
        self.assertEqual({r['status'] for r in table['rows'] if r['kind']=='revenue_driver'},{'verified_reported'})
        from dalton_core.forecast_financial_execution import additional_drivers
        forecast=additional_drivers(spec,table,[])
        self.assertEqual({r['unit'] for r in forecast},{'tons','usd_per_ton'})
        self.assertEqual({r['history'][0]['value'] for r in forecast},{'40000' if native else '40','12'})
        self.assertEqual(len(adapter.served),1)  # original source-gap model never repurchased
        return f,owner,model,context,spec

    def test_real_original_and_found_source_enter_normal_admission_once(self):
        self.completed_context()

    def test_raw_shared_columns_complete_normal_canonical_and_model_path(self):
        f,owner,model,context,spec=self.completed_context(native=True)
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM mission_document_operating_figure_sources').fetchone()[0],2)
        from dalton_core.operating_driver_context import OperatingDriverReadContext
        before=f.store.connection.total_changes
        with OperatingDriverReadContext(store=f.store,state_dir=f.state,model_scheduler_db=model.scheduler_db) as reader:
            self.assertEqual(reader.projection(context['company_ref']),context)
        self.assertEqual(f.store.connection.total_changes,before)

    def test_raw_column_second_company_and_append_only_owner_rollback(self):
        from unittest.mock import patch
        from tests import test_mission_annual_research as fixture
        from dalton_core.mission_document_research_executor import effective_mission_document_work_orders
        from dalton_core import operating_figure_source as source
        from dalton_core.claim_index_figures import MissionFigureAuthorityResolver
        from dalton_core.operating_driver_context import OperatingDriverReadContext
        other='company:ticker:second-raw-operating'
        with patch.object(fixture,'COMPANY',other),patch.object(numeric_fixture,'COMPANY',other):
            f,owner,model,context,spec=self.completed_context(native=True)
        self.assertEqual(context['company_ref'],other)
        executor,admission=self._completed_executors[0]
        works=effective_mission_document_work_orders(owner,executor.scheduler,admission['id'],draft_worker=executor.draft_worker,verifier_worker=executor.verifier_worker)
        figure=MissionFigureAuthorityResolver(f.store.connection).figure(context['records'][0]['figure_ref'])
        # The row chosen from sorted context may belong to the other admission.
        own=next(r for r in context['records'] if r['admission_ref']==admission['id'])
        figure=MissionFigureAuthorityResolver(f.store.connection).figure(own['figure_ref'])
        original=dict(f.store.connection.execute('SELECT * FROM '+source.TABLE+' WHERE figure_ref=?',(figure['figure_id'],)).fetchone())
        figures=[dict(r) for r in f.store.connection.execute('SELECT * FROM coverage_mission_document_figures')]
        for sql in ['UPDATE '+source.TABLE+" SET content_hash='tampered'",'DELETE FROM '+source.TABLE]:
            with self.assertRaises(Exception):f.store.connection.execute(sql)
        # Isolated fault injection removes only the private adjunct, then
        # proves a failed append rolls back without changing the Figure/fees.
        f.store.connection.execute('DROP TRIGGER operating_figure_sources_no_delete')
        with owner._transaction() as cur:cur.execute('DELETE FROM '+source.TABLE+' WHERE figure_ref=?',(figure['figure_id'],))
        proof=owner.registry.search(admission['request'])
        with patch.object(source,'read',side_effect=ValueError('injected current source refusal')):
            with self.assertRaisesRegex(ValueError,'injected'):source.record(executor,admission,works,figure,proof)
        self.assertIsNone(f.store.connection.execute('SELECT * FROM '+source.TABLE+' WHERE figure_ref=?',(figure['figure_id'],)).fetchone())
        source.record(executor,admission,works,figure,proof)
        self.assertEqual(dict(f.store.connection.execute('SELECT * FROM '+source.TABLE+' WHERE figure_ref=?',(figure['figure_id'],)).fetchone()),original)
        self.assertEqual([dict(r) for r in f.store.connection.execute('SELECT * FROM coverage_mission_document_figures')],figures)
        before=f.store.connection.total_changes
        source.record(executor,admission,works,figure,proof)
        self.assertEqual(f.store.connection.total_changes,before)
        with OperatingDriverReadContext(store=f.store,state_dir=f.state,model_scheduler_db=model.scheduler_db) as reader:
            self.assertEqual(reader.projection(other),context)
        objects=list((f.source.spool._root/'objects').glob('*/*'));body=objects[-1].read_bytes();objects[-1].write_bytes(body+b'tamper')
        try:
            with OperatingDriverReadContext(store=f.store,state_dir=f.state,model_scheduler_db=model.scheduler_db) as reader:
                with self.assertRaises(Exception):reader.projection(other)
        finally:objects[-1].write_bytes(body)
        self.revise_mission(f,owner,revoke=True)
        with OperatingDriverReadContext(store=f.store,state_dir=f.state,model_scheduler_db=model.scheduler_db) as reader:
            with self.assertRaises(Exception):reader.projection(other)

    def test_existing_owner_schema_upgrade_no_read_constructor_write(self):
        from dalton_core import operating_figure_source as source
        from dalton_core.mission_document_research import MissionDocumentResearchAuthority
        f,owner,model,call,adapter,opened,registration=self.setup_origin()
        connection=f.store.connection
        for name in ('authorized_insert','no_update','no_delete'):
            connection.execute('DROP TRIGGER operating_figure_sources_'+name)
        connection.execute('DROP TABLE '+source.TABLE)
        before=connection.total_changes
        self.assertIsNone(source.read(connection,{'figure_id':'not-an-existing-figure'}))
        self.assertEqual(connection.total_changes,before)
        # The existing schema's designated owner appends only the new table
        # and its guards; old persistent root/mission rows stay byte identical.
        tables=['scheduler_work_orders','scheduler_formal_results','coverage_missions','coverage_mission_versions']
        tables=[n for n in tables if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(n,)).fetchone()]
        old={n:[tuple(r) for r in connection.execute('SELECT * FROM '+n)] for n in tables}
        migrated=MissionDocumentResearchAuthority(f.store,registry=owner.registry,registration_resolver=owner.registration_resolver,
            model_execution_resolver=owner.model_execution_resolver,planner_scheduler_connection=owner.planner_scheduler_connection,
            planner_router_connection=owner.planner_router_connection,clock=owner.clock,model_source_plan_resolver=owner.model_source_plan_resolver)
        self.assertIsNotNone(connection.execute("SELECT name FROM sqlite_master WHERE name=?",(source.TABLE,)).fetchone())
        self.assertEqual({n:[tuple(r) for r in connection.execute('SELECT * FROM '+n)] for n in tables},old)
        with self.assertRaises(Exception):
            with migrated._transaction() as cur:
                cur.execute('INSERT INTO '+source.TABLE+' VALUES(?,?,?,?)',('missing-figure','missing-admission','{}','a'*64))
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM '+source.TABLE).fetchone()[0],0)
        from dalton_core.bootstrap import SCHEMA_DATABASES
        self.assertIn(('mission_document_research_schema.sql',None),SCHEMA_DATABASES)

    def readonly_files(self,f,owner,model):
        from dalton_core.store import canonical_json
        def write(path,body):
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(canonical_json(body));path.chmod(0o600)
        manifest=f.source.manifest
        directory=f.state/'fetches'/f.source.ticket_ref.split(':',1)[1]
        write(directory/'ticket.json',{'id':f.source.ticket_ref,'status':'succeeded','document_ref':manifest['url_ref']})
        write(directory/'summary.json',{'url_ref':manifest['url_ref'],'canonical_url':f.source.url,
            'manifest_ref':manifest['id'],'manifest_hash':manifest['content_hash'],'status':'succeeded'})
        write(directory/'manifest.json',manifest)
        write(f.state/'initial-screen-model-config.json',model.config)
        write(f.state/'document-research-config.json',{'schema_version':'document-research-config-0.1',
            'purpose':owner.registry.policy['allowed_purposes'][0],'inventory_preview_chars':600,
            'spool_dir':str(f.source.spool._root),'enabled_sources':['source:web-search'],
            'policy':owner.registry.policy,'source_reading_limits':{'alphaengine_max_document_chars':100000,
                'public_web_max_source_chars':100000,'public_web_max_pdf_pages':100,'public_web_max_decompressed_bytes':1000000}})

    def test_independent_readonly_consumer_replays_complete_canonical_chain(self):
        from dalton_core.operating_driver_context import OperatingDriverReadContext
        f,owner,model,context,spec=self.completed_context()
        self.readonly_files(f,owner,model)
        before=f.store.connection.total_changes
        with OperatingDriverReadContext(store=f.store,state_dir=f.state,model_scheduler_db=model.scheduler_db) as reader:
            self.assertEqual(reader.projection(context['company_ref']),context)
            self.assertEqual(reader.projection(context['company_ref']),context)
        self.assertEqual(before,f.store.connection.total_changes)


    def revise_mission(self,f,owner,*,revoke=False):
        old=owner._missions._mission_record(f.mission['id'])
        body={k:copy.deepcopy(old[k]) for k in ('title','objective','industry_ref','universe','research_questions','deliverables','source_plan','bindings','autonomy','budget')}
        if revoke:body['autonomy']['may_write'].remove('model_run')
        else:body['budget']['max_daily_cost_usd'] += 1
        return owner._missions.create_mission(old['mission_ref'],**body,actor_ref='human:test-owner',
            version_id=old['id']+'-revision',prior_version_ref=old['id'],idempotency_key='operating-source-mission-revision')

    def test_cross_mission_budget_revision_keeps_original_plan_and_admission(self):
        f,owner,model,call,adapter,opened,registration=self.setup_origin()
        kwargs=dict(authority=owner,registrations={registration['id']:registration},
            backlog=ResearchQuestionBacklog(f.store),state_dir=f.state,model_config=model.config)
        first=admit_found_materials(mission=f.mission,**kwargs)
        newer=self.revise_mission(f,owner)
        second=admit_found_materials(mission=newer,**kwargs)
        self.assertEqual([r['admission_ref'] for r in first],[r.get('admission_ref') for r in second],second)
        self.assertEqual([r['status'] for r in second],['duplicate','duplicate'])
        self.assertEqual(len(adapter.served),1)
        self.assertEqual(f.store.connection.execute("SELECT COUNT(*) FROM coverage_mission_research_plans WHERE json_extract(plan_json,'$.task_ref')=?",('owner:model-operating-driver-source-plan:0.1',)).fetchone()[0],2)

    def test_independent_reader_rejects_original_bytes_and_current_permission_changes(self):
        from dalton_core.operating_driver_context import OperatingDriverReadContext
        f,owner,model,context,spec=self.completed_context();self.readonly_files(f,owner,model)
        with OperatingDriverReadContext(store=f.store,state_dir=f.state,model_scheduler_db=model.scheduler_db) as reader:
            self.assertEqual(reader.projection(context['company_ref']),context)
        objects=list((f.source.spool._root/'objects').glob('*/*'))
        original=objects[-1].read_bytes();objects[-1].write_bytes(original+b'tampered')
        try:
            with OperatingDriverReadContext(store=f.store,state_dir=f.state,model_scheduler_db=model.scheduler_db) as reader:
                with self.assertRaises(Exception):reader.projection(context['company_ref'])
        finally:objects[-1].write_bytes(original)
        self.revise_mission(f,owner,revoke=True)
        with OperatingDriverReadContext(store=f.store,state_dir=f.state,model_scheduler_db=model.scheduler_db) as reader:
            with self.assertRaisesRegex(Exception,'grant|permission|model'):reader.projection(context['company_ref'])

    def test_second_company_uses_identical_source_numeric_model_path(self):
        from unittest.mock import patch
        from tests import test_mission_annual_research as mission_fixture
        other='company:ticker:second-operating'
        with patch.object(mission_fixture,'COMPANY',other),patch.object(numeric_fixture,'COMPANY',other):
            f,owner,model,context,spec=self.completed_context()
            self.assertEqual(context['company_ref'],other)
            self.readonly_files(f,owner,model)
            from dalton_core.operating_driver_context import OperatingDriverReadContext
            with OperatingDriverReadContext(store=f.store,state_dir=f.state,model_scheduler_db=model.scheduler_db) as reader:
                self.assertEqual(reader.projection(other),context)

    def test_model_choice_consumes_reported_series_without_becoming_filed(self):
        from tests.test_company_model_cli import _spec_body
        from dalton_core.company_model_spec import spec_from_response,build_prompt
        from dalton_core.company_model_inputs import build_model_inputs
        f,owner,model,context,input_spec=self.completed_context()
        state=build_company_model_state(owner._missions,context['company_ref'],ticker='TEST',operating_driver_context=context)
        raw=_spec_body();raw['revenue_drivers']=[{k:v for k,v in d.items() if k not in {'basis_operating_context_hash','basis_operating_context_evidence_refs'}} for d in input_spec['revenue_drivers']]
        raw['driver_formulas']=[{'output_ref':'revenue','operator':'product','driver_refs':[d['ref'] for d in raw['revenue_drivers']],'because':'Explicit test model economic choice; quantity times reported price.'}]
        raw['expense_lines'][0]['driver_ref']=raw['revenue_drivers'][0]['ref']
        spec=spec_from_response(state,raw,decided_by='automation:test')
        self.readonly_files(f,owner,model)
        table=build_model_inputs(owner._missions,spec)
        financial=owner._missions.validate_company_model_spec_financials(spec)
        self.assertEqual(financial['financial_inputs']['operating_driver_lines'],table['operating_driver_lines'])
        self.assertEqual(table['readiness']['rows_by_status']['verified_reported'],2)
        self.assertNotIn('basis_operating_context_hash',build_prompt(state))
        for key,value in (('unit','headcount'),('basis_operating_metric_ref','metric:foreign')):
            changed=copy.deepcopy(raw);changed['revenue_drivers'][0][key]=value
            with self.assertRaises(Exception):spec_from_response(state,changed,decided_by='automation:test')


    def test_natural_coordinator_wakes_without_paid_planner_and_stops_after_admission(self):
        from unittest.mock import patch
        from tests.test_mission_research_task_lane import FakeLauncher
        from dalton_core.mission_research_task_lane import ResearchTaskCoordinator
        from dalton_core.operating_driver_source_plan import wake_signature
        f,owner,model,call,adapter,opened,registration=self.setup_origin()
        launcher=FakeLauncher(f.state/'research-tasks');launcher.state_dir=f.state
        coordinator=ResearchTaskCoordinator(store=f.store,launcher=launcher,clock=f.harness.clock)
        with patch('dalton_core.operating_driver_source_plan.wake_signature',return_value=None):
            baseline=coordinator.dispatch_once()
            self.assertEqual(baseline['status'],'launched')
            launcher.settle(baseline['ticket_ref'],status='succeeded',summary={'status':'succeeded','admitted':0,'refused':[]})
            self.assertEqual(coordinator.dispatch_once()['status'],'held')
        first=coordinator.dispatch_once()
        self.assertEqual(first['status'],'launched',first)
        self.assertEqual(len(launcher.started),2)
        self.assertNotEqual(launcher.started[0][1],launcher.started[1][1])
        self.assertEqual(coordinator.dispatch_once()['status'],'busy')
        results=admit_found_materials(authority=owner,registrations={registration['id']:registration},
            backlog=ResearchQuestionBacklog(f.store),mission=f.mission,state_dir=f.state,model_config=model.config)
        self.assertEqual([r['status'] for r in results],['fresh','fresh'])
        self.assertIsNone(wake_signature(f.state,f.store.connection))
        launcher.settle(first['ticket_ref'],status='succeeded',summary={'status':'succeeded','admitted':2,'refused':[]})
        final=coordinator.dispatch_once()
        self.assertEqual(final['status'],'launched')  # ordinary pass observes the new admissions
        launcher.settle(final['ticket_ref'],status='succeeded',summary={'status':'succeeded','admitted':0,'refused':[]})
        self.assertEqual(coordinator.dispatch_once()['status'],'held')
        self.assertEqual(len(launcher.started),3)
        self.assertEqual(len(adapter.served),1)

    def test_historical_context_selection_ignores_new_arrivals_but_rejects_changed_records(self):
        from dalton_core.operating_driver_context import selected_context
        f,owner,model,context,spec=self.completed_context()
        bindings=[{'ref':r['ref'],'hash':r['content_hash'],'admission_ref':r['admission_ref']} for r in context['records']]
        newer=copy.deepcopy(context)
        record=copy.deepcopy(newer['records'][0]);record['ref']='operating-driver-evidence:'+'f'*32
        record['period_start']='2026-04-01';record['period_end']='2026-06-30'
        record['content_hash']=content_hash({k:v for k,v in record.items() if k!='content_hash'})
        newer['records'].append(record);newer['records'].sort(key=lambda r:r['ref'])
        newer['content_hash']=content_hash({k:v for k,v in newer.items() if k!='content_hash'})
        self.assertEqual(selected_context(newer,bindings,context['content_hash']),context)
        changed=copy.deepcopy(newer);changed['records'][0]['value']='41'
        changed['records'][0]['content_hash']=content_hash({k:v for k,v in changed['records'][0].items() if k!='content_hash'})
        changed['content_hash']=content_hash({k:v for k,v in changed.items() if k!='content_hash'})
        with self.assertRaises(ValueError):selected_context(changed,bindings,context['content_hash'])


    def test_twelve_requests_cross_bounded_ticks_keep_paid_input_after_source_change(self):
        from unittest.mock import patch
        f,owner,model,call,adapter,opened,registration=self.setup_origin(twelve=True)
        self.assertEqual(len(opened),12)
        kwargs=dict(authority=owner,registrations={registration['id']:registration},
            backlog=ResearchQuestionBacklog(f.store),mission=f.mission,state_dir=f.state,model_config=model.config)
        first=admit_found_materials(**kwargs)
        self.assertEqual([r['status'] for r in first],['fresh','fresh'],first)
        from dalton_core.directed_numeric_output import quotes,candidate_ref
        from tests.test_mission_document_research import RouteBoundCountingFakeAdapter
        from dalton_core.mission_document_research_executor import effective_mission_document_work_orders
        from dalton_core.operating_driver_context import project_result
        records=[]
        for item in first:
            admission=owner.resolve_for_execution(item['admission_ref']);target=admission['numeric_authority']['targets'][0]
            proof=owner.registry.search(admission['request'])
            qid=next(q for q,text in quotes(proof).items() if target['period'] in text and target['metric_definition']['label'] in text)
            amount={'tons':'40','currency_per_ton':'12','currency_per_mmbtu':'3','percent':'82'}[target['unit']]
            draft={'schema_version':admission['numeric_authority']['contract_ref'],'targets':[{'target_ref':target['target_ref'],'status':'supported','reason':None,
                'figure':{'quote_id':qid,'metric_ref':target['metric_ref'],'subject_as_named':'Synthetic issuer','as_reported_label':target['metric_definition']['label'],
                          'value':amount,'scale':None,**{k:target[k] for k in ('period','unit','currency','basis')}}}]}
            verdict={'schema_version':'0.1','candidate_ref':candidate_ref(draft),'verdict':'pass','targets':[{'target_ref':target['target_ref'],'verdict':'accept','reason':None}]}
            fixture=numeric_fixture.NumericWorkflowTests('runTest')
            executor,producer,verifier=fixture._executor(f,owner,draft_adapter=RouteBoundCountingFakeAdapter(draft),verifier_adapter=RouteBoundCountingFakeAdapter(verdict))
            from dalton_core.mission_document_research_runtime import ACTOR_REF
            executor.actor_ref=ACTOR_REF
            completed=fixture.drive(executor,admission)
            self.assertEqual(completed['research_status'],'numeric_targets_completed')
            works=effective_mission_document_work_orders(owner,executor.scheduler,admission['id'],draft_worker=executor.draft_worker,verifier_worker=executor.verifier_worker)
            records.extend(project_result(executor=executor,admission=admission,works=works))
            self.assertEqual((producer.calls,verifier.calls),(1,1))
        self.assertEqual(len(records),2)
        # The first two actual Claims now alter current operating inputs. The
        # following ten retain the original complete paid root frame.
        with patch('dalton_core.company_model_state.build_company_model_state',side_effect=AssertionError('must reuse the frozen paid frame')):
            later=[]
            for _ in range(5):later.extend(admit_found_materials(**kwargs))
        self.assertEqual(len({r['admission_ref'] for r in [*first,*later] if r['status']=='fresh'}),12,later)
        self.assertEqual(len(adapter.served),1)
        self.assertEqual([r['status'] for r in admit_found_materials(**kwargs)],['duplicate','duplicate'])

if __name__=='__main__': unittest.main()
