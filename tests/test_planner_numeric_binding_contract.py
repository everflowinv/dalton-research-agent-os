"""The planner must copy rejection identity, never synthesize admission authority."""
from __future__ import annotations

import copy
import json
import unittest

from dalton_core.research_planner import (
    OUTPUT_SCHEMA, ResearchPlanError, build_prompt, plan_from_response,
    project_state_for_prompt,
)
from tests import test_planner_numeric_requirements as fixture
from tests import test_research_planner as planner_fixture


class PlannerNumericBindingContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.NumericRequirementTests()
        self.fixture.setUp()

    def test_prompt_and_schema_name_exact_nested_copy_contract(self):
        prompt = build_prompt(self.fixture.state)
        self.assertIn('directed_document.replaces_numeric_rejection', prompt)
        self.assertIn('not inquiry top level or numeric_targets', prompt)
        self.assertIn('missing, null or guessed binding invalidates the entire plan', prompt)
        self.assertIn('never borrow another requirement', prompt)
        marker = '{"admission_ref":"<copy>","admission_hash":"<copy>","rejection_ref":"<copy>","rejection_hash":"<copy>"}'
        self.assertIn(marker, prompt)
        schema = OUTPUT_SCHEMA['properties']['inquiries']['items']['properties']['directed_document']['properties']['replaces_numeric_rejection']
        self.assertIn('copy its entire four-field object exactly', schema['description'])
        self.assertEqual(set(schema['required']), set(self.fixture.binding))

    def test_successful_transport_output_with_omitted_binding_is_still_refused(self):
        # Mirrors the actual IBM output: numeric strategy/targets and exact source
        # all present, but the four-field authority object was omitted.
        reply = json.loads(self.fixture.reply(numeric=True))
        with self.assertRaisesRegex(ResearchPlanError, 'inquiry 0:.*exact rejection binding'):
            plan_from_response(self.fixture.state, json.dumps(reply), created_at=planner_fixture.NOW)
        reply['inquiries'][0]['directed_document']['replaces_numeric_rejection'] = copy.deepcopy(self.fixture.binding)
        planned = plan_from_response(self.fixture.state, json.dumps(reply), created_at=planner_fixture.NOW)
        self.assertEqual(planned['inquiries'][0]['directed_document']['replaces_numeric_rejection'], self.fixture.binding)
        self.assertEqual(planned['inquiries'][0]['directed_document']['document_version_hash'], self.fixture.doc['document_version_hash'])

    def test_copy_contract_does_not_authorize_null_wrong_location_or_foreign_binding(self):
        for variant in ('null', 'top_level', 'guessed_hash', 'foreign_source'):
            with self.subTest(variant=variant):
                reply = json.loads(self.fixture.reply(numeric=True, binding=self.fixture.binding))
                inquiry = reply['inquiries'][0]
                strategy = inquiry['directed_document']
                if variant == 'null':
                    strategy['replaces_numeric_rejection'] = None
                elif variant == 'top_level':
                    inquiry['replaces_numeric_rejection'] = strategy.pop('replaces_numeric_rejection')
                elif variant == 'guessed_hash':
                    strategy['replaces_numeric_rejection']['rejection_hash'] = 'd' * 64
                else:
                    strategy['document_version_hash'] = 'd' * 64
                with self.assertRaises(ResearchPlanError):
                    plan_from_response(self.fixture.state, json.dumps(reply), created_at=planner_fixture.NOW)

    def test_budgeted_projection_retains_both_authority_and_copy_instruction(self):
        helper = planner_fixture.FeedbackProjectionTests()
        state = helper.worst_case(4)
        for company in state['companies']:
            company['document_research_feedback'] = [
                {**self.fixture.obs, 'company_ref': company['company_ref']},
                *company['document_research_feedback'],
            ]
            company['numeric_reading_requirements'] = fixture.numeric_requirements(company['document_research_feedback'])
            company['readable_documents'].append({**self.fixture.doc, 'doc_date': '2020-01-01'})
        state = helper.rebind(state)
        projected = project_state_for_prompt(state, max_input_bytes=64000)
        self.assertEqual(len(projected['companies']), 4)
        self.assertLessEqual(len(build_prompt(projected).encode()), 64000)
        self.assertIn('directed_document.replaces_numeric_rejection', build_prompt(projected))
        for company in projected['companies']:
            self.assertEqual(company['numeric_reading_requirements']['requirements'][0]['replaces_numeric_rejection'], self.fixture.binding)
