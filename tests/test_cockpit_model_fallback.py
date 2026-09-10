"""P14-M: a cockpit-shaped call walks its tier's chain when the policy pins one."""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

# Importing the lane is what registers its purpose, exactly as the lane's own
# child process does before it builds a WorkOrder.
import dalton_core.claim_index_tagging  # noqa: F401
import dalton_core.event_judgement  # noqa: F401
from dalton_core.cockpit_model import (
    CockpitModel,
    CockpitModelError,
    build_work,
    independent_model_call,
)
from dalton_core.contracts import ModelInvocation, ResultEnvelope
from dalton_core.model_fallback_chain import register_purpose_tier, tier_chain
from dalton_core.model_router import ModelRouter
from dalton_core.openclaw_catalog_reconcile import sync_openclaw_model_catalog
from dalton_core.research_planner_setup import credential_slots_for, ensure_planner_policy
from dalton_core.scheduler import Scheduler
from dalton_core.store import content_hash
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from tests.test_openclaw_catalog_reconcile import _config


NOW = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)
BUDGET_POLICY = "thesis-impact-day-budget-policy:p14m:1"


class ChainAdapter:
    """A broker that fails some profiles and answers for the rest."""

    def __init__(self, script: dict[str, dict]) -> None:
        self.script = script
        self.served: list[str] = []

    def execute(self, work, route, profile):
        self.served.append(profile["id"])
        scripted = self.script.get(profile["id"])
        if isinstance(scripted, BaseException):
            raise scripted
        moment = NOW.isoformat(timespec="microseconds")
        invocation = ModelInvocation(
            schema_version="0.1",
            id=f"invocation:p14m-{content_hash({'w': work.id, 'p': profile['id']})[:32]}",
            created_at=moment,
            work_order_ref=work.id,
            profile_ref=profile["profile_version_ref"],
            granularity="work_order",
            capability=route["capability"],
            provider=profile["provider"],
            model=profile["model"],
            model_family=profile["family"],
            input_refs=(),
            output_refs=(),
            started_at=moment,
            completed_at=moment,
            # No provider telemetry, so the cost falls back to this route
            # decision's own estimate for this exact profile version -- which
            # is the point of the assertion in the budget test.
            usage={},
            side_effects=(),
            runtime_ref="runtime:openclaw-model-broker",
            actor_ref="worker:cockpit-model:0.1",
        )
        failed = isinstance(scripted, dict)
        return invocation, ResultEnvelope(
            schema_version="0.1",
            id=f"result:p14m-{content_hash({'w': work.id, 'p': profile['id']})[:32]}",
            created_at=moment,
            work_order_ref=work.id,
            invocation_ref=invocation.id,
            status="failed" if failed else "succeeded",
            outputs={} if failed else {"text": f"answered by {profile['id']}"},
            actual_side_effects=(),
            usage_refs=(),
            artifact_refs=(),
            error=scripted if failed else None,
            metadata={"route_decision_ref": route["id"]},
        )


class CockpitChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.router_db = self.root / "router.sqlite"
        with ModelRouter(self.router_db) as router:
            sync_openclaw_model_catalog(router, _config(), checked_at=NOW)
            self.chain_policy = ensure_planner_policy(
                router, tier="brain", now=NOW,
                policy_id="model-routing-policy:p14m-cockpit-brain",
            )["policy_version_ref"]
            self.cheap_policy = ensure_planner_policy(
                router, tier="cheap", now=NOW,
                policy_id="model-routing-policy:p14m-cockpit-cheap",
            )["policy_version_ref"]
            self.cheap_slots = credential_slots_for(router, list(tier_chain("cheap")))
            register_purpose_tier("p14m_route_verify", "verifier")
            self.verifier_policy = ensure_planner_policy(
                router, tier="verifier", now=NOW,
                policy_id="model-routing-policy:p14m-cockpit-verifier",
            )["policy_version_ref"]
            self.verifier_slots = credential_slots_for(
                router, list(tier_chain("verifier")))
            self.pinned_policy = ensure_planner_policy(
                router, profile_ids=["profile:gpt-6-astra"], now=NOW,
                policy_id="model-routing-policy:p14m-cockpit-pinned",
            )["policy_version_ref"]
            self.slots = credential_slots_for(router, list(tier_chain("brain")))
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as budget:
            budget.register_policy(
                policy_version_id=BUDGET_POLICY, day_cap_micros=5_000_000
            )
        self.mission = {
            "mission_ref": "coverage-mission:p14m",
            "id": "coverage-mission-version:p14m:1",
            "content_hash": content_hash({"mission": "p14m"}),
            "created_at": NOW.isoformat(timespec="microseconds"),
            "budget": {"max_daily_paid_calls": 50, "max_daily_cost_usd": 5.0},
        }

    def _model(self, adapter: ChainAdapter, *, policy_version_ref: str,
               slots: list[str] | None = None) -> CockpitModel:
        config = {
            "routing_policy_ref": policy_version_ref,
            "credential_slot_refs": list(slots if slots is not None else self.slots),
            "model_router_db": str(self.router_db),
            "broker_socket": str(self.root / "none.sock"),
            "broker_auth_key": str(self.root / "none.key"),
            "broker_client_id": "client:dalton-core",
            "expected_agent_id": "chem",
            "budget_db": str(self.root / "budget.sqlite"),
            "budget_policy_ref": BUDGET_POLICY,
        }
        return CockpitModel(
            config, scheduler_db=str(self.root / "scheduler.sqlite"),
            adapter_factory=lambda router: adapter, clock=lambda: NOW,
            # Brain-tier models are 50 USD per million output tokens, so the
            # cockpit default of 3,000 tokens at a 0.05 USD cap is refused by
            # the WorkOrder budget before any of this is reached.
            max_output_tokens=500, max_cost_usd=0.5,
        )

    def _links(self) -> list[dict]:
        with ModelRouter(self.router_db, read_only=True) as router:
            return router.chain_links()

    def _decisions(self) -> list[dict]:
        with ModelRouter(self.router_db, read_only=True) as router:
            return router.list_decisions()

    def test_a_failed_first_link_falls_back_inside_the_same_attempt(self) -> None:
        adapter = ChainAdapter({
            "profile:gpt-6-astra": {"code": "PROVIDER_OVERLOADED", "message": "no capacity"}
        })
        answer = self._model(adapter, policy_version_ref=self.chain_policy).call(
            purpose="plan", request_id="one", prompt="what next?", mission=self.mission,
        )
        self.assertEqual(answer["text"], "answered by profile:claude-fable-5-1")
        self.assertEqual(
            adapter.served, ["profile:gpt-6-astra", "profile:claude-fable-5-1"]
        )
        links = self._links()
        self.assertEqual(
            [(link["chain_position"], link["served"], link["skip_reason"]) for link in links],
            [(1, False, "model_unavailable"), (2, True, None)],
        )
        self.assertEqual({link["attempt_number"] for link in links}, {1})
        self.assertEqual({link["tier"] for link in links}, {"brain"})
        self.assertEqual({link["purpose"] for link in links}, {"plan"})
        decisions = self._decisions()
        self.assertEqual([item["decision_kind"] for item in decisions], ["initial", "switch"])
        self.assertEqual(answer["route_decision_ref"], decisions[1]["id"])

    def test_independent_call_fails_closed_and_keeps_legacy_fakes_compatible(self) -> None:
        class Legacy:
            def __init__(self):
                self.calls = 0

            def call(self, *, purpose, request_id, prompt, mission):
                self.calls += 1
                return {"text": "ok"}

        fake = Legacy()
        with self.assertRaisesRegex(CockpitModelError, "every producer"):
            independent_model_call(
                fake, producer_route_decision_refs=(), purpose="plan",
                request_id="missing", prompt="x", mission=self.mission)
        self.assertEqual(fake.calls, 0)
        self.assertEqual(
            independent_model_call(
                fake, producer_route_decision_refs=["route:producer"],
                purpose="plan", request_id="valid", prompt="x",
                mission=self.mission)["text"],
            "ok",
        )
        self.assertEqual(fake.calls, 1)

        class BrokenLegacy(Legacy):
            def call(self, *, purpose, request_id, prompt, mission):
                raise TypeError("inside fake")

        with self.assertRaisesRegex(TypeError, "inside fake"):
            independent_model_call(
                BrokenLegacy(), producer_route_decision_refs=["route:producer"],
                purpose="plan", request_id="broken", prompt="x",
                mission=self.mission)

    def test_verifier_uses_the_producers_served_family_before_buying_a_call(self) -> None:
        producer_adapter = ChainAdapter({
            "profile:gpt-6-astra": {
                "code": "PROVIDER_OVERLOADED", "message": "no capacity"
            }
        })
        producer = self._model(
            producer_adapter, policy_version_ref=self.chain_policy
        ).call(purpose="plan", request_id="producer-fallback", prompt="draft",
               mission=self.mission)
        self.assertEqual(producer_adapter.served[-1], "profile:claude-fable-5-1")

        verifier_adapter = ChainAdapter({})
        verified = self._model(
            verifier_adapter, policy_version_ref=self.verifier_policy,
            slots=self.verifier_slots,
        ).call(
            purpose="p14m_route_verify", request_id="same-verification",
            prompt="verify", mission=self.mission,
            producer_route_decision_refs=[producer["route_decision_ref"]],
        )
        self.assertEqual(verified["text"], "answered by profile:zai-glm-5-3")
        self.assertEqual(verifier_adapter.served, ["profile:zai-glm-5-3"])

        # Producer evidence is part of the WorkOrder identity. The same
        # request and prompt with a different producer cannot replay this one.
        openai = self._model(
            ChainAdapter({}), policy_version_ref=self.chain_policy
        ).call(purpose="plan", request_id="producer-openai", prompt="draft",
               mission=self.mission)
        another = ChainAdapter({})
        second = self._model(
            another, policy_version_ref=self.verifier_policy,
            slots=self.verifier_slots,
        ).call(
            purpose="p14m_route_verify", request_id="same-verification",
            prompt="verify", mission=self.mission,
            producer_route_decision_refs=[openai["route_decision_ref"]],
        )
        self.assertFalse(second["replayed"])
        self.assertEqual(another.served, ["profile:claude-fable-5-1"])

    def test_event_verifier_work_order_binds_provider_contract_into_identity(self) -> None:
        producer = self._model(
            ChainAdapter({}), policy_version_ref=self.chain_policy
        ).call(purpose="event_judgement", request_id="event-provider-contract",
               prompt="draft", mission=self.mission)
        verifier = self._model(
            ChainAdapter({}), policy_version_ref=self.verifier_policy,
            slots=self.verifier_slots,
        ).call(
            purpose="event_judgement_verifier", request_id="event-provider-contract",
            prompt='{"verdict":"pass","findings":[]}', mission=self.mission,
            producer_route_decision_refs=[producer["route_decision_ref"]],
        )
        with Scheduler(self.root / "scheduler.sqlite") as scheduler:
            stored = scheduler.work_order_authority(verifier["work_order_ref"])
        metadata = stored["work_order"]["metadata"]
        self.assertEqual(metadata["verifier_output_schema_version"], "0.1")
        self.assertEqual(metadata["verifier_provider_contract"],
                         "event-judgement-verifier-provider-output-0.1")
        legacy = build_work(
            purpose="event_judgement_verifier", request_id="event-provider-contract",
            prompt='{"verdict":"pass","findings":[]}',
            mission_version_ref=self.mission["id"], max_input_tokens=120_000,
            max_output_tokens=500, max_cost_usd=0.5, max_seconds=120,
            created_at=self.mission["created_at"],
        )
        self.assertNotEqual(verifier["work_order_ref"], legacy.id)

    def test_unknown_producer_route_fails_before_adapter_or_budget_charge(self) -> None:
        adapter = ChainAdapter({})
        with self.assertRaisesRegex(CockpitModelError, "could not prove"):
            self._model(
                adapter, policy_version_ref=self.verifier_policy,
                slots=self.verifier_slots,
            ).call(
                purpose="p14m_route_verify", request_id="unknown-producer",
                prompt="verify", mission=self.mission,
                producer_route_decision_refs=["model-route-decision:missing"],
            )
        self.assertEqual(adapter.served, [])
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as ledger:
            count = ledger.connection.execute(
                "SELECT COUNT(*) FROM thesis_impact_day_admissions"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_mixed_known_and_unclassified_producers_fail_before_charge(self) -> None:
        adapter = ChainAdapter({})
        with patch(
            "dalton_core.model_fallback_chain.served_family",
            side_effect=["openai-gpt-6", "unclassified:deepseek"],
        ), self.assertRaisesRegex(CockpitModelError, "verifier_not_independent"):
            self._model(
                adapter, policy_version_ref=self.pinned_policy,
                slots=["credential-slot:openclaw:openai"],
            ).call(
                purpose="p14m_route_verify", request_id="mixed-producers",
                prompt="verify", mission=self.mission,
                producer_route_decision_refs=["route:known", "route:unknown"],
            )
        self.assertEqual(adapter.served, [])
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as ledger:
            count = ledger.connection.execute(
                "SELECT COUNT(*) FROM thesis_impact_day_admissions"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_the_budget_is_charged_for_the_model_that_actually_ran(self) -> None:
        # The cheap chain, because its links have genuinely different rate cards:
        # deepseek-v4-flash is 0.22/0.66 and glm-5.3-flash 0.075/0.25, so "which
        # model was charged for" is a question with two different answers.
        adapter = ChainAdapter({
            "profile:deepseek-v4-flash": {"code": "UPSTREAM_TIMEOUT", "message": "gone"}
        })
        answer = self._model(
            adapter, policy_version_ref=self.cheap_policy, slots=self.cheap_slots
        ).call(
            purpose="claim_index", request_id="two", prompt="tag these",
            mission=self.mission,
        )
        self.assertEqual(answer["text"], "answered by profile:zai-glm-5-3-flash")
        decisions = self._decisions()
        served = next(
            item for item in decisions[1]["candidate_snapshot"]
            if item["profile_version_ref"] == decisions[1]["selected_profile_version_ref"]
        )
        first = next(
            item for item in decisions[0]["candidate_snapshot"]
            if item["profile_version_ref"] == decisions[0]["selected_profile_version_ref"]
        )
        self.assertNotEqual(first["estimated_cost_usd"], served["estimated_cost_usd"])
        expected = round(float(served["estimated_cost_usd"]) * 1_000_000)
        self.assertEqual(answer["cost_micros"], expected)
        self.assertEqual(answer["cost_status"], "estimated")
        # And the day ledger settled that same number, not the first link's.
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as ledger:
            settled = [
                json.loads(row["record_json"])["actual_micros"]
                for row in ledger.connection.execute(
                    "SELECT record_json FROM thesis_impact_day_settlements"
                )
            ]
        self.assertEqual(settled, [expected])

    def test_reservation_covers_expensive_fallback_in_owner_selected_chain(self) -> None:
        from dalton_core.model_selection import publish_selection

        links = ["profile:deepseek-v4-flash", "profile:gpt-6-astra"]
        with ModelRouter(self.router_db) as router:
            pinned = ensure_planner_policy(
                router, profile_ids=links, now=NOW,
                policy_id="model-routing-policy:selected-budget",
            )["policy_version_ref"]
            selected = publish_selection(
                router, policy_version_ref=pinned, purpose="claim_index",
                mode="explicit", chain=links, now=NOW,
            )["policy_version_ref"]
            slots = credential_slots_for(router, links)
        adapter = ChainAdapter({links[0]: {"code": "UPSTREAM_TIMEOUT", "message": "gone"}})
        answer = self._model(adapter, policy_version_ref=selected, slots=slots).call(
            purpose="claim_index", request_id="selected-reservation", prompt="tag these",
            mission=self.mission,
        )
        self.assertEqual(adapter.served, links)
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as ledger:
            admissions = [json.loads(row[0]) for row in ledger.connection.execute(
                "SELECT record_json FROM thesis_impact_day_admissions"
            )]
        self.assertEqual(len(admissions), 1)
        self.assertGreaterEqual(admissions[0]["reserved_micros"], answer["cost_micros"])

    def test_tier_choice_on_legacy_pin_keeps_the_single_profile(self) -> None:
        from dalton_core.model_selection import publish_selection

        with ModelRouter(self.router_db) as router:
            selected = publish_selection(
                router, policy_version_ref=self.pinned_policy,
                purpose="plan", mode="tier", chain=(), now=NOW,
            )["policy_version_ref"]
        adapter = ChainAdapter({})
        answer = self._model(
            adapter, policy_version_ref=selected,
            slots=["credential-slot:openclaw:openai"],
        ).call(
            purpose="plan", request_id="legacy-tier-choice",
            prompt="tag these", mission=self.mission,
        )
        self.assertEqual(answer["text"], "answered by profile:gpt-6-astra")
        self.assertEqual(adapter.served, ["profile:gpt-6-astra"])

    def test_a_content_refusal_does_not_buy_a_second_opinion(self) -> None:
        adapter = ChainAdapter({
            "profile:gpt-6-astra": {"code": "CONTENT_REFUSAL", "message": "declined"}
        })
        with self.assertRaises(CockpitModelError):
            self._model(adapter, policy_version_ref=self.chain_policy).call(
                purpose="plan", request_id="three", prompt="what next?", mission=self.mission,
            )
        self.assertEqual(adapter.served, ["profile:gpt-6-astra"])
        self.assertEqual([link["skip_reason"] for link in self._links()], ["content_refusal"])

    def test_every_link_failing_is_one_refusal_naming_all_of_them(self) -> None:
        adapter = ChainAdapter({
            "profile:gpt-6-astra": {"code": "PROVIDER_ERROR", "message": "down"},
            "profile:claude-fable-5-1": {"code": "PROVIDER_ERROR", "message": "down"},
        })
        with self.assertRaisesRegex(CockpitModelError, "every model in the brain chain failed"):
            self._model(adapter, policy_version_ref=self.chain_policy).call(
                purpose="plan", request_id="four", prompt="what next?", mission=self.mission,
            )
        self.assertEqual(len(self._links()), 2)
        self.assertTrue(all(not link["served"] for link in self._links()))

    def test_a_policy_with_no_chain_keeps_routing_exactly_as_before(self) -> None:
        adapter = ChainAdapter({})
        answer = self._model(adapter, policy_version_ref=self.pinned_policy).call(
            purpose="plan", request_id="five", prompt="what next?", mission=self.mission,
        )
        self.assertEqual(answer["text"], "answered by profile:gpt-6-astra")
        self.assertEqual(self._links(), [])
        self.assertEqual([item["decision_kind"] for item in self._decisions()], ["initial"])

    def test_a_purpose_with_no_tier_is_refused_under_a_chain_policy(self) -> None:
        from dalton_core.cockpit_model import register_purpose

        register_purpose("p14m_cockpit_untiered")
        with self.assertRaisesRegex(CockpitModelError, "no model tier"):
            self._model(ChainAdapter({}), policy_version_ref=self.chain_policy).call(
                purpose="p14m_cockpit_untiered", request_id="six",
                prompt="what next?", mission=self.mission,
            )

    def test_the_same_question_replays_the_chain_it_already_walked(self) -> None:
        script = {"profile:gpt-6-astra": {"code": "PROVIDER_ERROR", "message": "down"}}
        first = self._model(ChainAdapter(dict(script)), policy_version_ref=self.chain_policy).call(
            purpose="plan", request_id="seven", prompt="what next?", mission=self.mission,
        )
        links, decisions = self._links(), self._decisions()
        replay_adapter = ChainAdapter(dict(script))
        again = self._model(replay_adapter, policy_version_ref=self.chain_policy).call(
            purpose="plan", request_id="seven", prompt="what next?", mission=self.mission,
        )
        self.assertTrue(again["replayed"])
        self.assertEqual(again["text"], first["text"])
        self.assertEqual(replay_adapter.served, [])
        self.assertEqual(self._links(), links)
        self.assertEqual(self._decisions(), decisions)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
