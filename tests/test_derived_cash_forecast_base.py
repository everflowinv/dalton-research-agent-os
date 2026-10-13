from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal
import unittest
from unittest.mock import patch

from dalton_core.cash_flow_derived_base import CONTRACT_REF, derived_base_is_forecastable, derived_trailing_share
from dalton_core.company_model_spec import _cash_flow_companion, CompanyModelSpecError
from dalton_core.model_forecast_driver import (
    ForecastModelUnavailable, ForecastModelAuthority, build_cash_flow_companion,
    build_structured_forecast_model, default_cash_flow_companion_assumptions,
    build_structure_drivers, build_cash_flow_companion_drivers,
)
from dalton_core.store import canonical_json
from tests.test_structured_cash_flow_companion import authorities


def derived_fixture(company='company:test:alpha', unit='usd'):
    inputs, spec, structure, replay, binding = authorities(unit=unit)
    # Both source identities and already-materialized income histories are held.
    # Company labels are deliberately irrelevant to the cash dependency rule.
    spec['cash_flow_companion']['lines'][0]['forecast_base_ref'] = 'net'
    spec['cash_flow_companion']['schema_version'] = '0.2'
    inputs['company_ref'] = company
    spec['company_ref'] = company
    from dalton_core.company_financial_statement_structure import (
        materialize_financial_statement_structure, forecast_structure_binding,
    )
    structure, replay = materialize_financial_statement_structure(spec, inputs)
    binding = forecast_structure_binding(structure, replay, inputs)
    return inputs, spec, structure, replay, binding


