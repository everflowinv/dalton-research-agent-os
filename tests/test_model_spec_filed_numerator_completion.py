"""Unavailable filed EPS totals remain unavailable, with bounded repair replay."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from dalton_core import company_model_cli as cli,company_model_spec as specs
from dalton_core.forecast_coverage import forecast_computability,PROPOSAL_METHOD_COMPLETION_REF
from tests import test_model_spec_eps_numerator_bridge as bridge
from tests import test_model_spec_it_services_structure as replay

OLD_REPAIR_HASH='ea0236f3c92367e851406e5e8fb89556dfd0d5753b9cc49945f6fcc3a43226a5'
OLD_VALIDATION_HASH='9f854ccc7f6753334062826eb8e412bbfc1f93e69eaf7b8ada210b22a1372690'
ERROR='financial_statement_structure is invalid: a filed subtotal may only be actual/tie authority or unavailable; forecasted subtotals require an explicit formula'


def reply(method='unavailable'):
    state,body=bridge.model_reply()
    concept='synthetic:DilutedNetIncomeAvailableToCommon'
    state['concepts'].append(concept)
    state['statements']['income'].append({'concept':concept,'label':'Diluted net income available to common',
        'level':0,'parent_concept':None,'is_breakdown':False,'dimension_axis':None,'unit':'USD','period_kind':'duration'})
    s=body['financial_statement_structure']
    # Coverage completion is the same proposal-time path as the two real 0.6 payloads.
    s['schema_version']='0.6'
    for line in s['lines']:
        line['annual_forecast_method']='unavailable' if line['role']=='diluted_weighted_average_shares' else None
    line=next(x for x in s['lines']if x['ref']=='eps-numerator')
    line.update(kind='filed',concept=concept,forecast_method=method,
                forecast_base_ref='net' if method=='share_of_line' else None)
    s['formulas']=[x for x in s['formulas']if x['output_ref']!='eps-numerator']
    return state,body


class FiledNumeratorCompletionTests(unittest.TestCase):
    def test_real_parser_preserves_unavailable_without_inventing_forecast(self):
        state,body=reply();before=copy.deepcopy(body)
        normalized=specs.spec_from_response(state,json.dumps(body),decided_by='automation:test')
        structure=normalized['financial_statement_structure'];line=next(x for x in structure['lines']if x['ref']=='eps-numerator')
        self.assertEqual((line['kind'],line['forecast_method'],line['concept']),
                         ('filed','unavailable','synthetic:DilutedNetIncomeAvailableToCommon'))
        self.assertIn('没有预测方法',forecast_computability(structure['lines'],structure['formulas'])['eps'])
        self.assertEqual(body,before)

    def test_wrong_independent_ratio_or_growth_is_still_refused(self):
        for method in ['share_of_line','carry_forward_mean']:
            state,body=reply(method)
            with self.subTest(method=method),self.assertRaisesRegex(specs.CompanyModelSpecError,'filed subtotal.*unavailable'):
                specs.spec_from_response(state,json.dumps(body),decided_by='automation:test')

    def test_known_subtotal_repair_preserves_exact_concept_in_formula_tie(self):
        state,body=reply('share_of_line');prompt=cli._repair_prompt(json.dumps(body),specs.CompanyModelSpecError(ERROR,code='structure'))
        self.assertIn("original exact filed concept in the matching formula's tie_out_concept",prompt)
        self.assertIn('Do not add, remove or rename any line',prompt)
        self.assertIn('keep the subtotal unavailable',prompt)
        s=body['financial_statement_structure'];line=next(x for x in s['lines']if x['ref']=='eps-numerator');concept=line['concept']
        line.update(kind='derived',concept=None,forecast_method='formula',forecast_base_ref=None)
        s['formulas'].append({'output_ref':line['ref'],'operator':'sum','terms':[{'line_ref':'net','coefficient':'1'}],
            'tie_out_concept':concept,'evidence_refs':[state['filings'][0]['accession']]})
        specs.spec_from_response(state,json.dumps(body),decided_by='automation:test')
        s['formulas'][-1]['tie_out_concept']='invented:DifferentNumerator'
        with self.assertRaises(specs.CompanyModelSpecError):
            specs.spec_from_response(state,json.dumps(body),decided_by='automation:test')

    def test_same_task_old_contract_answer_is_revalidated_free_when_repairs_disabled(self):
        state,bad=reply('carry_forward_mean');_,held=reply('unavailable')
        original=json.dumps(bad);answer=json.dumps(held)
        with tempfile.TemporaryDirectory()as d:
            db=Path(d)/'scheduler.sqlite';replay._scheduler_db(db,[{'binding':replay._binding(original,ERROR,contract=OLD_REPAIR_HASH),'text':answer}])
            before=db.read_bytes()
            class Model:
                scheduler_db=db
                def call(self,**kwargs):raise AssertionError('zero configured repairs must not make a call')
            root={'text':original,'work_order_ref':'work:root','work_order_hash':'1'*64,
                'result_envelope_ref':'result:root','result_envelope_hash':'2'*64,
                'invocation_ref':'invocation:root','route_decision_ref':'route:root'}
            accepted,attempts,call=cli._validated_spec_with_repair(model=Model(),state=state,mission={},
                original_call=root,repair_config={'max_attempts':0},decided_by='automation:test')
            self.assertTrue(call['replayed']);self.assertEqual(call['text'],answer)
            self.assertEqual([(x['replayed'],x['cost_micros'])for x in attempts],[(True,0)])
            self.assertEqual(db.read_bytes(),before)
            self.assertEqual(next(x for x in accepted['financial_statement_structure']['lines']if x['ref']=='eps-numerator')['forecast_method'],'unavailable')

    def test_without_matching_held_repair_zero_means_zero_new_calls(self):
        state,body=reply('share_of_line')
        class Model:
            scheduler_db=None
            def call(self,**kwargs):raise AssertionError('disabled repair must not call')
        with self.assertRaisesRegex(specs.CompanyModelSpecError,'filed subtotal'):
            cli._validated_spec_with_repair(model=Model(),state=state,mission={},original_call={'text':json.dumps(body)},
                repair_config={'max_attempts':0},decided_by='automation:test')

    def test_normal_validation_binding_changes_and_task_and_eps_bridge_remain(self):
        self.assertNotEqual(cli.model_spec_validation_contract_hash(),OLD_VALIDATION_HASH)
        self.assertEqual(cli.model_spec_validation_contract()['proposal_method_completion'],PROPOSAL_METHOD_COMPLETION_REF)
        self.assertNotEqual(cli.REPAIR_CONTRACT_HASH,OLD_REPAIR_HASH)
        self.assertEqual(cli.REPAIR_CONTRACT_REF,'contract:company-model-spec-structured-output-repair:0.15')
        self.assertEqual(specs.TASK_HASH,'608338bcd6cf70cbe6e012acd5f579b99d34f9d990d5bc846c4059a198eda9af')
        self.assertEqual(cli.DEFAULT_REPAIR_ATTEMPTS,1)
        prompt=cli._repair_prompt('{}',specs.CompanyModelSpecError('EPS numerator must use the company-specific diluted EPS numerator role',code='structure'))
        self.assertIn(specs.EPS_NUMERATOR_BRIDGE_GUIDANCE,prompt)
