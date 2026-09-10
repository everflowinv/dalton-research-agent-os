"""Real Cockpit routing, scheduler and adapter must agree on verifier contracts."""
from __future__ import annotations

import unittest

import tests.test_cockpit_model_fallback as fixture
import tests.test_openclaw_model_adapter as broker_fixture
from dalton_core.model_router import ModelRouter
from dalton_core.openclaw_model_adapter import OpenClawModelAdapter
from dalton_core.scheduler import Scheduler


class CockpitProviderBoundaryTests(unittest.TestCase):
    setUp = fixture.CockpitChainTests.setUp
    _model = fixture.CockpitChainTests._model

    def test_argument_verifiers_cross_real_adapter_and_replay_without_transport(self):
        producer = self._model(
            fixture.ChainAdapter({}), policy_version_ref=self.chain_policy,
        ).call(purpose="plan", request_id="socket-producer", prompt="draft",
               mission=self.mission)
        for purpose in ("debate_map_verifier", "conviction_call_verifier"):
            with self.subTest(purpose=purpose):
                def respond(request):
                    response = broker_fixture.success_response(
                        request, text='{"verdict":"pass","findings":[]}')
                    with ModelRouter(self.router_db, read_only=True) as router:
                        profile = next(p for p in router.latest_profiles() if p["id"] == request["profileId"])
                    response.update(provider=profile["provider"], model=profile["model"],
                                    canonicalModel=f'{profile["provider"]}/{profile["model"]}')
                    response.pop("contentHash")
                    return broker_fixture.seal(response)

                broker = broker_fixture.FakeBroker(self.root, respond)
                self.addCleanup(broker.close)
                model = self._model(None, policy_version_ref=self.verifier_policy,
                                    slots=self.verifier_slots)
                model.adapter_factory = lambda router: OpenClawModelAdapter(
                    broker.path, route_resolver=router.get_decision,
                    auth_client_id=broker_fixture.AUTH_CLIENT_ID,
                    auth_key_provider=lambda: broker_fixture.AUTH_SECRET,
                    expected_agent_id="dalton-model-broker", timeout_seconds=2,
                )
                args = dict(purpose=purpose, request_id=f"socket-{purpose}",
                            prompt="Verify the cited draft", mission=self.mission,
                            producer_route_decision_refs=[producer["route_decision_ref"]])
                first = model.call(**args)
                self.assertEqual(first["text"], '{"verdict":"pass","findings":[]}')
                self.assertEqual(len(broker.requests), 1)
                structured = broker.requests[0]["requiredControls"]["structuredOutput"]
                self.assertEqual(structured["schemaName"], purpose + "_provider_output_v0_1")
                with Scheduler(self.root / "scheduler.sqlite") as scheduler:
                    authority = scheduler.work_order_authority(first["work_order_ref"])
                self.assertEqual(structured["schemaHash"], authority["work_order"]["metadata"][
                    "verifier_provider_schema_hash"])
                broker.close()
                again = model.call(**args)
                self.assertTrue(again["replayed"])
                self.assertEqual(again["text"], first["text"])
                self.assertEqual(len(broker.requests), 1)
