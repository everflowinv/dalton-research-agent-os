"""Synthetic admission binding; Unix fake broker only, no provider transport."""
import json
import unittest
from unittest.mock import patch

from dalton_core.contracts import WorkOrder
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.openclaw_model_adapter import ModelAdmissionError, OpenClawModelAdapter
from dalton_core.store import content_hash
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from tests.test_openclaw_model_adapter import (
    AUTH_CLIENT_ID, AUTH_SECRET, FIXED_NOW, FakeBroker, success_response,
)
from tests import test_openclaw_model_adapter as adapter_fixture
from tests import test_cockpit_model_fallback as cockpit_fixture


class ControlledReservationTests(unittest.TestCase):
    def setUp(self):
        self.h = adapter_fixture.OpenClawModelAdapterTests()
        self.h.setUp()
        self.addCleanup(self.h.tearDown)
        self.work = WorkOrder.from_dict({**self.h.work.to_dict(),
            "id": "work:controlled-reservation", "requested_capabilities": ["verify"],
            "metadata": {"verifier_output_schema_version": "0.2",
                         "verifier_decision_schema_version": "0.1",
                         "verifier_binding_mode": "wrapper-owned-v1"}})
        self.route = self.h.router.route(self.work, attempt_number=1, capability="verify",
            policy_version_ref="model-routing-policy-version:default:1",
            credential_slot_refs=["credential-slot:openai:dalton"],
            required_modalities=["text"], required_context_tokens=1000,
            estimated_input_tokens=500, estimated_output_tokens=250,
            producer_family="anthropic-claude", idempotency_key="reservation-route")["decision"]

    def binding(self, micros=20000, work=None):
        return {"admission_id": "thesis-impact-admission:synthetic",
                "work_order_ref": (work or self.work).id,
                "reserved_micros": micros, "content_hash": "a" * 64}

    def run_bound(self, binding, responder=success_response, *, work=None, route=None):
        broker = FakeBroker(self.h.directory, responder)
        self.addCleanup(broker.close)
        adapter = OpenClawModelAdapter(broker.path, route_resolver=self.h.router.get_decision,
            auth_client_id=AUTH_CLIENT_ID, auth_key_provider=lambda: AUTH_SECRET,
            provider_cost_admission=binding, timeout_seconds=1, clock=lambda: FIXED_NOW)
        result = adapter.execute(work or self.work, route or self.route, self.h.profile)
        return result, broker

    def test_actual_admission_bounds_controls_without_changing_work_or_route(self):
        before = content_hash(self.work.to_dict()), content_hash(self.route)
        (invocation, result), broker = self.run_bound(self.binding())
        self.assertEqual(broker.requests[0]["requiredControls"]["maxCostUsd"], .02)
        self.assertEqual(broker.requests[0]["workOrderId"], self.work.id)
        self.assertEqual(content_hash(self.work.to_dict()), before[0])
        self.assertEqual(content_hash(self.route), before[1])
        self.assertEqual(result.metadata["provider_cost_admission"], self.binding())
        self.assertEqual(result.status, "succeeded")

    def test_smaller_owner_cap_cannot_be_raised_by_admission(self):
        work = WorkOrder.from_dict({**self.work.to_dict(), "id": "work:smaller-owner",
            "budget": {**self.work.budget, "max_cost_usd": .01}})
        route = self.h.router.route(work, attempt_number=1, capability="verify",
            policy_version_ref="model-routing-policy-version:default:1",
            credential_slot_refs=["credential-slot:openai:dalton"],
            required_modalities=["text"], required_context_tokens=1000,
            estimated_input_tokens=500, estimated_output_tokens=250,
            producer_family="anthropic-claude", idempotency_key="small-owner-route")["decision"]
        (_, result), broker = self.run_bound(self.binding(20000, work),
            lambda r: success_response(r, cost={"available": True, "usd": .001}), work=work, route=route)
        self.assertEqual(broker.requests[0]["requiredControls"]["maxCostUsd"], .01)
        self.assertEqual(result.status, "succeeded")

    def test_known_actual_cost_over_reservation_is_kept_in_failed_contract(self):
        (invocation, result), _ = self.run_bound(self.binding(),
            lambda r: success_response(r, cost={"available": True, "usd": .03}))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error["code"], "PROVIDER_BUDGET_EXCEEDED")
        self.assertEqual(invocation.usage["raw_provider_telemetry"]["cost"]["usd"], .03)
        self.assertEqual(invocation.usage["input_tokens"], 100)
        self.assertTrue(result.usage_refs)
        self.assertIsNone(result.metadata["dispatch_proof"])

    def test_binding_for_another_work_is_refused_before_send(self):
        binding = self.binding();binding["work_order_ref"] = "work:other"
        adapter = OpenClawModelAdapter(self.h.directory / "never.sock",
            route_resolver=self.h.router.get_decision, auth_client_id=AUTH_CLIENT_ID,
            auth_key_provider=lambda: AUTH_SECRET, provider_cost_admission=binding,
            clock=lambda: FIXED_NOW)
        with patch("dalton_core.openclaw_model_adapter.socket.socket") as transport:
            with self.assertRaisesRegex(ModelAdmissionError, "another WorkOrder"):
                adapter.execute(self.work, self.route, self.h.profile)
            transport.assert_not_called()

    def test_injected_real_adapter_binding_is_a_request_local_copy(self):
        adapter = OpenClawModelAdapter(self.h.directory / "never.sock",
            route_resolver=self.h.router.get_decision, auth_client_id=AUTH_CLIENT_ID,
            auth_key_provider=lambda: AUTH_SECRET)
        local = adapter.with_provider_cost_admission(self.binding())
        self.assertIsNot(local, adapter)
        self.assertIsNone(adapter._provider_cost_admission)
        self.assertEqual(local._provider_cost_admission, self.binding())

    def test_invalid_or_zero_binding_does_not_create_adapter(self):
        for micros in (0, -1, True, .01):
            with self.subTest(micros=micros), self.assertRaises(ValueError):
                OpenClawModelAdapter(self.h.directory / "never.sock",
                    route_resolver=self.h.router.get_decision, auth_client_id=AUTH_CLIENT_ID,
                    auth_key_provider=lambda: AUTH_SECRET, provider_cost_admission=self.binding(micros))


