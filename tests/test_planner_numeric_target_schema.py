import copy
import re
import unittest

from dalton_core.directed_numeric_research import (
    CONTRACT_REF, DirectedNumericError, normalize_targets, numeric_target_schema,
)
from dalton_core.document_numeric_claim import ALLOWED_BASES
from dalton_core.document_numeric_unit_contracts import numeric_units
from dalton_core.research_planner import OUTPUT_SCHEMA


class PlannerNumericTargetSchemaTests(unittest.TestCase):
    def setUp(self):
        self.target = {
            'target_ref': 'target:contract-value', 'metric_ref': 'metric:contract-value',
            'metric_definition': {'label': 'Contract value', 'meaning': 'Remaining contracted revenue in the original note.'},
            'period': 'FY2025', 'unit': 'currency', 'currency': 'USD', 'basis': 'management-reported',
        }

    def test_planner_exposes_admission_vocabulary_including_duration(self):
        held = OUTPUT_SCHEMA['properties']['inquiries']['items']['properties']['directed_document']['properties']['numeric_targets']
        self.assertEqual(held, numeric_target_schema())
        props = held['items']['properties']
        self.assertEqual(props['unit']['enum'], list(numeric_units(CONTRACT_REF)))
        self.assertEqual(props['basis']['enum'], list(ALLOWED_BASES))
        for unit in numeric_units(CONTRACT_REF):
            value = [{**self.target, 'unit': unit, 'currency': 'USD' if unit == 'currency' else None}]
            self.assertEqual(normalize_targets(value)[0]['unit'], unit)

    def test_actual_failure_shapes_are_disclosed_and_still_refused(self):
        schema = numeric_target_schema()['items']
        props = schema['properties']
        self.assertIsNone(re.fullmatch(props['metric_ref']['pattern'], 'remaining_performance_obligations'))
        self.assertNotIn('usd', props['unit']['enum'])
        self.assertNotIn('10-K revenue note', props['basis']['enum'])
        self.assertEqual(schema['else']['properties']['currency']['type'], 'null')
        self.assertEqual(schema['then']['properties']['currency']['pattern'], '^[A-Z]{3}$')
        for changes in ({'metric_ref': 'remaining_performance_obligations'},
                        {'unit': 'usd'}, {'basis': '10-K revenue note'},
                        {'unit': 'years', 'currency': 'USD'}, {'currency': None},
                        {'currency': 'usd'}):
            with self.subTest(changes=changes):
                value = [{**self.target, **changes}]
                with self.assertRaises(DirectedNumericError):
                    normalize_targets(value)

    def test_schema_is_fresh_and_does_not_supply_a_financial_answer(self):
        schema = numeric_target_schema()
        schema['items']['properties']['unit']['enum'].append('usd')
        self.assertNotIn('usd', numeric_target_schema()['items']['properties']['unit']['enum'])
        value = [copy.deepcopy(self.target)]
        self.assertNotIn('value', normalize_targets(value)[0])


if __name__ == '__main__':
    unittest.main()
