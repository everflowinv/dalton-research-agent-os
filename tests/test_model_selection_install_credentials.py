"""An installed purpose selection must retain transport admission, not just policy text."""
import json
import unittest

import tests.test_cockpit_model_fallback as fixture
from dalton_core.cockpit_model import CockpitModel
from dalton_core.model_router import ModelRouter
from dalton_core.model_selection import publish_selection
from dalton_core.research_planner_setup import install, credential_slots_for


class ModelSelectionInstallCredentialTests(unittest.TestCase):
    setUp = fixture.CockpitChainTests.setUp
    _model = fixture.CockpitChainTests._model

    def test_reinstall_preserves_cross_tier_verifier_transport_admission(self):
        # Production initial-screen config is installed with the brain tier,
        # while its independent verifier was explicitly selected elsewhere.
        target = self.root / 'initial-screen-model-config.json'
        config = self._model(None, policy_version_ref=self.chain_policy).config
        target.write_text(json.dumps(config))
        with ModelRouter(self.router_db) as router:
            selected = publish_selection(
                router, policy_version_ref=self.chain_policy,
                purpose='debate_map_verifier', mode='explicit',
                chain=['profile:gemini-3-5-flash-lite'],
                actor_ref='operator:test', now=fixture.NOW,
            )
            policy = router.get_policy(selected['policy_version_ref'])
        service = self.root / 'service.json'
        service.write_text(json.dumps({
            'core_db': str(self.root / 'core.sqlite'),
            'model_router_db': str(self.router_db),
            'bounded_planner': {'config': {
                'planner_broker_socket': config['broker_socket'],
                'planner_broker_auth_key': config['broker_auth_key'],
                'planner_broker_client_id': config['broker_client_id'],
                'planner_expected_agent_id': config['expected_agent_id'],
            }},
            'thesis_impact': {'config': {
                'budget_db': config['budget_db'],
                'budget_policy_version_id': config['budget_policy_ref'],
            }},
        }))
        install(service, tier='brain', policy_id=policy['id'],
                config_file_name=target.name, now=fixture.NOW)
        regenerated = json.loads(target.read_text())
        self.assertIn('credential-slot:openclaw:google', regenerated['credential_slot_refs'])
        producer = self._model(fixture.ChainAdapter({}), policy_version_ref=self.chain_policy).call(
            purpose='plan', request_id='installed-producer', prompt='draft', mission=self.mission)
        adapter = fixture.ChainAdapter({})
        model = CockpitModel(regenerated, scheduler_db=self.root/'scheduler.sqlite',
                             adapter_factory=lambda router: adapter, clock=lambda: fixture.NOW,
                             max_output_tokens=500, max_cost_usd=.5)
        result = model.call(purpose='debate_map_verifier', request_id='installed-verifier',
                            prompt='verify', mission=self.mission,
                            producer_route_decision_refs=[producer['route_decision_ref']])
        self.assertEqual(adapter.served, ['profile:gemini-3-5-flash-lite'])
        self.assertIn('answered', result['text'])

    def test_no_policy_keeps_legacy_credential_scope(self):
        with ModelRouter(self.router_db) as router:
            self.assertEqual(credential_slots_for(router, ['profile:gpt-6-astra']),
                             ['credential-slot:openclaw:openai'])