class CockpitAdmissionWiringTests(unittest.TestCase):
    def setUp(self):
        self.h = cockpit_fixture.CockpitChainTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def test_direct_and_chain_pass_the_persisted_admission_to_adapter(self):
        for chained in (False, True):
            with self.subTest(chained=chained):
                adapter = cockpit_fixture.ChainAdapter({})
                model = self.h._model(adapter, policy_version_ref=(self.h.chain_policy if chained else self.h.pinned_policy))
                original = model._adapter;captured=[]
                def observe(router, **kwargs):
                    captured.append(kwargs["provider_cost_admission"])
                    return original(router, **kwargs)
                model._adapter = observe
                answer = model.call(purpose="plan", request_id="reservation-wiring:"+str(chained),
                                    prompt="bounded synthetic request", mission=self.h.mission)
                with ThesisImpactBudgetStore(self.h.root / "budget.sqlite") as budget:
                    row = budget.connection.execute("SELECT record_json FROM thesis_impact_day_admissions WHERE work_order_ref=?", (answer["work_order_ref"],)).fetchone()
                persisted = json.loads(row[0])
                self.assertEqual(captured[0]["reserved_micros"], persisted["reserved_micros"])
                self.assertEqual(captured[0]["content_hash"], persisted["content_hash"])

    def test_chain_known_overrun_settles_actual_cost_without_hiding_usage(self):
        self._known_overrun(chained=True)

    def test_direct_known_overrun_settles_actual_cost_without_hiding_usage(self):
        self._known_overrun(chained=False)

    def test_free_fallback_then_known_overrun_keeps_full_violation_cost(self):
        self._known_overrun(chained=True, free_first=True)

    def _known_overrun(self, *, chained, free_first=False):
        class ViolatingAdapter(cockpit_fixture.ChainAdapter):
            def execute(inner, work, route, profile):
                if free_first and profile["id"] == "profile:gpt-6-astra":
                    from dalton_core.openclaw_model_adapter import BrokerDefinitelyNotSent
                    inner.served.append(profile["id"])
                    raise BrokerDefinitelyNotSent("synthetic proved pre-send refusal")
                invocation, envelope = super().execute(work, route, profile)
                from dataclasses import replace
                invocation = replace(invocation, usage={"input_tokens": 100,
                    "raw_provider_telemetry": {"cost": {"available": True, "usd": 2.0}}})
                envelope = replace(envelope, status="failed", outputs={},
                    error={"code": "PROVIDER_BUDGET_EXCEEDED", "message": "reservation violated"},
                    usage_refs=("usage:"+invocation.id,))
                return invocation, envelope
        adapter = ViolatingAdapter({})
        model = self.h._model(adapter, policy_version_ref=(self.h.chain_policy if chained else self.h.pinned_policy))
        with self.assertRaises(CockpitModelError):
            model.call(purpose="plan", request_id="known-admission-overrun", prompt="short", mission=self.h.mission)
        with ThesisImpactBudgetStore(self.h.root / "budget.sqlite") as budget:
            row = budget.connection.execute("SELECT a.reserved_micros,s.actual_micros FROM thesis_impact_day_admissions a JOIN thesis_impact_day_settlements s USING(admission_id)").fetchone()
        self.assertEqual(row[1], 2000000)
        self.assertLess(row[0], row[1])
        from dalton_core.scheduler import Scheduler
        with Scheduler(self.h.root / "scheduler.sqlite") as scheduler:
            row = scheduler.connection.execute("SELECT result_envelope_json FROM scheduler_formal_results").fetchone()
        envelope = json.loads(row[0])
        self.assertEqual(envelope["error"]["code"], "PROVIDER_BUDGET_EXCEEDED")
        self.assertTrue(envelope["usage_refs"])
        if free_first:
            self.assertEqual(adapter.served, ["profile:gpt-6-astra", "profile:claude-fable-5-1"])


if __name__ == "__main__":
    unittest.main()
