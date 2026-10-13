import copy,json,unittest,sqlite3
from unittest.mock import patch
from datetime import timedelta
from dalton_core import company_model_cli as cli, model_spec_maintenance_repair as maintenance
from dalton_core.company_model_spec import CompanyModelSpecError
from dalton_core.planner_paid_output_recovery import _authority_connection
from dalton_core.model_spec_source_sign_projection import read
from dalton_core.store import content_hash
from dalton_core.company_model_cli import model_spec_request_id,model_spec_request_identity,build_prompt
from dalton_core.model_spec_source_sign_projection import derive,project,TABLE
from tests import test_cockpit_model_fallback as runtime
from tests.test_model_spec_maintenance_repair import Missions
from tests.test_model_spec_signed_expense_repair import case,callback,SIGNED_CONCEPT


class SourceSignProjectionTests(unittest.TestCase):
    def fixture(self, company='company:synthetic:projection-a', *, maintenance_chain=False, governed=None):
        owner=runtime.CockpitChainTests('runTest');owner.setUp();self.addCleanup(owner.doCleanups)
        state,reply,inputs=case()
        state['company_ref']=inputs['company_ref']=company
        if maintenance_chain:state['industry_classification']='generic'
        if governed is not None:
            state['numeric_context']={'content_hash':'1'*64,'policy':{},'included_cells':0,
                'omitted_by_series_limit':0,'omitted_by_total_limit':0,'omitted_by_prompt_limit':0,
                'prompt_byte_limit':128000,'prompt_bytes':12000}
        state['state_hash']=content_hash({k:v for k,v in state.items() if k!='state_hash'})
        owner.mission.update(universe=[{'company_ref':company}],deliverables=['company_model'],
            autonomy={'may_write':['model_run'],'automation_principal':'automation:test'},
            bindings={'playbook_version':{},'constitution_version':{},'mandate_version':{}},industry_ref='industry:test')
        owner.mission['content_hash']=content_hash({k:v for k,v in owner.mission.items() if k!='content_hash'})
        if governed is not None:owner.mission=governed.missions._mission_record(governed.mission['id'])
        raw_replies=[reply]
        if maintenance_chain:
            bad=copy.deepcopy(reply)
            for i in (0,1):bad['expense_lines'][i].update(cost_driver_slot='fixed_cost',cost_driver_unbound_reason='No disclosed unit basis.')
            partial=copy.deepcopy(bad);partial['expense_lines'][0]['cost_driver_slot']=None
            reply=copy.deepcopy(bad)
            for i in (0,1):reply['expense_lines'][i]['cost_driver_slot']=None
            raw_replies=[bad,partial,reply]
        class Adapter(runtime.ChainAdapter):
            def execute(self,work,route,profile):
                from dataclasses import replace
                inv,result=super().execute(work,route,profile)
                return inv,replace(result,outputs={'text':json.dumps(raw_replies.pop(0))})
        adapter=Adapter({});model=owner._model(adapter,policy_version_ref=owner.pinned_policy,model_spec_output_tokens=6000)
        call=model.call(purpose='model_spec',request_id=model_spec_request_id(state['state_hash'],repair_config={'max_attempts':1}),
            prompt=build_prompt(state),mission=owner.mission,
            _model_spec_request_identity=model_spec_request_identity(state['state_hash'],repair_config={'max_attempts':1}))
        if maintenance_chain:
            from tests.test_model_spec_complete_cost_pairs import HISTORICAL
            from dalton_core import model_spec_typed_field_repair as pairs
            old_contract=copy.deepcopy(HISTORICAL)
            with patch.multiple(cli,REPAIR_CONTRACT=old_contract,REPAIR_CONTRACT_REF=old_contract['ref'],
                    REPAIR_CONTRACT_HASH=content_hash(old_contract)),patch.object(pairs,'cost_slot_pair_scope',pairs.legacy_cost_slot_pair_scope):
                with self.assertRaises(CompanyModelSpecError):
                    cli._validated_spec_with_repair(model=model,state=state,mission=owner.mission,
                        original_call=call,repair_config={'max_attempts':1},decided_by='automation:test')
        missions=governed.missions if governed is not None else Missions(owner.root/'projection-core.sqlite',owner.mission)
        if governed is None:
            missions.old_mission=owner.mission;self.addCleanup(missions.connection.close)
        validations=[]
        def validate(spec,**kwargs):return callback(inputs,validations)(spec)
        if governed is None:missions.validate_company_model_spec_financials=validate
        return owner,model,adapter,missions,state,reply,inputs,call

    def test_two_companies_owner_record_then_free_restart_preserves_original_call(self):
        for company in ('company:synthetic:projection-a','company:synthetic:projection-b'):
            owner,model,adapter,missions,state,reply,inputs,call=self.fixture(company)
            original=copy.deepcopy(call);paid=len(adapter.served)
            corrected,record=project(missions,model,state,owner.mission,call)
            self.assertEqual(record['signed_proof']['required_coefficient'],'1')
            self.assertEqual(len(record['signed_proof']['periods']),4)
            replay,again=project(missions,model,state,owner.mission,call)
            self.assertEqual((replay,again),(corrected,record))
            self.assertEqual(call,original);self.assertEqual(len(adapter.served),paid)
            self.assertEqual(missions.connection.execute('SELECT COUNT(*) FROM '+TABLE).fetchone()[0],1)

    def test_ambiguous_incomplete_or_changed_source_never_records_or_calls(self):
        for kind in ('source_value','source_unit','history_gap'):
            owner,model,adapter,missions,state,reply,inputs,call=self.fixture()
            source=next(r for r in inputs['filed_lines'] if r['concept']==SIGNED_CONCEPT)
            if kind=='source_value':
                for cell in source['cells'].values():cell['value']='10'
            elif kind=='source_unit':
                for cell in source['cells'].values():cell['unit']='shares'
            else:source['cells']={}
            count=len(adapter.served)
            with self.assertRaises(Exception):project(missions,model,state,owner.mission,call)
            self.assertEqual(len(adapter.served),count)
            self.assertIsNone(missions.connection.execute("SELECT 1 FROM sqlite_master WHERE name=?",(TABLE,)).fetchone())

    def test_permission_withdrawal_raw_reply_or_result_hash_drift_refused(self):
        owner,model,adapter,missions,state,reply,inputs,call=self.fixture()
        for field in ('text','work_order_hash','result_envelope_hash'):
            altered=copy.deepcopy(call);altered[field]+=' forged'
            with self.assertRaises(ValueError):derive(missions,model,state,owner.mission,altered)
        revoked=copy.deepcopy(owner.mission);revoked['autonomy']['may_write']=[]
        missions.mission=revoked
        with self.assertRaises(ValueError):project(missions,model,state,revoked,call)
        self.assertEqual(len(adapter.served),1)

    def test_used_maintenance_pair_answer_projects_then_restarts_without_another_slot(self):
        for company in ('company:synthetic:projection-c','company:synthetic:projection-d'):
            owner,model,adapter,missions,state,reply,inputs,root=self.fixture(company,maintenance_chain=True)
            authority=maintenance.MaintenanceAuthority(missions,model,state,owner.mission)
            with _authority_connection(model.scheduler_db) as c:candidate=authority.find(c)
            self.assertIsNotNone(candidate)
            from dalton_core.model_spec_signed_expense_repair import SignedExpenseRepairError
            # The paid answer is still source-invalid; no raw or owner record
            # is corrected by the normal model result itself.
            with self.assertRaises(SignedExpenseRepairError):
                maintenance.execute(authority,candidate,decided_by='automation:test',repairs=[],
                    financial_validator=lambda spec:missions.validate_company_model_spec_financials(spec))
            self.assertEqual(len(adapter.served),3)
            records=[tuple(r) for r in missions.connection.execute('SELECT * FROM '+maintenance.CLAIMS)]
            bills=copy.deepcopy(candidate[0]['paid_prefix'])
            def project_answer(call,spec,error):return project(missions,model,state,owner.mission,call)[0]
            corrected,call=maintenance.execute(authority,candidate,decided_by='automation:test',repairs=[],
                financial_validator=lambda spec:missions.validate_company_model_spec_financials(spec),
                source_sign_projection=project_answer)
            self.assertEqual(call['text'],json.dumps(reply))
            self.assertEqual(corrected['financial_statement_structure']['formulas'][0]['terms'][-1]['coefficient'],'1')
            model.clock=lambda:runtime.NOW+timedelta(days=1)
            model.config['purpose_call_budgets']['model_spec']['max_output_tokens']=7000
            replay=maintenance.execute(authority,candidate,decided_by='automation:test',repairs=[],
                financial_validator=lambda spec:missions.validate_company_model_spec_financials(spec),
                source_sign_projection=project_answer)
            self.assertEqual(replay,(corrected,call));self.assertEqual(len(adapter.served),3)
            self.assertEqual(records,[tuple(r) for r in missions.connection.execute('SELECT * FROM '+maintenance.CLAIMS)])
            self.assertEqual(candidate[0]['paid_prefix'],bills)
            self.assertEqual(missions.connection.execute('SELECT COUNT(*) FROM '+TABLE).fetchone()[0],1)

    def test_normal_validation_callback_projects_without_a_paid_repair(self):
        owner,model,adapter,missions,state,reply,inputs,call=self.fixture()
        corrected,repairs,accepted=cli._validated_spec_with_repair(model=model,state=state,mission=owner.mission,
            original_call=call,repair_config={'max_attempts':0},decided_by='automation:test',
            financial_validator=lambda spec:missions.validate_company_model_spec_financials(spec),
            source_sign_projection=lambda source,spec,error:project(missions,model,state,owner.mission,source)[0])
        self.assertEqual(repairs,[]);self.assertEqual(accepted,call);self.assertEqual(len(adapter.served),1)
        self.assertEqual(corrected['financial_statement_structure']['formulas'][0]['terms'][-1]['coefficient'],'1')
        self.assertIn('source_sign_projection',cli.model_spec_validation_contract())

    def test_owner_record_tamper_and_permission_source_change_rejected_on_free_read(self):
        owner,model,adapter,missions,state,reply,inputs,call=self.fixture()
        corrected,record=project(missions,model,state,owner.mission,call)
        self.assertEqual(read(missions,model,state,owner.mission,call),(corrected,record))
        original=copy.deepcopy(inputs)
        next(r for r in inputs['filed_lines'] if r['concept']==SIGNED_CONCEPT)['cells'].clear()
        with self.assertRaises(Exception):read(missions,model,state,owner.mission,call)
        inputs.clear();inputs.update(original)
        revoked=copy.deepcopy(owner.mission);revoked['autonomy']['may_write']=[];missions.mission=revoked
        with self.assertRaises(ValueError):read(missions,model,state,revoked,call)
        missions.mission=owner.mission
        missions.connection.execute('DROP TRIGGER model_source_sign_projection_no_update')
        forged=copy.deepcopy(record);forged['projected_spec_hash']='f'*64
        forged['content_hash']=content_hash({k:v for k,v in forged.items() if k!='content_hash'})
        from dalton_core.store import canonical_json
        missions.connection.execute('UPDATE '+TABLE+' SET record_json=?,content_hash=?',
            (canonical_json(forged),forged['content_hash']))
        with self.assertRaisesRegex(ValueError,'owner record differs|matches original authority'):
            read(missions,model,state,owner.mission,call)
        self.assertEqual(len(adapter.served),1)

    def test_unknown_or_unsettled_attempt_cannot_gain_owner_projection(self):
        for mode in ('unknown','unsettled'):
            owner,model,adapter,missions,state,reply,inputs,call=self.fixture()
            if mode=='unknown':
                with sqlite3.connect(model.scheduler_db) as c:
                    c.row_factory=sqlite3.Row
                    for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='scheduler_result_envelopes'").fetchall():c.execute('DROP TRIGGER '+r['name'])
                    rr=c.execute('SELECT * FROM scheduler_result_envelopes WHERE work_order_id=?',(call['work_order_ref'],)).fetchone()
                    wire=json.loads(rr['result_envelope_json']);wire['metadata']['dispatch_state']='unknown'
                    h=content_hash(wire)
                    receipt={k:rr[k] for k in ('result_envelope_id','work_order_id','attempt_number','result_envelope_hash','outcome','created_at')}
                    receipt['result_envelope_hash']=h
                    from dalton_core.store import canonical_json
                    c.execute('UPDATE scheduler_result_envelopes SET result_envelope_json=?,result_envelope_hash=?,content_hash=? WHERE result_envelope_id=?',
                        (canonical_json(wire),h,content_hash(receipt),rr['result_envelope_id']))
            else:
                with sqlite3.connect(model.config['budget_db']) as c:
                    for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='thesis_impact_day_settlements'").fetchall():c.execute('DROP TRIGGER '+r[0])
                    c.execute('DELETE FROM thesis_impact_day_settlements')
            with self.assertRaises(ValueError):project(missions,model,state,owner.mission,call)
            self.assertIsNone(missions.connection.execute("SELECT 1 FROM sqlite_master WHERE name=?",(TABLE,)).fetchone())
            self.assertEqual(len(adapter.served),1)

    def test_owner_cas_rechecks_source_change_and_rolls_back_append(self):
        owner,model,adapter,missions,state,reply,inputs,call=self.fixture()
        from contextlib import contextmanager
        original=missions._transaction
        @contextmanager
        def changed_before_cas():
            next(r for r in inputs['filed_lines'] if r['concept']==SIGNED_CONCEPT)['cells'].clear()
            with original() as cursor:yield cursor
        missions._transaction=changed_before_cas
        with self.assertRaises(Exception):project(missions,model,state,owner.mission,call)
        self.assertEqual(missions.connection.execute('SELECT COUNT(*) FROM '+TABLE).fetchone()[0],0)
        self.assertEqual(len(adapter.served),1)

    def test_normal_cli_records_projection_and_spec_with_original_maintenance_work(self):
        from tests.test_company_model_cli import ChooseCompanyTests,ACN
        from dalton_core.coverage_mission import CoverageMissionAuthority,company_model_spec_id
        from dalton_core.scheduler import Scheduler
        governed=ChooseCompanyTests('runTest');governed.setUp();self.addCleanup(governed.doCleanups)
        owner,model,adapter,missions,state,reply,inputs,root=self.fixture(ACN,maintenance_chain=True,governed=governed)
        # The test supplies strong synthetic filed input rows at the financial
        # input seam; parsing, all history materialization, owner CAS and the
        # normal specification persistence are real.
        def financial(owner,candidate,**kwargs):
            structure,replay=callback(inputs,[])(candidate)
            return {'spec_id':company_model_spec_id(candidate['company_ref'],candidate['state_hash'],candidate['task_hash']),
                'financial_input_hash':content_hash(inputs),'financial_structure':structure,'financial_replay':replay}
        config=governed.state_dir/'source-sign-model.json';config.write_text(json.dumps(model.config))
        scheduler=Scheduler(model.scheduler_db);self.addCleanup(scheduler.close)
        with patch.object(cli,'CockpitModel',return_value=model),patch.object(cli,'choose_company',return_value=(ACN,state)), \
                patch('dalton_core.statement_original_revenue_context.prepare_model_revenue_inputs',return_value={}), \
                patch('dalton_core.sec_filing_units.prepare_model_unit_inputs',return_value={'unresolved':[],'refreshed':False}), \
                patch.object(CoverageMissionAuthority,'validate_company_model_spec_financials',financial):
            out=cli.run_model_spec(state_dir=governed.state_dir,model_config_path=config,scheduler_db=model.scheduler_db,
                summary_dir=governed.state_dir/'source-sign-run',company_ref=ACN)
            self.assertEqual(out['spec_status'],'fresh');self.assertIn('source_sign_projection',out)
            stored=missions.latest_company_model_spec(ACN)
            self.assertEqual(stored['content_hash'],out['source_sign_projection']['projected_spec_hash'])
            self.assertEqual(stored['work_order_ref'],out['source_sign_projection']['source_call']['work_order_ref'])
            self.assertEqual(len(adapter.served),3)
            rows=[tuple(r) for r in missions.connection.execute('SELECT * FROM '+TABLE)]
            again=cli.run_model_spec(state_dir=governed.state_dir,model_config_path=config,scheduler_db=model.scheduler_db,
                summary_dir=governed.state_dir/'source-sign-restart',company_ref=ACN)
            self.assertEqual(again['spec_status'],'duplicate');self.assertEqual(len(adapter.served),3)
            self.assertEqual(rows,[tuple(r) for r in missions.connection.execute('SELECT * FROM '+TABLE)])

    def test_later_unproved_repair_cannot_be_bypassed_by_earlier_success(self):
        owner,model,adapter,missions,state,reply,inputs,call=self.fixture()
        from dalton_core.scheduler import Scheduler
        from dalton_core.model_spec_maintenance_repair import _proof
        with _authority_connection(model.scheduler_db) as c:
            original=json.loads(c.execute('SELECT work_order_json FROM scheduler_work_orders WHERE work_order_id=?',
                (call['work_order_ref'],)).fetchone()[0])
        later=copy.deepcopy(original);later['id']='work:cockpit-model_spec-later-unproved'
        later['idempotency_key']='source-sign-test-later-unproved'
        later['metadata']['structured_output_repair']={'root_original':_proof(call),'repair_parent':_proof(call)}
        with Scheduler(model.scheduler_db) as scheduler:
            scheduler.enqueue(later)
            self.assertIsNotNone(scheduler.work_order_authority(later['id']))
        with self.assertRaisesRegex(ValueError,'complete latest repair chain'):
            project(missions,model,state,owner.mission,call)
        self.assertEqual(len(adapter.served),1)
        self.assertIsNone(missions.connection.execute("SELECT 1 FROM sqlite_master WHERE name=?",(TABLE,)).fetchone())

    def test_ambiguous_sign_candidates_are_not_projected(self):
        owner,model,adapter,missions,state,reply,inputs,call=self.fixture()
        # A changed held tie cannot make two identical financial paths a
        # unique source decision; ambiguity remains an ordinary refusal.
        from dalton_core.company_model_spec import spec_from_response
        from dalton_core.model_spec_signed_expense_repair import repair_proof
        spec=spec_from_response(state,call['text'],decided_by='automation:test')
        twin=copy.deepcopy(spec['financial_statement_structure']['lines'][-1]);twin['ref']='other-signed-path'
        spec['financial_statement_structure']['lines'].append(twin)
        spec['financial_statement_structure']['formulas'][0]['terms'].append({'line_ref':twin['ref'],'coefficient':'-1'})
        for cell in next(r for r in inputs['filed_lines'] if r['concept']=='us-gaap:OperatingIncomeLoss')['cells'].values():cell['value']='200'
        self.assertIsNone(repair_proof(spec,inputs,'formula operating does not tie to filed history'))
        self.assertEqual(len(adapter.served),1)
