"""Accounting totals and diluted-EPS authority remain distinct, without new facts."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from dalton_core import company_model_cli as cli
from dalton_core import company_model_spec as specs
from dalton_core.company_financial_statement_structure import (
    FinancialStatementStructureError, SCHEMA_VERSION, financial_input_authority,
    validate_financial_statement_structure,
)
from dalton_core.mission_model_spec_lane import business_key
from dalton_core.lane_failure_class import LaneFailureBudget
from tests import test_company_financial_statement_structure as financial_fixture
from tests import test_company_model_spec as spec_fixture
from tests import test_model_spec_it_services_structure as replay_fixture

OLD_TASK_HASH = "6cbcf38fe0a5b774cbc098606c36998169e870d637b72fe5ff4f2f4e79387d94"


def bridge_case():
    inputs = financial_fixture.financial_inputs()
    filed = {line['concept']: line for line in inputs['filed_lines']}
    for end, cell in filed['net']['cells'].items():
        value = Decimal(cell['value'])
        filed['eps']['cells'][end]['value'] = str(value / Decimal(filed['shares']['cells'][end]['value']))
    candidate = financial_fixture.proposal(inputs)
    candidate['schema_version'] = SCHEMA_VERSION
    candidate['lines'] = [line for line in candidate['lines'] if line['ref'] not in {'parent', 'nci', 'eps-numerator'}]
    candidate['formulas'] = [formula for formula in candidate['formulas'] if formula['output_ref'] != 'parent']
    candidate['lines'].insert(-1, financial_fixture.derived('eps-numerator', 'diluted_eps_numerator'))
    candidate['formulas'].insert(-1, financial_fixture.sum_formula('eps-numerator', (('net', 1),), 'net'))
    for line in candidate['lines']:
        line['annual_forecast_method'] = 'day_weighted_quarters' if line['role'] == 'diluted_weighted_average_shares' else None
        if line['role'] == 'diluted_weighted_average_shares':
            line['forecast_method'] = 'quarterly_growth'
    candidate['financial_input_hash'] = financial_input_authority(inputs)['content_hash']
    return inputs, candidate


def without_bridge(candidate, *, retag=False):
    result = copy.deepcopy(candidate)
    result['lines'] = [line for line in result['lines'] if line['ref'] != 'eps-numerator']
    result['formulas'] = [f for f in result['formulas'] if f['output_ref'] != 'eps-numerator']
    if retag:
        next(line for line in result['lines'] if line['ref'] == 'net')['role'] = 'diluted_eps_numerator'
    next(f for f in result['formulas'] if f['output_ref'] == 'eps')['numerator_ref'] = 'net'
    return result


def model_reply():
    state = copy.deepcopy(spec_fixture.STATE)
    body = spec_fixture._spec()
    accession = state['filings'][0]['accession']
    for concept, unit in [('synthetic:SharesDiluted', 'shares'), ('synthetic:DilutedEPS', 'usd_per_share')]:
        state['concepts'].append(concept)
        state['statements']['income'].append({'concept': concept, 'label': concept, 'level': 0,
            'parent_concept': None, 'is_breakdown': False, 'dimension_axis': None,
            'unit': unit, 'period_kind': 'duration'})
    s = body['financial_statement_structure']
    s['lines'] += [financial_fixture.filed('shares', 'diluted_weighted_average_shares',
        'synthetic:SharesDiluted', unit='shares', annual='direct_annual', forecast='quarterly_growth'),
        financial_fixture.derived('eps-numerator', 'diluted_eps_numerator'),
        financial_fixture.derived('eps', 'diluted_eps', unit='usd_per_share', annual='annual_ratio')]
    s['formulas'] += [{'output_ref': 'eps-numerator', 'operator': 'sum',
        'terms': [{'line_ref': 'net', 'coefficient': '1'}], 'tie_out_concept': 'us-gaap:NetIncomeLoss',
        'evidence_refs': [accession]}, {'output_ref': 'eps', 'operator': 'divide',
        'numerator_ref': 'eps-numerator', 'denominator_ref': 'shares',
        'tie_out_concept': 'synthetic:DilutedEPS', 'evidence_refs': [accession]}]
    return state, body


class AccountingEPSBridgeTests(unittest.TestCase):
    def test_separate_bridge_preserves_accounting_and_replays_all_filed_periods(self):
        inputs, candidate = bridge_case()
        structure, replay = validate_financial_statement_structure(candidate, financial_fixture.company_spec(), inputs)
        roles = {line['ref']: line['role'] for line in structure['lines']}
        self.assertEqual((roles['net'], roles['eps-numerator']), ('net_income', 'diluted_eps_numerator'))
        self.assertTrue(all(len(f['tested_periods']) == 4 for f in replay['formulas']))
        self.assertEqual(inputs['filed_lines'][8]['concept'], 'net')

    def test_original_direct_net_income_divide_names_exact_failed_formula(self):
        inputs, candidate = bridge_case()
        with self.assertRaises(FinancialStatementStructureError) as raised:
            validate_financial_statement_structure(without_bridge(candidate), financial_fixture.company_spec(), inputs)
        message = str(raised.exception)
        self.assertIn("output_ref='eps'", message)
        self.assertIn("numerator_ref='net'", message)
        self.assertIn("numerator_role='net_income'", message)
        self.assertEqual(specs.structure_error_code(message), 'structure')

    def test_retagging_accounting_net_income_is_still_rejected_even_if_numbers_tie(self):
        inputs, candidate = bridge_case()
        with self.assertRaises(FinancialStatementStructureError) as raised:
            validate_financial_statement_structure(without_bridge(candidate, retag=True), financial_fixture.company_spec(), inputs)
        message = str(raised.exception)
        self.assertIn("output_ref='net'", message)
        self.assertIn("output_role='diluted_eps_numerator'", message)
        self.assertIn("'line_ref': 'pretax', 'role': 'pretax_income'", message)
        self.assertIn("'line_ref': 'tax', 'role': 'income_tax_expense'", message)
        self.assertEqual(specs.structure_error_code(message), 'structure')

    def test_bridge_does_not_weaken_filed_eps_tie(self):
        inputs, candidate = bridge_case()
        next(line for line in inputs['filed_lines'] if line['concept'] == 'eps')['cells']['2025-12-31']['value'] = '9.9'
        candidate['financial_input_hash'] = financial_input_authority(inputs)['content_hash']
        with self.assertRaisesRegex(FinancialStatementStructureError, 'does not tie'):
            validate_financial_statement_structure(candidate, financial_fixture.company_spec(), inputs)

    def test_unknown_dilution_adjustment_cannot_be_invented(self):
        inputs, candidate = bridge_case()
        candidate['lines'].append({**financial_fixture.filed('preferred', 'preferred_dividends', 'unfiled:Preferred'),
                                  'annual_forecast_method': None})
        next(f for f in candidate['formulas'] if f['output_ref'] == 'eps-numerator')['terms'].append({'line_ref': 'preferred', 'coefficient': '-1'})
        with self.assertRaisesRegex(FinancialStatementStructureError, 'not a filed line'):
            validate_financial_statement_structure(candidate, financial_fixture.company_spec(), inputs)

    def test_only_exact_eps_rule_receives_bridge_repair_scope(self):
        prompt = cli._repair_prompt('{}', specs.CompanyModelSpecError(
            "sum formula roles do not match its company statement output; output_role='diluted_eps_numerator'", code='structure'))
        self.assertIn('you may add one derived numerator line', prompt)
        self.assertIn('Keep every other line', prompt)
        self.assertIn('Do not add a filed line', prompt)
        other = cli._repair_prompt('{}', specs.CompanyModelSpecError(
            "sum formula roles do not match its company statement output; output_role='operating_income'", code='structure'))
        self.assertNotIn('you may add one derived numerator line', other)
        self.assertIn('Do not add, remove or rename any line', other)

    def test_versioned_contract_changes_normal_request_identity_without_clearing_old_key(self):
        self.assertNotEqual(specs.TASK_HASH, OLD_TASK_HASH)
        self.assertEqual(cli.REPAIR_CONTRACT_REF, 'contract:company-model-spec-structured-output-repair:0.9')
        self.assertEqual(cli.DEFAULT_REPAIR_ATTEMPTS, 1)
        common = dict(company_ref='company:synthetic', state_hash='a'*64, repair_policy_hash='b'*64, validation_hash='c'*64)
        old = business_key(**common, task_hash=OLD_TASK_HASH, repair_contract_hash='d'*64)
        new = business_key(**common, task_hash=specs.TASK_HASH)
        self.assertNotEqual(old, new)
        self.assertIn(OLD_TASK_HASH, old)
        self.assertIn(specs.EPS_NUMERATOR_BRIDGE_GUIDANCE, specs.build_prompt(spec_fixture.STATE))
        budget = LaneFailureBudget('mission_model_spec')
        budget.record(old, status='content_refused', reason='EPS numerator role is invalid')
        self.assertIsNotNone(budget.blocked(old))
        self.assertIsNone(budget.blocked(new))
        self.assertIsNotNone(budget.blocked(old))  # New identity does not erase the old refusal.

    def test_real_spec_parser_and_bounded_repair_accept_two_layer_bridge(self):
        state, valid = model_reply()
        bad = copy.deepcopy(valid)
        bad['financial_statement_structure'] = without_bridge(valid['financial_statement_structure'])
        calls=[]
        class Model:
            scheduler_db = None
            def call(self, **kwargs):
                calls.append(kwargs)
                return {'text': json.dumps(valid), 'work_order_ref': 'work:synthetic-repair',
                    'work_order_hash': '1'*64, 'result_envelope_ref': 'result:synthetic-repair',
                    'result_envelope_hash': '2'*64, 'invocation_ref': 'invocation:synthetic-repair',
                    'route_decision_ref': 'route:synthetic-repair', 'cost_micros': 100}
        original={'text':json.dumps(bad),'work_order_ref':'work:synthetic-original','work_order_hash':'3'*64,
            'result_envelope_ref':'result:synthetic-original','result_envelope_hash':'4'*64,
            'invocation_ref':'invocation:synthetic-original','route_decision_ref':'route:synthetic-original'}
        accepted, repairs, call = cli._validated_spec_with_repair(model=Model(), state=state, mission={},
            original_call=original, repair_config={'max_attempts':1}, decided_by='automation:test')
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]['cost_micros'],100)
        self.assertIn('you may add one derived numerator line',calls[0]['prompt'])
        roles={line['ref']:line['role'] for line in accepted['financial_statement_structure']['lines']}
        self.assertEqual((roles['net'],roles['eps-numerator']),('net_income','diluted_eps_numerator'))
        self.assertEqual(call['work_order_ref'],'work:synthetic-repair')

    def test_prior_paid_repair_with_old_task_is_not_replayed_as_the_new_question(self):
        original = replay_fixture._derived_claims_concept()
        message = 'financial_statement_structure is invalid: a derived line cannot claim a filed concept'
        binding = replay_fixture._binding(original, message)
        binding['task_hash'] = OLD_TASK_HASH
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'scheduler.sqlite'
            replay_fixture._scheduler_db(db, [{'binding': binding, 'text': replay_fixture._eps_misdeclared()}])
            before = db.read_bytes()
            self.assertIsNone(cli._prior_contract_repair(db, state_hash='a'*64,
                parent_text=original, validation_error={'code': 'structure', 'message': message},
                exclude=set()))
            self.assertEqual(db.read_bytes(), before)

    def test_retagged_repair_is_refused_after_one_call_and_keeps_cost_proof(self):
        state, valid = model_reply()
        original_body = copy.deepcopy(valid)
        original_body['financial_statement_structure'] = without_bridge(valid['financial_statement_structure'])
        retagged = copy.deepcopy(valid)
        retagged['financial_statement_structure'] = without_bridge(valid['financial_statement_structure'], retag=True)
        calls, repairs = [], []
        class Model:
            scheduler_db = None
            def call(self, **kwargs):
                calls.append(kwargs)
                return {'text': json.dumps(retagged), 'work_order_ref': 'work:synthetic-refused-repair',
                    'work_order_hash': '1'*64, 'result_envelope_ref': 'result:synthetic-refused-repair',
                    'result_envelope_hash': '2'*64, 'invocation_ref': 'invocation:synthetic-refused-repair',
                    'route_decision_ref': 'route:synthetic-refused-repair', 'cost_micros': 137}
        original = {'text': json.dumps(original_body), 'work_order_ref': 'work:synthetic-original',
            'work_order_hash': '3'*64, 'result_envelope_ref': 'result:synthetic-original',
            'result_envelope_hash': '4'*64, 'invocation_ref': 'invocation:synthetic-original',
            'route_decision_ref': 'route:synthetic-original'}
        with self.assertRaisesRegex(specs.CompanyModelSpecError,
                                    "output_role='diluted_eps_numerator'"):
            cli._validated_spec_with_repair(model=Model(), state=state, mission={},
                original_call=original, repair_config={'max_attempts': 1},
                decided_by='automation:test', repair_attempts=repairs)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]['cost_micros'], 137)
        self.assertEqual(repairs[0]['invocation_ref'], 'invocation:synthetic-refused-repair')
        self.assertFalse(repairs[0]['replayed'])
        self.assertEqual(json.loads(original['text']), original_body)


if __name__=='__main__': unittest.main()
