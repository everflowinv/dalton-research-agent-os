"""Financial proofs must cross the actual Cockpit/Scheduler admission boundary."""
import copy,json,hashlib,unittest
from dataclasses import replace
from unittest.mock import patch
from dalton_core import company_model_cli as cli,company_model_spec as specs
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.scheduler import Scheduler
from dalton_core.store import content_hash
from tests import test_cockpit_model_fallback as runtime
from tests import test_model_spec_signed_expense_repair as single
from tests import test_model_spec_joint_signed_coefficients as joint
from tests import test_cash_ratio_applicability as ratios
from dalton_core import model_spec_signed_expense_repair as signed_rule
from dalton_core import cash_flow_ratio_applicability as ratio_rule
from dalton_core.company_financial_statement_structure import materialize_financial_statement_structure
from dalton_core.model_forecast_driver import build_cash_flow_companion
from dalton_core.coverage_mission import company_model_spec_id

def cash_case():
    state,raw,inputs=single.case();raw=single.fixed(raw)
    for line in raw['financial_statement_structure']['lines']:
        if line['ref']=='pretax':line.update(kind='derived',concept=None,forecast_method='formula',forecast_base_ref=None)
        if line['ref']=='signed-other':line.update(forecast_method='share_of_line',forecast_base_ref='revenue')
        if line['ref']=='tax':line.update(forecast_method='share_of_line',forecast_base_ref='pretax')
    raw['financial_statement_structure']['formulas'].append({'output_ref':'pretax','operator':'sum',
        'terms':[{'line_ref':'operating','coefficient':'1'}],
        'tie_out_concept':'us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest',
        'evidence_refs':copy.deepcopy(raw['financial_statement_structure']['formulas'][0]['evidence_refs'])})
    cash_inputs=ratios.fixture()[0]['cash_flow_inputs']
    inputs['cash_flow_inputs']=copy.deepcopy(cash_inputs)
    for line in cash_inputs:
        state['concepts'].append(line['concept'])
        state['statements'].setdefault('cash',[]).append({'concept':line['concept'],'label':line['concept'],
            'level':0,'parent_concept':None,'is_breakdown':False,'dimension_axis':None,'unit':'USD','period_kind':'duration'})
    raw['cash_flow_companion']['lines'][0].update(concept=cash_inputs[0]['concept'],
        forecast_method='share_of_line',forecast_base_ref='net',because='Declared net income ratio.')
    first=min(inputs['filed_lines'][0]['cells'])
    for line in inputs['filed_lines']:
        if line['concept']=='us-gaap:IncomeTaxExpenseBenefit':line['cells'][first]['value']='190'
        if line['concept']in ('us-gaap:NetIncomeLoss','synthetic:DilutedEPS'):line['cells'][first]['value']='0'
        line['duration_facts']=[{'period_start':c['period_start'],'period_end':end,'period_kind':'quarter',
            'value':c['value'],'unit':c['unit'],'source_accessions':c['source_accessions'],'source_forms':['10-Q']}
            for end,c in line['cells'].items()]
    fixed=copy.deepcopy(raw)
    fixed['cash_flow_companion']['lines'][0].update(forecast_method='unavailable',forecast_base_ref=None,
        because='Zero denominator makes this ratio unsuitable; cash is still missing.')
    def validate(spec):
        candidate={**spec,'spec_id':company_model_spec_id(spec['company_ref'],spec['state_hash'],spec['task_hash'])}
        numeric={**copy.deepcopy(inputs),'spec_ref':candidate['spec_id']}
        structure,_=materialize_financial_statement_structure(candidate,numeric)
        try:build_cash_flow_companion(candidate,numeric,structure)
        except ratio_rule.CashRatioUnavailable as e:raise ratio_rule.repair_error(candidate,numeric,e.diagnostic)
    return state,raw,inputs,fixed,validate

