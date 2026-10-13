"""Previously paid bounded coefficient repairs stay reusable without new calls."""
import copy,json,hashlib,unittest
from dalton_core.company_financial_statement_structure import FinancialStatementStructureError
from pathlib import Path
from unittest.mock import patch
from dalton_core import company_model_spec as specs,company_model_cli as cli
from dalton_core.model_spec_assessment_replay import find_parent,validate_parent,AssessmentReplayError
from dalton_core.model_spec_signed_expense_repair import SignedExpenseRepairError
from dalton_core.cockpit_model import build_work
from dalton_core.store import content_hash,canonical_json
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from tests import test_model_spec_assessment_replay as old,test_model_spec_signed_expense_repair as signed

# A historical paid Work must retain its frozen published contract, not the
# current repair contract. The product reader still checks its exact known hash.
OLD_REPAIR_CONTRACT = json.loads((Path(__file__).parent / 'fixtures' / 'model_spec_repair_contract_v09.json').read_text())
OLD_REPAIR_CONTRACT_HASH = content_hash(OLD_REPAIR_CONTRACT)
assert OLD_REPAIR_CONTRACT_HASH == 'afb22cd7a12c408bfb166f5abb63b7072488e689d782958df9cacb4584310448'

class CompatibleSignedReplayTests(unittest.TestCase):
    def setUp(self):
        self.f=old.AssessmentReplayTests('runTest');self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.state,self.body,self.inputs=signed.case();self.state['state_hash']=content_hash({k:v for k,v in self.state.items()if k!='state_hash'})
        self.f.state,self.f.body=self.state,self.body
        self.mission=self.f.mission;self.db=self.f.db;self.c=self.f.connection
        self.validations=[];self.validate=signed.callback(self.inputs,self.validations)

    def parent(self):
        self.root=self.f.root();return find_parent(self.db,state=self.state,mission=self.mission)

    def repair(self, *, mutate_binding=None,body=None,suffix='',settle=True):
        work,formal=self.root;text=formal['outputs']['text'];original=specs.spec_from_response(self.state,text,decided_by='automation:test')
        prior_inputs={**copy.deepcopy(self.inputs),'old_metadata':'retained'}
        try:signed.callback(prior_inputs,[])(original)
        except SignedExpenseRepairError as error:failure=error
        call={k:v for k,v in find_parent(self.db,state=self.state,mission=self.mission)['call'].items()if k in ['work_order_ref','work_order_hash','result_envelope_ref','result_envelope_hash','invocation_ref','route_decision_ref']}
        prompt=cli._repair_prompt(text,failure,contract=OLD_REPAIR_CONTRACT,output_schema=specs.prior_assessment_output_schema())
        binding={'schema_version':'company-model-spec-repair-binding-0.1','root_original':call,'repair_parent':call,
          'original_text_sha256':hashlib.sha256(text.encode()).hexdigest(),'parent_text_sha256':hashlib.sha256(text.encode()).hexdigest(),
          'state_hash':self.state['state_hash'],'task_hash':specs.PRIOR_ASSESSMENT_TASK_HASH,
          'validation_error':{'code':failure.code,'message':str(failure)},'repair_contract_ref':OLD_REPAIR_CONTRACT['ref'],'repair_contract_hash':OLD_REPAIR_CONTRACT_HASH,
          'repair_prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),'repair_config':{'max_attempts':1},'repair_number':1}
        if mutate_binding:mutate_binding(binding)
        repair=build_work(purpose='model_spec',request_id='model-spec-repair:'+content_hash(binding)[:32]+suffix,prompt=prompt,
          mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'],max_input_tokens=250000,max_output_tokens=32768,max_cost_usd=14,max_seconds=300,structured_output_repair=binding)
        result=self.f.store(repair,signed.fixed(self.body)if body is None else body)
        budget=ThesisImpactBudgetStore(self.db.parent/'thesis-impact-budget.sqlite');self.addCleanup(budget.close)
        budget.register_policy(policy_version_id='budget:test',day_cap_micros=10000000)
        admitted=budget.admit(policy_version_id='budget:test',day='2026-10-11',work_order_ref=repair.id,attempt_number=1,phase='assessment',route_decision_ref=result[1]['metadata']['route_decision_ref'],reserved_micros=100000,purpose='model_spec')
        mission_binding={'mission_ref':'mission:canonical','mission_version_ref':self.mission['id'],'mission_version_hash':self.mission['content_hash']}
        budget.connection.execute('INSERT INTO model_mission_budget_bindings(admission_id,mission_ref,record_json)VALUES(?,?,?)',(admitted['admission_id'],mission_binding['mission_ref'],canonical_json(mission_binding)))
        if settle:budget.settle(admitted['admission_id'],actual_micros=1234)
        return result,budget

    def run_parent(self,parent):
        return validate_parent(parent,state=self.state,mission=self.mission,decided_by='automation:test',financial_validator=self.validate)

    def test_paid_signed_repair_replays_once_and_checks_current_complete_financials(self):
        parent=self.parent();repair,budget=self.repair();before=list(self.c.iterdump());oldbudget=list(budget.connection.iterdump())
        spec,attempts,call,proof=self.run_parent(parent)
        self.assertEqual(call['work_order_ref'],repair[0]['id']);self.assertEqual(spec['task_hash'],specs.TASK_HASH)
        self.assertEqual(len(self.validations),2);self.assertEqual(len(attempts),1)
        self.assertEqual(attempts[0]['settlement']['actual_micros'],1234)
        self.assertNotEqual(attempts[0]['prior_validation_proof_hash'],attempts[0]['current_validation_proof_hash'])
        self.assertEqual(list(self.c.iterdump()),before);self.assertEqual(list(budget.connection.iterdump()),oldbudget)
        self.assertEqual(proof['root_authority']['work_order_ref'],self.root[0]['id'])
        self.assertEqual(proof['accepted_authority']['work_order_ref'],repair[0]['id'])
        # This API has no provider/recovery parameter and works with no newly
        # authorised attempts. Repeated validation reuses the same exact rows.
        self.run_parent(parent);self.assertEqual(self.c.execute('SELECT COUNT(*)FROM scheduler_work_orders').fetchone()[0],2)

    def test_unsettled_or_drifted_settlement_refuses_no_repair_work(self):
        parent=self.parent();repair,budget=self.repair(settle=False)
        with self.assertRaisesRegex(AssessmentReplayError,'settlement'):self.run_parent(parent)
        admitted=budget.connection.execute('SELECT admission_id FROM thesis_impact_day_admissions').fetchone()[0];budget.settle(admitted,actual_micros=1234)
        budget.connection.execute('DROP TRIGGER thesis_impact_day_settlements_no_update')
        budget.connection.execute('UPDATE thesis_impact_day_settlements SET actual_micros=1')
        with self.assertRaisesRegex(AssessmentReplayError,'settlement'):self.run_parent(parent)
        self.assertEqual(self.c.execute('SELECT COUNT(*)FROM scheduler_work_orders').fetchone()[0],2)

    def test_foreign_parent_or_mission_and_more_than_one_repair_refuse(self):
        parent=self.parent();self.repair(mutate_binding=lambda b:b['repair_parent'].update(work_order_hash='0'*64))
        with self.assertRaisesRegex(AssessmentReplayError,'binding differs'):self.run_parent(parent)
        self.repair(suffix=':another')
        with self.assertRaisesRegex(AssessmentReplayError,'not unique'):self.run_parent(parent)

    def test_any_other_number_formula_or_evidence_change_refuses(self):
        parent=self.parent();bad=signed.fixed(self.body);bad['assessment']+=' changed'
        self.repair(body=bad)
        with self.assertRaisesRegex(specs.CompanyModelSpecError,'outside its exact coefficient scope'):self.run_parent(parent)
        self.assertEqual(self.c.execute('SELECT COUNT(*)FROM scheduler_work_orders').fetchone()[0],2)

    def test_current_economic_proof_must_match_not_just_its_coefficient_path(self):
        parent=self.parent();self.repair();self.inputs['filed_lines'][0]['cells'][next(iter(self.inputs['filed_lines'][0]['cells']))]['value']='1001'
        # An altered source must not be waived because the old repair worked.
        with self.assertRaises((FinancialStatementStructureError,AssessmentReplayError)):self.run_parent(parent)
        self.assertEqual(self.c.execute('SELECT COUNT(*)FROM scheduler_work_orders').fetchone()[0],2)

    def test_repaired_candidate_next_semantic_failure_does_not_buy_another_repair(self):
        parent=self.parent();self.repair();original=self.validate
        def validate(spec):
            value=original(spec)
            if spec['financial_statement_structure']['formulas'][0]['terms'][-1]['coefficient']=='1':raise specs.CompanyModelSpecError('next semantic failure')
            return value
        self.validate=validate
        with self.assertRaisesRegex(specs.CompanyModelSpecError,'next semantic failure'):self.run_parent(parent)
        self.assertEqual(self.c.execute('SELECT COUNT(*)FROM scheduler_work_orders').fetchone()[0],2)



    def test_latest_formal_attempt_wins_success_then_failure_and_failure_then_success(self):
        parent=self.parent();repair,budget=self.repair()
        self.c.execute('ALTER TABLE scheduler_formal_results RENAME TO old_results')
        self.c.execute('CREATE TABLE scheduler_formal_results AS SELECT *FROM old_results')
        first=dict(zip([r[1]for r in self.c.execute('PRAGMA table_info(scheduler_formal_results)')],self.c.execute('SELECT *FROM scheduler_formal_results WHERE work_order_id=?',(repair[0]['id'],)).fetchone()))
        later=copy.deepcopy(first);wire=json.loads(later['result_envelope_json']);wire['id']='result:later';wire['status']='failed';wire['error']={'code':'MODEL_ADAPTER_REJECTED','message':'no valid result','retryable':False};wire.pop('outputs')
        later.update(result_envelope_id=wire['id'],result_envelope_json=canonical_json(wire),result_envelope_hash=content_hash(wire),terminal_state='failed',result_record_id='formal:later',attempt_number=2)
        record={'id':later['result_record_id'],'work_order_id':later['work_order_id'],'attempt_number':2,'result_envelope_id':later['result_envelope_id'],'result_envelope_hash':later['result_envelope_hash'],'terminal_state':'failed','created_at':later['created_at']};later['content_hash']=content_hash(record)
        self.c.execute('INSERT INTO scheduler_formal_results VALUES('+','.join('?'for _ in later)+')',tuple(later.values()));self.c.commit()
        with self.assertRaises(AssessmentReplayError):self.run_parent(parent)
        # A still newer succeeded result is the exact selected result, not an
        # arbitrary old success. No oldest-row ordering can make this pass.
        newest=copy.deepcopy(first);newest['attempt_number']=3;newest['result_record_id']='formal:newest';record={**record,'id':newest['result_record_id'],'attempt_number':3,'terminal_state':'succeeded','result_envelope_id':newest['result_envelope_id'],'result_envelope_hash':newest['result_envelope_hash']};newest['content_hash']=content_hash(record)
        self.c.execute('INSERT INTO scheduler_formal_results VALUES('+','.join('?'for _ in newest)+')',tuple(newest.values()));self.c.commit()
        self.assertEqual(self.run_parent(parent)[2]['work_order_ref'],repair[0]['id'])

    def test_free_validation_marker_wakes_exact_old_refusal_without_changing_task_or_policy(self):
        from tests import test_mission_model_spec_lane as lanes
        f=lanes.ModelSpecLaneTests('runTest');f.setUp();self.addCleanup(f.doCleanups)
        f.missions.companies=[lanes.ACN];f.mission['universe']=f.mission['universe'][:1]
        f.launcher.financial_validation_contract_hash=cli.model_spec_validation_contract_hash
        current=cli.model_spec_validation_contract();previous={k:v for k,v in current.items()if k!='assessment_compatible_signed_repair'}
        with patch('dalton_core.company_model_cli.model_spec_validation_contract',return_value=previous):
            first=f.lane.dispatch_once()
            f.launcher.finish(first['ticket_ref'],summary={'spec_status':'refused','failure_reason':'signed exact old coefficient'})
            self.assertEqual(f.lane.dispatch_once()['status'],'held')
        resumed=f.lane.dispatch_once();self.assertEqual(resumed['status'],'launched')
        self.assertNotEqual(f.launcher.started[0]['financial_validation_contract_hash'],f.launcher.started[1]['financial_validation_contract_hash'])
        self.assertEqual(len(f.launcher.started),2)
        self.assertEqual(specs.TASK_HASH,'608338bcd6cf70cbe6e012acd5f579b99d34f9d990d5bc846c4059a198eda9af')
        self.assertEqual(cli.REPAIR_CONTRACT_REF,cli.REPAIR_CONTRACT['ref'])
        self.assertEqual(f.launcher.repair_config,{'max_attempts':0})

    def test_current_same_task_specs_do_not_launch_when_free_validation_marker_changes(self):
        from tests import test_mission_model_spec_lane as lanes
        f=lanes.ModelSpecLaneTests('runTest');f.setUp();self.addCleanup(f.doCleanups)
        f.launcher.financial_validation_contract_hash=cli.model_spec_validation_contract_hash
        previous={k:v for k,v in cli.model_spec_validation_contract().items()if k!='assessment_compatible_signed_repair'}
        with patch('dalton_core.company_model_cli.model_spec_validation_contract',return_value=previous):
            for company in f.missions.companies:
                launched=f.lane.dispatch_once();f.succeed(launched['ticket_ref'],company)
        before=len(f.launcher.started);result=f.lane.dispatch_once()
        self.assertEqual(result['status'],'idle');self.assertEqual(len(f.launcher.started),before)

if __name__=='__main__':unittest.main()
