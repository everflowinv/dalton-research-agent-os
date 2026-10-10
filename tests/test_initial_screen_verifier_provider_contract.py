"""Independent screens bind exact packaged output controls before transport."""
import json
import unittest
from dataclasses import replace
from importlib import resources

import dalton_core.initial_screen_cli  # registers the actual lane purposes
from dalton_core.cockpit_model import build_work, _verifier_provider_contract
from dalton_core.initial_screen import STRL_VERIFIER_FINDING_CODES
from dalton_core.openclaw_model_adapter import (
    ModelAdmissionError, _required_provider_controls,
    PROVIDER_CONTROL_MODE_REQUIRED,
)


class ProviderContractTests(unittest.TestCase):
    def test_exact_schema_controls_and_wrong_hash_version_or_purpose_refusal(self):
        ref, digest = _verifier_provider_contract('initial_screen_verifier')
        work=build_work(purpose='initial_screen_verifier',request_id='synthetic-schema',
            prompt='verify',mission_version_ref='mission-version:synthetic:1',
            max_input_tokens=1000,max_output_tokens=100,max_cost_usd=.2,max_seconds=30,
            verifier_provider_contract=ref,verifier_provider_schema_hash=digest)
        route={'constraints':{'producer_family':'synthetic-author'}}
        controls=_required_provider_controls(work,route,100,PROVIDER_CONTROL_MODE_REQUIRED)
        self.assertEqual(controls['structuredOutput']['schemaHash'],digest)
        schema=controls['structuredOutput']['jsonSchema']
        item=schema['properties']['findings']['items']
        self.assertEqual(set(item['required']),{'code','section','detail'})
        self.assertFalse(item['additionalProperties'])
        self.assertEqual(set(item['properties']['code']['enum']),set(STRL_VERIFIER_FINDING_CODES))
        for change in [{'verifier_provider_schema_hash':'0'*64},
                       {'verifier_output_schema_version':'0.2'},
                       {'purpose':'quality_verifier'},
                       {'verifier_provider_contract':'caller-contract'}]:
            with self.subTest(change=change),self.assertRaises(ModelAdmissionError):
                _required_provider_controls(replace(work,metadata={**work.metadata,**change}),route,100,PROVIDER_CONTROL_MODE_REQUIRED)

    def test_semantic_parser_rejects_inconsistent_or_untyped_findings(self):
        from dalton_core.initial_screen import parse_verifier_output, InitialScreenContractError
        finding={'code':'contradiction','section':'forecast','detail':'EPS definitions differ'}
        self.assertEqual(parse_verifier_output({'verdict':'reject','findings':[finding]})['verdict'],'reject')
        for value in [{'verdict':'pass','findings':[finding]},
                      {'verdict':'reject','findings':[]},
                      {'verdict':'reject','findings':[{**finding,'untrusted':True}]},
                      {'verdict':'reject','findings':[{**finding,'detail':''}]},
                      {'verdict':'reject','findings':[{**finding,'detail':' \t '}]},
                      {'verdict':'reject','findings':[{'code':'contradiction'}]}]:
            with self.subTest(value=value),self.assertRaises(InitialScreenContractError):parse_verifier_output(value)

    def test_both_cockpit_contracts_are_packaged_closed_shapes(self):
        for name in ['initial-screen','street-estimate']:
            schema=json.loads(resources.files('dalton_core').joinpath(name+'-verifier-provider-output-v0.1.schema.json').read_text())
            self.assertFalse(schema['additionalProperties'])
            self.assertEqual(set(schema['required']),set(schema['properties']))
            # Google's installed schema projection lacks string length keywords;
            # local semantic readers still reject empty and whitespace-only text.
            def keywords(node):
                if isinstance(node,dict):
                    self.assertNotIn('minLength',node)
                    for value in node.values():keywords(value)
                elif isinstance(node,list):
                    for value in node:keywords(value)
            keywords(schema)
