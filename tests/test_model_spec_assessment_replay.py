"""The old paid task is reused only for its one explicit length widening."""
import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core import company_model_cli as cli, company_model_spec as specs
from dalton_core.cockpit_model import build_work
from dalton_core.model_spec_assessment_replay import find_parent, validate_parent, AssessmentReplayError
from dalton_core.store import canonical_json, content_hash
from tests import test_model_spec_cash_flow_base_repair as cash
from tests import test_company_model_cli as cli_fixture


class AssessmentReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name)/'scheduler.sqlite'
        self.connection = sqlite3.connect(self.db)
        self.addCleanup(self.connection.close)
        self.connection.executescript('''
          CREATE TABLE scheduler_work_orders(work_order_id TEXT PRIMARY KEY,work_order_json TEXT,work_order_hash TEXT,created_at TEXT);
          CREATE TABLE scheduler_formal_results(work_order_id TEXT PRIMARY KEY,result_envelope_id TEXT,result_envelope_json TEXT,result_envelope_hash TEXT,terminal_state TEXT,result_record_id TEXT,attempt_number INTEGER,content_hash TEXT,created_at TEXT);
        ''')
        self.state,self.body = cash.proposal()
        self.body['assessment']='A detailed source-grounded assessment. '*39
        self.state['state_hash']=content_hash({k:v for k,v in self.state.items()if k!='state_hash'})
        self.mission={'id':'mission:one','content_hash':'b'*64}

    def store(self, work, body):
        wire=work.to_dict();text=json.dumps(body)
        envelope={'schema_version':'0.1','created_at':wire['created_at'],
                  'artifact_refs':[],'actual_side_effects':[],'usage_refs':[],
                  'id':'result:'+work.id,'status':'succeeded','work_order_ref':work.id,
                  'invocation_ref':'invocation:'+work.id,'outputs':{'text':text},
                  'metadata':{'route_decision_ref':'route:'+work.id}}
        self.connection.execute('INSERT INTO scheduler_work_orders VALUES(?,?,?,?)',
                                (work.id,canonical_json(wire),content_hash(wire),wire['created_at']))
        record={'id':'formal:'+work.id,'work_order_id':work.id,'attempt_number':1,
                'result_envelope_id':envelope['id'],'result_envelope_hash':content_hash(envelope),
                'terminal_state':'succeeded','created_at':wire['created_at']}
        self.connection.execute('INSERT INTO scheduler_formal_results VALUES(?,?,?,?,?,?,?,?,?)',
            (work.id,envelope['id'],canonical_json(envelope),content_hash(envelope),'succeeded',
             record['id'],1,content_hash(record),wire['created_at']))
        self.connection.commit()
        return wire,envelope

    def root(self, *, state=None, body=None, mission=None, suffix=""):
        state=self.state if state is None else state;body=self.body if body is None else body
        mission=self.mission if mission is None else mission
        identity={'schema_version':'company-model-spec-request-0.1','state_hash':state['state_hash'],
                  'task_hash':specs.PRIOR_ASSESSMENT_TASK_HASH,'structured_output_repair':{'max_attempts':1}}
        prompt=specs.build_prompt(state,output_schema=specs.prior_assessment_output_schema())
        work=build_work(purpose='model_spec',request_id=content_hash(identity)[:32]+suffix,prompt=prompt,
            mission_version_ref=mission['id'],mission_version_hash=mission['content_hash'],
            max_input_tokens=250000,max_output_tokens=32768,max_cost_usd=14,max_seconds=300,
            model_spec_request_identity=identity)
        return self.store(work,body)

    def repair(self, parent, *, body=None, mutate_binding=None, mutate_prompt=False):
        work,formal=parent;call={'work_order_ref':work['id'],'work_order_hash':content_hash(work),
            'result_envelope_ref':formal['id'],'result_envelope_hash':content_hash(formal),
            'invocation_ref':formal['invocation_ref'],'route_decision_ref':formal['metadata']['route_decision_ref']}
        text=formal['outputs']['text'];error=specs.CompanyModelSpecError(cash.MESSAGE,code=cash.CODE)
        prompt=cli._repair_prompt(text,error,output_schema=specs.prior_assessment_output_schema())
        binding={'schema_version':'company-model-spec-repair-binding-0.1','root_original':call,'repair_parent':call,
            'original_text_sha256':hashlib.sha256(text.encode()).hexdigest(),
            'parent_text_sha256':hashlib.sha256(text.encode()).hexdigest(),'state_hash':self.state['state_hash'],
            'task_hash':specs.PRIOR_ASSESSMENT_TASK_HASH,'validation_error':{'code':cash.CODE,'message':cash.MESSAGE},
            'repair_contract_ref':cli.REPAIR_CONTRACT_REF,'repair_contract_hash':cli.REPAIR_CONTRACT_HASH,
            'repair_prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),
            'repair_config':{'max_attempts':1},'repair_number':1}
        if mutate_binding:mutate_binding(binding)
        if mutate_prompt:prompt+='\nDifferent question';binding['repair_prompt_sha256']=hashlib.sha256(prompt.encode()).hexdigest()
        repair=build_work(purpose='model_spec',request_id='model-spec-repair:'+content_hash(binding)[:32],
            prompt=prompt,mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'],
            max_input_tokens=250000,max_output_tokens=32768,max_cost_usd=14,max_seconds=300,
            structured_output_repair=binding)
        return self.store(repair,cash.unavailable(self.body)if body is None else body)

    def test_schema_only_change_is_exact_and_old_prompt_is_reconstructible(self):
        contract=specs.assessment_compatibility_contract()
        prior=specs.prior_assessment_output_schema();expected=copy.deepcopy(prior)
        expected['properties']['assessment']['maxLength']=2400
        self.assertEqual(expected,specs.OUTPUT_SCHEMA)
        self.assertEqual(contract['prior_task_hash'],'497833a7b632a3e195fc16c60c20c99315c5ba5630d2292b33feee894cc3b0ea')
        self.assertNotEqual(contract['prior_task_hash'],specs.TASK_HASH)
        with patch.dict(specs.OUTPUT_SCHEMA['properties']['assessment'], {'minLength':2}):
            with self.assertRaisesRegex(specs.CompanyModelSpecError,'prior task contract drifted'):
                specs.assessment_compatibility_contract()

    def test_paid_cash_repair_is_free_under_disabled_new_repair_policy(self):
        root=self.root();repair=self.repair(root);before=list(self.connection.iterdump())
        parent=find_parent(self.db,state=self.state,mission=self.mission)
        validation=[]
        spec,attempts,call,proof=validate_parent(parent,state=self.state,mission=self.mission,
            decided_by='automation:test',financial_validator=lambda result:validation.append(result))
        self.assertEqual(spec['assessment'],self.body['assessment'].strip())
        self.assertEqual(spec['task_hash'],specs.TASK_HASH)
        self.assertEqual(call['work_order_ref'],repair[0]['id'])
        self.assertEqual(attempts[0]['cost_micros'],0)
        self.assertEqual(len(validation),1)
        self.assertEqual(proof['root_authority']['work_order_ref'],root[0]['id'])
        self.assertEqual(list(self.connection.iterdump()),before)
        # API has no model/provider argument or recovery policy to enable;
        # actual CLI max_attempts=0 is separately exercised below.

    def test_semantic_failure_after_matched_repair_refuses_without_more_repairs(self):
        self.repair(self.root());parent=find_parent(self.db,state=self.state,mission=self.mission);attempts=[]
        def refuse(_spec):raise specs.CompanyModelSpecError('next financial semantic failure')
        with self.assertRaisesRegex(specs.CompanyModelSpecError,'next financial semantic failure'):
            validate_parent(parent,state=self.state,mission=self.mission,decided_by='automation:test',
                            financial_validator=refuse,repair_attempts=attempts)
        self.assertEqual(len(attempts),1);self.assertEqual(attempts[0]['cost_micros'],0)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM scheduler_work_orders').fetchone()[0],2)

    def test_over_2400_remains_refused_without_repair(self):
        self.body['assessment']='x'*2401;self.root(body=cash.unavailable(self.body))
        parent=find_parent(self.db,state=self.state,mission=self.mission)
        with self.assertRaisesRegex(specs.CompanyModelSpecError,'longer than 2400'):
            validate_parent(parent,state=self.state,mission=self.mission,decided_by='automation:test')
        self.assertEqual(self.connection.execute('SELECT count(*) FROM scheduler_work_orders').fetchone()[0],1)

    def test_wrong_state_mission_company_prompt_and_formal_hash_are_rejected(self):
        root,_=self.root()
        wrong=copy.deepcopy(self.state);wrong['company_ref']='company:foreign';wrong['state_hash']=content_hash({k:v for k,v in wrong.items()if k!='state_hash'})
        self.assertIsNone(find_parent(self.db,state=wrong,mission=self.mission))
        invalid=copy.deepcopy(self.state);invalid['ticker']='FOREIGN'
        with self.assertRaisesRegex(AssessmentReplayError,'state hash differs'):find_parent(self.db,state=invalid,mission=self.mission)
        for mission in ({'id':'mission:foreign','content_hash':'b'*64},{'id':self.mission['id'],'content_hash':'c'*64}):
            with self.assertRaises(AssessmentReplayError):find_parent(self.db,state=self.state,mission=mission)
        root['question']+='\nwrong prompt'
        self.connection.execute('UPDATE scheduler_work_orders SET work_order_json=?,work_order_hash=?',
                                (canonical_json(root),content_hash(root)));self.connection.commit()
        with self.assertRaisesRegex(AssessmentReplayError,'prompt/task binding differs'):find_parent(self.db,state=self.state,mission=self.mission)
        self.connection.execute("UPDATE scheduler_formal_results SET result_envelope_hash=?",('0'*64,));self.connection.commit()
        with self.assertRaisesRegex(AssessmentReplayError,'Work/formal authority'):find_parent(self.db,state=self.state,mission=self.mission)

    def test_missing_formal_or_exact_paid_cash_repair_refuses(self):
        self.root();parent=find_parent(self.db,state=self.state,mission=self.mission)
        with self.assertRaisesRegex(AssessmentReplayError,'cash repair is unavailable'):
            validate_parent(parent,state=self.state,mission=self.mission,decided_by='automation:test')
        self.connection.execute('DELETE FROM scheduler_formal_results');self.connection.commit()
        self.assertIsNone(find_parent(self.db,state=self.state,mission=self.mission))

    def test_latest_inflight_or_failed_root_does_not_borrow_older_success(self):
        self.root(suffix=":old")
        latest,_=self.root(suffix=":latest")
        self.connection.execute("UPDATE scheduler_work_orders SET created_at='2099-01-01T00:00:00Z' WHERE work_order_id=?",(latest['id'],))
        self.connection.execute('DELETE FROM scheduler_formal_results WHERE work_order_id=?',(latest['id'],));self.connection.commit()
        self.assertIsNone(find_parent(self.db,state=self.state,mission=self.mission))
        # A non-success terminal row is not a compatible answer either. The
        # normal new-task path, rather than this helper, owns its admission.
        self.connection.execute("INSERT INTO scheduler_formal_results(work_order_id,terminal_state) VALUES(?,'failed')",(latest['id'],));self.connection.commit()
        self.assertIsNone(find_parent(self.db,state=self.state,mission=self.mission))

    def test_full_formal_record_or_envelope_shape_drift_is_rejected(self):
        self.root()
        self.connection.execute('UPDATE scheduler_formal_results SET attempt_number=2');self.connection.commit()
        with self.assertRaisesRegex(AssessmentReplayError,'Work/formal authority'):
            find_parent(self.db,state=self.state,mission=self.mission)
        self.connection.execute('UPDATE scheduler_formal_results SET attempt_number=1');self.connection.commit()
        row=self.connection.execute('SELECT result_envelope_json FROM scheduler_formal_results').fetchone()
        body=json.loads(row[0]);body['foreign_field']='not in ResultEnvelope'
        self.connection.execute('UPDATE scheduler_formal_results SET result_envelope_json=?,result_envelope_hash=?',
                                (canonical_json(body),content_hash(body)));self.connection.commit()
        with self.assertRaisesRegex(AssessmentReplayError,'Work/formal authority'):
            find_parent(self.db,state=self.state,mission=self.mission)

    def test_changed_numbers_formulas_prompt_and_parent_binding_never_pass(self):
        for change in ('formula','source','assessment','prompt','parent'):
            with self.subTest(change=change):
                self.connection.execute('DELETE FROM scheduler_formal_results');self.connection.execute('DELETE FROM scheduler_work_orders');self.connection.commit()
                root=self.root();fixed=cash.unavailable(self.body)
                if change=='formula':fixed['cash_flow_companion']['formula']['terms'][0]['coefficient']='2'
                if change=='source':fixed['cash_flow_companion']['lines'][1]['concept']='foreign:capex'
                if change=='assessment':fixed['assessment']+='extra'
                self.repair(root,body=fixed,mutate_prompt=change=='prompt',
                            mutate_binding=(lambda b:b.update(parent_text_sha256='0'*64))if change=='parent'else None)
                parent=find_parent(self.db,state=self.state,mission=self.mission)
                with self.assertRaises(specs.CompanyModelSpecError):
                    validate_parent(parent,state=self.state,mission=self.mission,decided_by='automation:test')