class DerivedCashForecastBaseTests(unittest.TestCase):
    def test_two_currencies_parser_and_full_builder_keep_original_source_amounts(self):
        for company, unit in [('company:test:alpha', 'usd'), ('company:test:beta', 'eur')]:
            with self.subTest(company=company):
                inputs, spec, structure, replay, binding = derived_fixture(company, unit)
                before = canonical_json(inputs)
                self.assertTrue(derived_base_is_forecastable(structure, 'net'))
                selected = {x['concept']: x for x in inputs['cash_flow_inputs']}
                raw = copy.deepcopy(spec['cash_flow_companion']); raw['schema_version'] = '0.1'
                parsed = _cash_flow_companion(
                    raw, concepts=set(selected), cash_lines=selected,
                    structure=structure, cash_importance='required')
                self.assertEqual(parsed, spec['cash_flow_companion'])
                self.assertEqual(raw['schema_version'], '0.1')
                # Storage carries the internal definition, not a new raw reply schema.
                stored_spec = json.loads(canonical_json(spec))
                self.assertEqual(build_cash_flow_companion(stored_spec, inputs, structure),
                                 build_cash_flow_companion(spec, inputs, structure))
                with self.assertRaisesRegex(CompanyModelSpecError, 'schema_version is not 0.1'):
                    _cash_flow_companion(stored_spec['cash_flow_companion'],
                                         concepts=set(selected), cash_lines=selected,
                                         structure=structure, cash_importance='required')
                body = build_structured_forecast_model(
                    spec, inputs, structure=structure, replay=replay, binding=binding)
                record = body
                assumptions = [x for x in record['assumptions']
                               if x['driver_ref'] == 'concept:us-gaap:NetCashProvidedByUsedInOperatingActivities']
                self.assertEqual(len(assumptions), len(record['forecast_periods']))
                self.assertEqual({x['provenance']['rule_ref'] for x in assumptions}, {CONTRACT_REF})
                self.assertFalse(any(x.get('concept') == 'net'
                                     for a in assumptions for x in a['refs']))
                self.assertTrue(all(len(x['refs']) >= 4 for x in assumptions))
                results = {x['ref']: x for x in record['results']}
                self.assertTrue(all(x['status'] == 'computed'
                                    for x in results['result:operating_cash_flow']['cells']))
                self.assertEqual(canonical_json(inputs), before)
                # Normal durable writer/read replay remains authoritative.
                from dalton_core.store import DaltonStore
                import tempfile
                from pathlib import Path
                with tempfile.TemporaryDirectory() as directory:
                    store = DaltonStore(str(Path(directory) / 'core.sqlite'))
                    try:
                        authority = ForecastModelAuthority(store)
                        held = authority.publish(body)
                        self.assertEqual(authority.latest(held['company_ref']), {k:v for k,v in held.items() if k != 'status'})
                    finally:
                        store.close()

    def test_derived_missing_history_is_rejected_without_skipping_a_period(self):
        inputs, spec, structure, _replay, _binding = derived_fixture()
        line = next(x for x in inputs['filed_lines'] if x['concept'] == 'cost')
        del line['cells'][next(iter(line['cells']))]
        with self.assertRaisesRegex(ForecastModelUnavailable, 'complete same-duration'):
            build_cash_flow_companion(spec, inputs, structure)

    def test_wrong_duration_does_not_pair_same_end_with_a_different_window(self):
        inputs, spec, structure, _replay, _binding = derived_fixture()
        inputs['cash_flow_inputs'][0]['series']['quarters'][0]['period_start'] = '2024-12-01'
        with self.assertRaisesRegex(ForecastModelUnavailable, 'complete same-duration'):
            build_cash_flow_companion(spec, inputs, structure)

    def test_unit_mismatch_still_rejected(self):
        inputs, spec, structure, _replay, _binding = derived_fixture()
        inputs['cash_flow_inputs'][0]['unit'] = 'eur'
        with self.assertRaisesRegex(ForecastModelUnavailable, 'units differ'):
            build_cash_flow_companion(spec, inputs, structure)

    def test_cycle_unavailable_and_cash_roll_dependency_cannot_be_a_derived_base(self):
        _inputs, _spec, structure, _replay, _binding = derived_fixture()
        for mutate in ('cycle', 'unavailable', 'cash_roll', 'mixed_currency'):
            with self.subTest(mutate=mutate):
                broken = copy.deepcopy(structure)
                byref = {x['ref']: x for x in broken['lines']}
                if mutate == 'cycle':
                    byref['tax']['forecast_base_ref'] = 'net'
                elif mutate == 'unavailable':
                    byref['cost']['forecast_method'] = 'unavailable'
                elif mutate == 'cash_roll':
                    byref['long-term-debt']['forecast_method'] = 'roll_forward'
                else:
                    byref['opex']['unit'] = 'eur'
                self.assertFalse(derived_base_is_forecastable(broken, 'net'))
                selected = {'cash:operating': {'unit': 'usd'}}
                definition = {'schema_version': '0.1', 'lines': [
                    {'role': 'operating_cash_flow', 'concept': 'cash:operating',
                     'forecast_method': 'share_of_line', 'forecast_base_ref': 'net', 'because': 'Original basis.'},
                    {'role': 'capital_expenditure', 'concept': None,
                     'forecast_method': 'unavailable', 'forecast_base_ref': None, 'because': 'No source.'}],
                    'formula': {'output_ref':'free_cash_flow','operator':'sum','terms':[
                        {'role':'operating_cash_flow','coefficient':'1'},
                        {'role':'capital_expenditure','coefficient':'-1'}]}}
                with self.assertRaises(CompanyModelSpecError):
                    _cash_flow_companion(definition, concepts=set(selected), cash_lines=selected,
                                         structure=broken, cash_importance='required')

    def test_filed_base_does_not_enter_new_history_or_predicate_path(self):
        inputs, spec, structure, replay, binding = authorities()
        from dalton_core import cash_flow_derived_base as helper
        with patch.object(helper, 'derived_base_is_forecastable', side_effect=AssertionError('old path')):
            body = build_structured_forecast_model(
                spec, inputs, structure=structure, replay=replay, binding=binding)
        record = body
        self.assertFalse(any(x['provenance']['rule_ref'] == CONTRACT_REF for x in record['assumptions']))
        # Golden captured from exact pre-change 31a4/base implementation.
        self.assertEqual(hashlib.sha256(canonical_json(body).encode()).hexdigest(),
                         '2ace18b49dac29819c7d2f25edb98f7819fe2d2f79f8cb14153da7f16fcad33a')

    def test_legacy_derived_definition_keeps_old_assumptions_and_builder_bytes(self):
        inputs, spec, structure, replay, binding = authorities()
        spec['cash_flow_companion']['lines'][0]['forecast_base_ref'] = 'net'
        from dalton_core import cash_flow_derived_base as helper
        with patch.object(helper, 'derived_base_is_forecastable', side_effect=AssertionError('legacy')):
            body = build_structured_forecast_model(
                spec, inputs, structure=structure, replay=replay, binding=binding)
        self.assertFalse(any(x['provenance']['rule_ref'] == CONTRACT_REF for x in body['assumptions']))
        # Legacy derived shares were never inferred by the filed-driver path.
        self.assertFalse(any(x['driver_ref'] == 'concept:us-gaap:NetCashProvidedByUsedInOperatingActivities'
                             for x in body['assumptions']))
        self.assertEqual(hashlib.sha256(canonical_json(body).encode()).hexdigest(),
                         '34e722e15394ace8b986bd51162ee814820d0c5d56974bf0a5d26b8327d17615')

    def test_zero_mixed_sign_or_unproved_operand_does_not_generate_a_share(self):
        from tests.test_structured_cash_flow_companion import QUARTERS
        cash = []
        cells = []
        values = {}
        refs = {}
        for start, end in QUARTERS:
            cell = {'concept': 'income:revenue', 'period_start': start,
                    'period_end': end, 'value': '100', 'accessions': ['original:1']}
            cells.append(cell)
            cash.append({**cell, 'concept': 'cash:operating', 'value': '10'})
            values[end] = Decimal('100')
            refs[end] = [{'kind': 'input_cell', 'ref': None,
                          'concept': 'income:revenue', 'period_end': end,
                          'accession': 'original:1'}]
        args = {'quarters': 4, 'rate_quant': Decimal('0.000000000001')}
        self.assertIsNotNone(derived_trailing_share(cash, values, refs, [{'history': cells}], **args))
        for wrong in ('zero', 'mixed_sign', 'wrong_accession'):
            with self.subTest(wrong=wrong):
                v = copy.deepcopy(values); r = copy.deepcopy(refs)
                end = QUARTERS[-1][1]
                if wrong == 'zero': v[end] = Decimal(0)
                elif wrong == 'mixed_sign': v[end] = -v[end]
                else: r[end][0]['accession'] = 'wrong:2'
                self.assertIsNone(derived_trailing_share(cash, v, r, [{'history': cells}], **args))

    def test_latest_missing_period_does_not_fall_back_to_earlier_complete_window(self):
        from datetime import date, timedelta
        from tests.test_structured_cash_flow_companion import QUARTERS
        ends = [end for _start, end in QUARTERS] + ['2026-03-31','2026-06-30','2026-09-30','2026-12-31']
        start = '2025-01-01'; cash = []; cells = []; values = {}; refs = {}
        for end in ends:
            cell = {'concept':'income:leaf','period_start':start,'period_end':end,
                    'value':'100','accessions':['original:1']}
            cash.append({**cell,'concept':'cash:ocf','value':'10'}); cells.append(cell)
            values[end] = Decimal(100)
            refs[end] = [{'kind':'input_cell','ref':None,'concept':'income:leaf',
                          'period_end':end,'accession':'original:1'}]
            start = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
        kwargs = {'quarters':4,'rate_quant':Decimal('0.000000000001')}
        good = derived_trailing_share(cash, values, refs, [{'history':cells}], **kwargs)
        self.assertEqual(good['first_period'], '2026-03-31')
        del values[ends[-1]]
        self.assertIsNone(derived_trailing_share(cash, values, refs, [{'history':cells}], **kwargs))

    def test_default_without_exact_structure_does_not_invent_a_derived_driver(self):
        inputs, spec, structure, _replay, _binding = derived_fixture()
        companion = build_cash_flow_companion(spec, inputs, structure)
        income = build_structure_drivers(inputs, structure)
        cash = build_cash_flow_companion_drivers(inputs, companion)
        periods = [{'start': '2026-01-01', 'end': '2026-03-31', 'kind': 'quarter', 'calendar': 'company:fiscal'}]
        assumptions = default_cash_flow_companion_assumptions(income, cash, periods, companion)
        self.assertFalse(any(x['driver_ref'] == cash[0]['ref'] for x in assumptions))