class FinancialRepairPaidAuthorityTests(unittest.TestCase):
    def run_actual(self,kind='single',change=None,expected=None):
        fixture=runtime.CockpitChainTests('test_model_spec_repair_is_a_second_budgeted_scheduler_work');fixture.setUp();self.addCleanup(fixture.doCleanups)
        state,bad,inputs=single.case() if kind=='single' else joint.case(3)
        if kind=='ratio':state,bad,inputs,corrected,validate=cash_case()
        if kind!='ratio':corrected=single.fixed(bad)
        if kind=='joint':
            for term in corrected['financial_statement_structure']['formulas'][0]['terms']:
                if term['line_ref'].startswith('expense-') or term['line_ref']=='signed-other':term['coefficient']='1'
        if kind!='ratio':validate=single.callback(inputs,[]) if kind=='single' else joint.callback(inputs)
        class Adapter(runtime.ChainAdapter):
            def __init__(self):super().__init__({});self.outputs=[bad,corrected]
            def execute(self,work,route,profile):
                inv,env=super().execute(work,route,profile)
                return inv,replace(env,outputs={'text':json.dumps(self.outputs.pop(0))})
        adapter=Adapter();model=fixture._model(adapter,policy_version_ref=fixture.pinned_policy,model_spec_output_tokens=6000)
        identity=cli.model_spec_request_identity(state['state_hash'],repair_config={'max_attempts':1})
        root=model.call(purpose='model_spec',request_id=cli.model_spec_request_id(state['state_hash'],repair_config={'max_attempts':1}),
            prompt=specs.build_prompt(state),mission=fixture.mission,_model_spec_request_identity=identity)
        call=model.call;captured=[]
        def intercept(**kw):
            if '_structured_output_repair' in kw:
                b=copy.deepcopy(kw['_structured_output_repair']);captured.append(copy.deepcopy(b))
                if change:
                    change(b,model)
                    kw['_structured_output_repair']=b
                    # Attacker may recompute a public digest; never the frozen
                    # source diagnosis or the owner capability.
                    kw['request_id']='model-spec-repair:'+content_hash(b)[:32]
            return call(**kw)
        with patch.object(model,'call',side_effect=intercept):
            if expected:
                with self.assertRaisesRegex(CockpitModelError,expected):
                    cli._validated_spec_with_repair(model=model,state=state,mission=fixture.mission,original_call=root,
                        repair_config={'max_attempts':1},decided_by='automation:test',financial_validator=validate)
                self.assertEqual(len(adapter.served),1)
                return
            accepted,attempts,result=cli._validated_spec_with_repair(model=model,state=state,mission=fixture.mission,
                original_call=root,repair_config={'max_attempts':1},decided_by='automation:test',financial_validator=validate)
            self.assertEqual(len(adapter.served),2);self.assertEqual(len(attempts),1)
            proof=json.loads(captured[0]['validation_error']['message'])
            if kind=='ratio':self.assertEqual(proof,captured[0]['cash_ratio_proof'])
            else:self.assertEqual(proof['contract_ref'],signed_rule.CONTRACT_REF if kind=='single' else joint.JOINT_CONTRACT_REF)
            with Scheduler(model.scheduler_db) as sch:
                original=sch.work_order_authority(root['work_order_ref']);child=sch.work_order_authority(result['work_order_ref'])
                self.assertEqual(child['work_order']['budget'],original['work_order']['budget'])
                original_bytes=copy.deepcopy(original)
            # Existing successful repair is reused for free even with max0.
            accepted2,attempts2,result2=cli._validated_spec_with_repair(model=model,state=state,mission=fixture.mission,
                original_call=root,repair_config={'max_attempts':0},decided_by='automation:test',financial_validator=validate)
            self.assertEqual(len(adapter.served),2);self.assertTrue(attempts2[0]['replayed'])
            self.assertEqual(result2['work_order_ref'],result['work_order_ref'])
            with Scheduler(model.scheduler_db) as sch:self.assertEqual(sch.work_order_authority(root['work_order_ref']),original_bytes)

    def test_real_single_and_joint_adapter_admission_and_free_replay(self):
        for kind in ('single','joint','ratio'):
            with self.subTest(kind=kind):self.run_actual(kind)

    def test_unknown_and_rehashed_wrong_source_proof_cannot_send(self):
        for field in ('contract_ref','financial_inputs_hash','required_coefficient'):
            def change(binding,model):
                proof=json.loads(binding['validation_error']['message'])
                proof[field]='unknown:contract' if field=='contract_ref' else 'e'*64 if field=='financial_inputs_hash' else '-1'
                proof['content_hash']=content_hash({k:v for k,v in proof.items()if k!='content_hash'})
                binding['validation_error']['message']=json.dumps(proof,sort_keys=True,separators=(',',':'))
            with self.subTest(field=field):self.run_actual(change=change,expected='financial repair parent source proof differs')

    def test_missing_owner_and_missing_signed_proof_are_friendly_zero_send(self):
        self.run_actual(change=lambda b,m:delattr(m,'_model_spec_financial_repair_authority'),expected='live source validation owner')
        self.run_actual(change=lambda b,m:b['validation_error'].update(message='null'),expected='signed financial proof is missing')

    def test_cash_ratio_missing_forged_and_foreign_proof_are_zero_send(self):
        self.run_actual('ratio',change=lambda b,m:b.pop('cash_ratio_proof'),expected='cash ratio proof is missing')
        for field in ('company_ref','financial_input_hash','diagnostic'):
            def change(binding,model):
                p=binding['cash_ratio_proof']
                if field=='diagnostic':p[field]['samples'][0]['denominator']='999'
                else:p[field]='company:foreign' if field=='company_ref' else 'e'*64
                p['content_hash']=content_hash({k:v for k,v in p.items()if k!='content_hash'})
                binding['validation_error']['message']=json.dumps(p,sort_keys=True,separators=(',',':'))
            with self.subTest(field=field):self.run_actual('ratio',change=change,expected='financial repair parent source proof differs')

    def test_source_or_mission_owner_changes_refuse_before_adapter(self):
        def change(binding,model):model._model_spec_financial_repair_authority._state['state_hash']='e'*64
        self.run_actual(change=change,expected='owner state differs')
        def mission(binding,model):model._model_spec_financial_repair_authority._mission['content_hash']='e'*64
        self.run_actual(change=mission,expected='owner mission differs')

    def test_contract_wakes_normal_revalidation_without_new_repair_slot(self):
        from tests import test_mission_model_spec_lane as lanes
        f=lanes.ModelSpecLaneTests('runTest');f.setUp();self.addCleanup(f.doCleanups)
        f.missions.companies=[lanes.ACN];f.mission['universe']=f.mission['universe'][:1]
        f.launcher.financial_validation_contract_hash=cli.model_spec_validation_contract_hash
        current=cli.model_spec_validation_contract();old={k:v for k,v in current.items()if k!='financial_repair_admission'}
        with patch.object(cli,'model_spec_validation_contract',return_value=old):
            first=f.lane.dispatch_once();f.launcher.finish(first['ticket_ref'],summary={'spec_status':'refused','failure_reason':'financial source proof adapter refused'})
            self.assertEqual(f.lane.dispatch_once()['status'],'held')
        resumed=f.lane.dispatch_once();self.assertEqual(resumed['status'],'launched')
        self.assertEqual(f.launcher.repair_config,{'max_attempts':0})
        f.launcher.finish(resumed['ticket_ref'],summary={'spec_status':'refused','failure_reason':'real remaining gap'})
        self.assertEqual(f.lane.dispatch_once()['status'],'held');self.assertEqual(len(f.launcher.started),2)

    def test_signed_prompt_without_proof_refuses_without_attribute_error(self):
        with self.assertRaisesRegex(specs.CompanyModelSpecError,'proof is missing'):
            cli._repair_prompt('{}',specs.CompanyModelSpecError('{}',code=signed_rule.ERROR_CODE))

    def test_owner_validator_with_missing_proof_never_opens_repair(self):
        state,body,_=single.case()
        class Model:
            scheduler_db=None
            calls=[]
            def call(self,**kwargs):self.calls.append(kwargs);raise AssertionError('no repair call permitted')
        model=Model()
        from tests import test_model_spec_cash_flow_base_repair as calls
        def missing(spec):raise specs.CompanyModelSpecError('{}',code=signed_rule.ERROR_CODE)
        with self.assertRaisesRegex(specs.CompanyModelSpecError,'financial repair proof is missing'):
            cli._validated_spec_with_repair(model=model,state=state,mission={},original_call=calls.root_call(body),
                repair_config={'max_attempts':1},decided_by='automation:test',financial_validator=missing)
        self.assertEqual(model.calls,[])