class AssessmentReplayCliTests(unittest.TestCase):
    def test_real_cli_disabled_repair_reads_old_paid_answer_without_model_call(self):
        fixture=cli_fixture.ChooseCompanyTests(methodName='runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
        _,state=cli.choose_company(fixture.missions,fixture.mission)
        records=AssessmentReplayTests(methodName='runTest');records.setUp();self.addCleanup(records.doCleanups)
        records.state=state;records.mission=fixture.mission;body=cli_fixture._spec_body();body['assessment']='x'*1407
        records.root(body=body)
        config=fixture.state_dir/'model.json';config.write_text(json.dumps({'structured_output_repair':{'max_attempts':0}}))
        class NoProvider:
            def __init__(self,config,**kwargs):self.config=config;self.scheduler_db=str(records.db)
            def call(self,**_):raise AssertionError('matched held answer must not call provider')
        with patch('dalton_core.company_model_cli.CockpitModel',NoProvider):
            result=cli.run_model_spec(state_dir=fixture.state_dir,model_config_path=config,
                summary_dir=fixture.state_dir/'summary',scheduler_db=records.db,company_ref=cli_fixture.ACN)
        self.assertEqual(result['spec_status'],'fresh',result)
        self.assertTrue(result['replayed']);self.assertEqual(result['cost_micros'],0)
        self.assertEqual(result['assessment_compatible_replay']['prior_task_hash'],specs.PRIOR_ASSESSMENT_TASK_HASH)
        self.assertEqual(records.connection.execute('SELECT count(*) FROM scheduler_work_orders').fetchone()[0],1)

    def test_real_cli_matched_answer_new_semantic_failure_never_calls_repair(self):
        fixture=cli_fixture.ChooseCompanyTests(methodName='runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
        _,state=cli.choose_company(fixture.missions,fixture.mission)
        records=AssessmentReplayTests(methodName='runTest');records.setUp();self.addCleanup(records.doCleanups)
        records.state=state;records.mission=fixture.mission;body=cli_fixture._spec_body();body['assessment']='x'*1407
        records.root(body=body)
        config=fixture.state_dir/'model.json';config.write_text(json.dumps({'structured_output_repair':{'max_attempts':0}}))
        class NoProvider:
            def __init__(self,config,**kwargs):self.config=config;self.scheduler_db=str(records.db)
            def call(self,**_):raise AssertionError('matched semantic failure must not buy another repair')
        with patch('dalton_core.company_model_cli.CockpitModel',NoProvider), patch(
            'dalton_core.company_model_cli.validate_financials_for_repair',
            side_effect=cli.FinancialStatementStructureError('filed line unit differs or is ambiguous')):
            result=cli.run_model_spec(state_dir=fixture.state_dir,model_config_path=config,
                summary_dir=fixture.state_dir/'summary',scheduler_db=records.db,company_ref=cli_fixture.ACN)
        self.assertEqual(result['spec_status'],'refused')
        self.assertIn('filed line unit differs',result['failure_reason'])
        self.assertEqual(result['formal_authority_writes'],0)
        self.assertEqual(result['cost_micros'],0)
        self.assertTrue(result['replayed'])
        self.assertEqual(records.connection.execute('SELECT count(*) FROM scheduler_work_orders').fetchone()[0],1)

    def test_real_cli_old_inflight_failed_or_absent_answer_uses_normal_new_task(self):
        for status in ('inflight','failed','absent'):
            with self.subTest(status=status):
                fixture=cli_fixture.ChooseCompanyTests(methodName='runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
                _,state=cli.choose_company(fixture.missions,fixture.mission)
                records=AssessmentReplayTests(methodName='runTest');records.setUp();self.addCleanup(records.doCleanups)
                records.state=state;records.mission=fixture.mission
                if status!='absent':
                    records.root(body=cli_fixture._spec_body())
                    if status=='inflight':records.connection.execute('DELETE FROM scheduler_formal_results')
                    else:records.connection.execute("UPDATE scheduler_formal_results SET terminal_state='failed'")
                    records.connection.commit()
                config=fixture.state_dir/'model.json';config.write_text(json.dumps({'structured_output_repair':{'max_attempts':0}}))
                calls=[]
                class NormalProvider:
                    def __init__(self,config,**kwargs):self.config=config;self.scheduler_db=str(records.db)
                    def call(self,**kwargs):
                        calls.append(kwargs)
                        return {'text':json.dumps(cli_fixture._spec_body()),'replayed':False,'cost_micros':0}
                with patch('dalton_core.company_model_cli.CockpitModel',NormalProvider):
                    result=cli.run_model_spec(state_dir=fixture.state_dir,model_config_path=config,
                        summary_dir=fixture.state_dir/'summary',scheduler_db=records.db,company_ref=cli_fixture.ACN)
                self.assertEqual(result['spec_status'],'fresh',result)
                self.assertEqual(len(calls),1)
                self.assertEqual(calls[0]['_model_spec_request_identity']['task_hash'],specs.TASK_HASH)
                self.assertNotIn('assessment_compatible_replay',result)


if __name__=='__main__':unittest.main()
