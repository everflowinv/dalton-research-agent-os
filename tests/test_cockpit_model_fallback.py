"""P14-M: a cockpit-shaped call walks its tier's chain when the policy pins one."""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Importing the lane is what registers its purpose, exactly as the lane's own
# child process does before it builds a WorkOrder.
import dalton_core.claim_index_tagging  # noqa: F401
import dalton_core.conviction_call_draft  # noqa: F401
import dalton_core.debate_map_draft  # noqa: F401
import dalton_core.event_judgement  # noqa: F401
from dalton_core.cockpit_model import (
    CockpitModel,
    CockpitModelError,
    build_work,
    independent_model_call,
    register_purpose,
)
from dalton_core.contracts import ModelInvocation, ResultEnvelope
from dalton_core.model_fallback_chain import register_purpose_tier, tier_chain
from dalton_core.model_router import ModelRouter
from dalton_core.openclaw_catalog_reconcile import sync_openclaw_model_catalog
from dalton_core.openclaw_model_adapter import (
    BrokerDefinitelyNotSent,
    BrokerTimeout,
)
from dalton_core.research_planner_setup import credential_slots_for, ensure_planner_policy
from dalton_core.scheduler import Scheduler
from dalton_core.store import content_hash
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from tests.test_openclaw_catalog_reconcile import _config, _controls


NOW = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)
BUDGET_POLICY = "thesis-impact-day-budget-policy:p14m:1"


class MutableClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


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


class BusyThenAvailableAdapter(ChainAdapter):
    def execute(self, work, route, profile):
        if not self.served:
            self.served.append(profile["id"])
            moment = NOW.isoformat(timespec="microseconds")
            invocation = ModelInvocation(
                schema_version="0.1", id="invocation:busy-first",
                created_at=moment, work_order_ref=work.id,
                profile_ref=profile["profile_version_ref"], granularity="work_order",
                capability=route["capability"], provider=profile["provider"],
                model=profile["model"], model_family=profile["family"],
                input_refs=(), output_refs=(), started_at=moment, completed_at=moment,
                usage={}, side_effects=(), runtime_ref="runtime:openclaw-model-broker",
                actor_ref="worker:cockpit-model:0.1",
            )
            return invocation, ResultEnvelope(
                schema_version="0.1", id="result:busy-first", created_at=moment,
                work_order_ref=work.id, invocation_ref=invocation.id, status="failed",
                outputs={}, actual_side_effects=(), usage_refs=(), artifact_refs=(),
                error={"code": "BUSY", "message": "broker concurrency limit reached"},
                metadata={"route_decision_ref": route["id"]},
            )
        return super().execute(work, route, profile)


class CockpitChainTests(unittest.TestCase):
    def test_only_a_proved_pre_send_failure_may_fall_back(self) -> None:
        adapter = ChainAdapter({
            "profile:gpt-6-astra": BrokerDefinitelyNotSent("connect failed")})
        answer = self._model(adapter, policy_version_ref=self.chain_policy).call(
            purpose="plan", request_id="not-sent", prompt="draft", mission=self.mission)
        self.assertEqual(adapter.served,
                         ["profile:gpt-6-astra", "profile:claude-fable-5-1"])
        self.assertIn("claude", answer["text"])

    def test_single_pin_broker_busy_is_settled_as_not_sent(self) -> None:
        adapter = BusyThenAvailableAdapter({})
        with self.assertRaisesRegex(CockpitModelError, "broker concurrency") as raised:
            self._model(adapter, policy_version_ref=self.pinned_policy).call(
                purpose="plan", request_id="pinned-busy", prompt="draft",
                mission=self.mission,
            )
        trace = raised.exception.failure_trace
        self.assertEqual(trace["purpose"], "plan")
        self.assertEqual(trace["base_request_id"], "pinned-busy")
        self.assertRegex(trace["work_order_ref"], r"^work:cockpit-plan-")
        self.assertRegex(trace["work_order_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(trace["formal_result_envelope_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(adapter.served, ["profile:gpt-6-astra"])
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as ledger:
            settlement = ledger.connection.execute(
                "SELECT actual_micros FROM thesis_impact_day_settlements"
            ).fetchone()
        self.assertEqual(settlement[0], 0)

    def test_post_send_timeout_halts_and_keeps_the_full_reservation(self) -> None:
        adapter = ChainAdapter({"profile:gpt-6-astra": BrokerTimeout("recv timed out")})
        with self.assertRaises(CockpitModelError):
            self._model(adapter, policy_version_ref=self.chain_policy).call(
                purpose="plan", request_id="unknown-timeout", prompt="draft",
                mission=self.mission)
        self.assertEqual(adapter.served, ["profile:gpt-6-astra"])
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as ledger:
            admission = json.loads(ledger.connection.execute(
                "SELECT record_json FROM thesis_impact_day_admissions").fetchone()[0])
            settlement = json.loads(ledger.connection.execute(
                "SELECT record_json FROM thesis_impact_day_settlements").fetchone()[0])
        self.assertEqual(settlement["actual_micros"], admission["reserved_micros"])
        self.assertGreater(settlement["actual_micros"], 0)

    def test_host_completion_failure_halts_without_a_second_paid_call(self) -> None:
        adapter = ChainAdapter({"profile:gpt-6-astra": {
            "code": "HOST_COMPLETION_FAILED", "message": "host completion failed"}})
        with self.assertRaises(CockpitModelError):
            self._model(adapter, policy_version_ref=self.chain_policy).call(
                purpose="plan", request_id="host-failed", prompt="draft",
                mission=self.mission)
        self.assertEqual(adapter.served, ["profile:gpt-6-astra"])
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as ledger:
            admission = json.loads(ledger.connection.execute(
                "SELECT record_json FROM thesis_impact_day_admissions").fetchone()[0])
            settlement = json.loads(ledger.connection.execute(
                "SELECT record_json FROM thesis_impact_day_settlements").fetchone()[0])
        self.assertEqual(settlement["actual_micros"], admission["reserved_micros"])

    def test_single_pin_ambiguous_adapter_failure_keeps_reservation(self) -> None:
        adapter = ChainAdapter({"profile:gpt-6-astra": BrokerTimeout("recv timed out")})
        with self.assertRaises(CockpitModelError):
            self._model(adapter, policy_version_ref=self.pinned_policy).call(
                purpose="plan", request_id="pinned-unknown", prompt="draft",
                mission=self.mission)
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as ledger:
            admission = ledger.connection.execute(
                "SELECT reserved_micros FROM thesis_impact_day_admissions").fetchone()[0]
            settlement = ledger.connection.execute(
                "SELECT actual_micros FROM thesis_impact_day_settlements").fetchone()[0]
        self.assertEqual(settlement, admission)

    def test_single_pin_failed_host_envelope_keeps_ceiling_without_cost_telemetry(self) -> None:
        adapter = ChainAdapter({"profile:gpt-6-astra": {
            "code": "HOST_COMPLETION_FAILED", "message": "host completion failed"}})
        with self.assertRaisesRegex(CockpitModelError, "host completion failed"):
            self._model(adapter, policy_version_ref=self.pinned_policy).call(
                purpose="plan", request_id="pinned-host-failed", prompt="draft",
                mission=self.mission)
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as ledger:
            row = ledger.connection.execute(
                "SELECT a.reserved_micros,s.actual_micros FROM thesis_impact_day_admissions a "
                "JOIN thesis_impact_day_settlements s ON s.admission_id=a.admission_id"
            ).fetchone()
        self.assertEqual(row["actual_micros"], row["reserved_micros"])

    def test_capacity_retry_policy_is_closed_and_install_preserved(self) -> None:
        retry = {"cooldown_seconds": 90, "max_recovery_epochs": 2,
                 "scheduler_max_attempts": 4}
        model = self._model(ChainAdapter({}), policy_version_ref=self.chain_policy,
                            capacity_retry=retry)
        self.assertEqual(model.config["capacity_retry"], retry)
        from dalton_core.budget_config_install import preserved_budget_overrides
        path = self.root / "model-config.json"
        path.write_text(json.dumps(model.config))
        self.assertEqual(preserved_budget_overrides(path)["capacity_retry"], retry)
        with self.assertRaisesRegex(Exception, "capacity retry"):
            self._model(ChainAdapter({}), policy_version_ref=self.chain_policy,
                        capacity_retry={"cooldown_seconds": 0,
                                        "max_recovery_epochs": 2,
                                        "scheduler_max_attempts": 4})

    def test_explicit_capacity_policy_versions_base_work_but_default_does_not(self) -> None:
        default_adapter = ChainAdapter({})
        kwargs = {"purpose": "plan", "request_id": "capacity-policy-identity",
                  "prompt": "what next?", "mission": self.mission}
        default_model = self._model(default_adapter, policy_version_ref=self.chain_policy)
        first = default_model.call(**kwargs)
        self.assertTrue(default_model.call(**kwargs)["replayed"])
        self.assertEqual(len(default_adapter.served), 1)

        explicit_adapter = ChainAdapter({})
        explicit = self._model(
            explicit_adapter, policy_version_ref=self.chain_policy,
            capacity_retry={"cooldown_seconds": 60, "max_recovery_epochs": 1,
                            "scheduler_max_attempts": 3})
        second = explicit.call(**kwargs)
        self.assertNotEqual(second["work_order_ref"], first["work_order_ref"])
        changed = self._model(
            explicit_adapter, policy_version_ref=self.chain_policy,
            capacity_retry={"cooldown_seconds": 120, "max_recovery_epochs": 1,
                            "scheduler_max_attempts": 3}).call(**kwargs)
        self.assertNotEqual(changed["work_order_ref"], second["work_order_ref"])

        injected = explicit.call(
            **{**kwargs, "request_id":
               "caller:capacity-policy:not-the-current-policy"})
        self.assertNotEqual(injected["work_order_ref"], second["work_order_ref"])
        with Scheduler(self.root / "scheduler.sqlite") as scheduler:
            stored = scheduler.work_order_authority(
                injected["work_order_ref"])["work_order"]
        expected_retry = {"cooldown_seconds": 60, "max_recovery_epochs": 1,
                          "scheduler_max_attempts": 3}
        self.assertIn(
            ":capacity-policy:" + content_hash(expected_retry)[:16],
            stored["metadata"]["request_id"],
        )
        operator_request = (
            "operator-capacity"
            + ":capacity-policy:" + content_hash(expected_retry)[:16]
            + ":operator-recovery:" + "a" * 16
        )
        before = len(explicit_adapter.served)
        recovered = explicit.call(**{**kwargs, "request_id": operator_request})
        replay = explicit.call(**{**kwargs, "request_id": operator_request})
        self.assertFalse(recovered["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(explicit_adapter.served), before + 1)
        with Scheduler(self.root / "scheduler.sqlite") as scheduler:
            recovered_work = scheduler.work_order_authority(
                recovered["work_order_ref"])["work_order"]
        self.assertEqual(
            recovered_work["metadata"]["request_id"].count(":capacity-policy:"),
            1,
        )

    def test_a_legacy_terminal_busy_failure_gets_one_versioned_recovery_identity(self) -> None:
        adapter = BusyThenAvailableAdapter({})
        clock = MutableClock()
        model = self._model(adapter, policy_version_ref=self.chain_policy, clock=clock)
        kwargs = {"purpose": "plan", "request_id": "legacy-busy-recovery",
                  "prompt": "what next?", "mission": self.mission}
        with patch("dalton_core.model_fallback_chain.classify_model_failure",
                   return_value="unclassified_failure"):
            with self.assertRaisesRegex(CockpitModelError, "unclassified_failure"):
                model.call(**kwargs)
        clock.advance(1800)
        answer = model.call(**kwargs)
        self.assertEqual(answer["text"], "answered by profile:gpt-6-astra")
        with Scheduler(self.root / "scheduler.sqlite") as scheduler:
            rows = list(scheduler.connection.execute(
                "SELECT work_order_id FROM scheduler_work_orders "
                "ORDER BY created_at,work_order_id"
            ))
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["work_order_id"], rows[1]["work_order_id"])
        replay = model.call(**kwargs)
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(adapter.served), 2)

    def test_exhausted_busy_waits_then_uses_one_configured_recovery_epoch(self) -> None:
        adapter = ChainAdapter({
            "profile:gpt-6-astra": {
                "code": "BUSY", "message": "broker concurrency limit reached"}
        })
        clock = MutableClock()
        retry = {"cooldown_seconds": 60, "max_recovery_epochs": 1,
                 "scheduler_max_attempts": 3}
        model = self._model(adapter, policy_version_ref=self.chain_policy,
                            capacity_retry=retry, clock=clock)
        kwargs = {"purpose": "plan", "request_id": "long-busy",
                  "prompt": "what next?", "mission": self.mission}
        for _ in range(3):
            with self.assertRaisesRegex(CockpitModelError, "capacity_busy"):
                model.call(**kwargs)
        self.assertEqual(len(adapter.served), 3)
        adapter.script.clear()
        with self.assertRaisesRegex(CockpitModelError, "capacity_busy"):
            model.call(**kwargs)
        self.assertEqual(len(adapter.served), 3)
        clock.advance(60)
        answer = model.call(**kwargs)
        self.assertEqual(len(adapter.served), 4)
        self.assertGreater(answer["cost_micros"], 0)
        restarted = self._model(adapter, policy_version_ref=self.chain_policy,
                                capacity_retry=retry, clock=clock)
        self.assertTrue(restarted.call(**kwargs)["replayed"])
        self.assertEqual(len(adapter.served), 4)
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as ledger:
            charges = [row[0] for row in ledger.connection.execute(
                "SELECT s.actual_micros FROM thesis_impact_day_admissions a "
                "JOIN thesis_impact_day_settlements s ON s.admission_id=a.admission_id "
                "ORDER BY a.created_at,a.attempt_number"
            )]
        self.assertEqual(len(charges), 4)
        self.assertEqual(charges[:3], [0, 0, 0])
        self.assertEqual(charges[3], answer["cost_micros"])

    def test_configured_capacity_epochs_exhaust_without_new_identities(self) -> None:
        adapter = ChainAdapter({
            "profile:gpt-6-astra": {"code": "BUSY", "message": "still busy"}
        })
        clock = MutableClock()
        retry = {"cooldown_seconds": 10, "max_recovery_epochs": 1,
                 "scheduler_max_attempts": 1}
        model = self._model(adapter, policy_version_ref=self.chain_policy,
                            capacity_retry=retry, clock=clock)
        kwargs = {"purpose": "plan", "request_id": "bounded-long-busy",
                  "prompt": "what next?", "mission": self.mission}
        with self.assertRaises(CockpitModelError):
            model.call(**kwargs)
        clock.advance(10)
        with self.assertRaises(CockpitModelError):
            model.call(**kwargs)
        clock.advance(10)
        with self.assertRaisesRegex(CockpitModelError, "capacity_recovery_exhausted"):
            model.call(**kwargs)
        self.assertEqual(len(adapter.served), 2)
        with Scheduler(self.root / "scheduler.sqlite") as scheduler:
            count = scheduler.connection.execute(
                "SELECT count(*) FROM scheduler_work_orders").fetchone()[0]
        self.assertEqual(count, 2)

    def test_broker_busy_retries_same_work_without_fallback_or_double_charge(self) -> None:
        adapter = BusyThenAvailableAdapter({})
        model = self._model(adapter, policy_version_ref=self.chain_policy)
        kwargs = {"purpose": "plan", "request_id": "busy-recovery",
                  "prompt": "what next?", "mission": self.mission}
        with self.assertRaisesRegex(CockpitModelError, "capacity_busy"):
            model.call(**kwargs)
        self.assertEqual(adapter.served, ["profile:gpt-6-astra"])
        answer = model.call(**kwargs)
        self.assertEqual(answer["text"], "answered by profile:gpt-6-astra")
        self.assertEqual(adapter.served,
                         ["profile:gpt-6-astra", "profile:gpt-6-astra"])
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as ledger:
            rows = list(ledger.connection.execute(
                "SELECT a.attempt_number,s.actual_micros "
                "FROM thesis_impact_day_admissions a "
                "JOIN thesis_impact_day_settlements s ON s.admission_id=a.admission_id "
                "ORDER BY a.attempt_number"
            ))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["actual_micros"], 0)
        self.assertEqual(rows[1]["actual_micros"], answer["cost_micros"])

    def test_debate_and_conviction_verifiers_exclude_the_actual_producer_family(self) -> None:
        for producer_purpose, verifier_purpose in (
            ("debate_map", "debate_map_verifier"),
            ("conviction_call", "conviction_call_verifier"),
        ):
            with self.subTest(verifier_purpose=verifier_purpose):
                producer_adapter = ChainAdapter({
                    "profile:gpt-6-astra": BrokerDefinitelyNotSent(
                        "scripted producer connect failure"),
                })
                producer = self._model(
                    producer_adapter, policy_version_ref=self.chain_policy
                ).call(
                    purpose=producer_purpose,
                    request_id=f"{producer_purpose}-producer-route",
                    prompt="draft",
                    mission=self.mission,
                )
                verifier_adapter = ChainAdapter({})
                verifier = self._model(
                    verifier_adapter,
                    policy_version_ref=self.verifier_policy,
                    slots=self.verifier_slots,
                ).call(
                    purpose=verifier_purpose,
                    request_id=f"{producer_purpose}-verifier-route",
                    prompt='{"verdict":"pass","findings":[]}',
                    mission=self.mission,
                    producer_route_decision_refs=[producer["route_decision_ref"]],
                )
                with ModelRouter(self.router_db, read_only=True) as router:
                    producer_route = router.get_decision(producer["route_decision_ref"])
                    verifier_route = router.get_decision(verifier["route_decision_ref"])
                    producer_profile = router.get_profile(
                        producer_route["selected_profile_version_ref"]
                    )
                self.assertNotEqual(
                    producer_route["selected_endpoint"]["family"],
                    verifier_route["selected_endpoint"]["family"],
                )
                self.assertNotIn(
                    producer_profile["id"],
                    verifier_adapter.served,
                )

    def test_replayed_formal_failure_preserves_its_error_code(self) -> None:
        formal = {"terminal_state": "failed", "result_envelope": {
            "error": {"code": "MODEL_CHAIN_EXHAUSTED"}}}
        with self.assertRaisesRegex(CockpitModelError, "MODEL_CHAIN_EXHAUSTED"):
            CockpitModel._answer(formal, type("Work", (), {"id": "work:x"})(),
                                 True, 0, "replayed")

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.router_db = self.root / "router.sqlite"
        config = _config()
        broker_profiles = config["plugins"]["entries"][
            "dalton-openclaw-model-broker"
        ]["config"]["profiles"]
        verifier_ids = set(tier_chain("verifier"))
        for profile in broker_profiles:
            if profile["id"] in verifier_ids:
                profile["providerControls"] = _controls(profile["model"])
        with ModelRouter(self.router_db) as router:
            sync_openclaw_model_catalog(router, config, checked_at=NOW)
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
            register_purpose_tier(register_purpose("investment_memo_verifier"), "verifier")
            register_purpose_tier(register_purpose("investment_memo"), "brain")
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
               slots: list[str] | None = None, capacity_retry=None,
               clock=None) -> CockpitModel:
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
            **({} if capacity_retry is None else {"capacity_retry": capacity_retry}),
        }
        return CockpitModel(
            config, scheduler_db=str(self.root / "scheduler.sqlite"),
            adapter_factory=lambda router: adapter, clock=clock or (lambda: NOW),
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
            "profile:gpt-6-astra": BrokerDefinitelyNotSent("connect failed")
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
            [(1, False, "transport_failure"), (2, True, None)],
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
            "profile:gpt-6-astra": BrokerDefinitelyNotSent("connect failed")
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
        self.assertEqual(
            stored["work_order"]["requested_capabilities"],
            ["provider-controlled-verify"],
        )
        self.assertEqual(metadata["verifier_output_schema_version"], "0.1")
        self.assertEqual(metadata["verifier_provider_contract"],
                         "event-judgement-verifier-provider-output-0.1")
        self.assertRegex(metadata["verifier_provider_schema_hash"], r"^[0-9a-f]{64}$")
        legacy = build_work(
            purpose="event_judgement_verifier", request_id="event-provider-contract",
            prompt='{"verdict":"pass","findings":[]}',
            mission_version_ref=self.mission["id"], max_input_tokens=120_000,
            max_output_tokens=500, max_cost_usd=0.5, max_seconds=120,
            created_at=self.mission["created_at"],
        )
        self.assertNotEqual(verifier["work_order_ref"], legacy.id)

    def test_debate_and_conviction_work_orders_bind_their_provider_contracts(self) -> None:
        producer = self._model(
            ChainAdapter({}), policy_version_ref=self.chain_policy
        ).call(purpose="plan", request_id="argument-contract-producer",
               prompt="draft", mission=self.mission)
        cases = (
            ("debate_map_verifier", "debate-map-verifier-provider-output-0.1"),
            ("conviction_call_verifier", "conviction-call-verifier-provider-output-0.1"),
        )
        for purpose, contract in cases:
            with self.subTest(purpose=purpose):
                result = self._model(
                    ChainAdapter({}), policy_version_ref=self.verifier_policy,
                    slots=self.verifier_slots,
                ).call(
                    purpose=purpose, request_id=f"{purpose}-contract",
                    prompt='{"verdict":"pass","findings":[]}',
                    mission=self.mission,
                    producer_route_decision_refs=[producer["route_decision_ref"]],
                )
                with Scheduler(self.root / "scheduler.sqlite") as scheduler:
                    stored = scheduler.work_order_authority(result["work_order_ref"])
                metadata = stored["work_order"]["metadata"]
                self.assertEqual(metadata["verifier_provider_contract"], contract)
                self.assertRegex(
                    metadata["verifier_provider_schema_hash"], r"^[0-9a-f]{64}$"
                )
                legacy = build_work(
                    purpose=purpose,
                    request_id=f"{purpose}-contract:producer:legacy",
                    prompt='{"verdict":"pass","findings":[]}',
                    mission_version_ref=self.mission["id"],
                    max_input_tokens=120_000,
                    max_output_tokens=500,
                    max_cost_usd=0.5,
                    max_seconds=120,
                    created_at=self.mission["created_at"],
                )
                self.assertNotEqual(result["work_order_ref"], legacy.id)

    def test_required_controls_failure_halts_without_trying_another_provider(self) -> None:
        producer = self._model(
            ChainAdapter({}), policy_version_ref=self.chain_policy
        ).call(purpose="event_judgement", request_id="controls-producer",
               prompt="draft", mission=self.mission)
        adapter = ChainAdapter({
            profile_id: {
                "code": "REQUIRED_CONTROLS_UNAVAILABLE",
                "message": "profile lacks providerControls; mode missing not advertised",
            }
            for profile_id in tier_chain("verifier")
        })
        with self.assertRaisesRegex(
            CockpitModelError,
            "contract_violation.*REQUIRED_CONTROLS_UNAVAILABLE.*lacks providerControls",
        ):
            self._model(
                adapter, policy_version_ref=self.verifier_policy,
                slots=self.verifier_slots,
            ).call(
                purpose="event_judgement_verifier",
                request_id="controls-halt",
                prompt='{"verdict":"pass","findings":[]}',
                mission=self.mission,
                producer_route_decision_refs=[producer["route_decision_ref"]],
            )
        self.assertEqual(len(adapter.served), 1)

    def test_memo_writer_reads_real_scheduler_work_and_router_route(self) -> None:
        from dalton_core.writer_server import WriterServer

        producer = self._model(
            ChainAdapter({}), policy_version_ref=self.chain_policy
        ).call(purpose="investment_memo", request_id="memo-real-authority",
               prompt="draft", mission=self.mission)
        verifier = self._model(
            ChainAdapter({}), policy_version_ref=self.verifier_policy,
            slots=self.verifier_slots,
        ).call(purpose="investment_memo_verifier", request_id="memo-real-authority",
               prompt='{"verdict":"pass"}', mission=self.mission,
               producer_route_decision_refs=[producer["route_decision_ref"]])
        server = object.__new__(WriterServer)
        server._scheduler = Scheduler(self.root / "scheduler.sqlite")
        self.addCleanup(server._scheduler.close)
        with ModelRouter(self.router_db, read_only=True) as router:
            producer_route = router.get_decision(producer["route_decision_ref"])
            verifier_route = router.get_decision(verifier["route_decision_ref"])
        producer_call = {"work_order_ref": producer["work_order_ref"],
                         "result_envelope_ref": producer["result_envelope_ref"],
                         "invocation_ref": producer["invocation_ref"]}
        envelope = WriterServer._verify_memo_formal_call(
            server, producer_call, producer_route, purpose="investment_memo",
            mission=self.mission)
        self.assertEqual(envelope["status"], "succeeded")
        verifier_call = {"work_order_ref": verifier["work_order_ref"],
                         "result_envelope_ref": verifier["result_envelope_ref"],
                         "invocation_ref": verifier["invocation_ref"]}
        envelope = WriterServer._verify_memo_formal_call(
            server, verifier_call, verifier_route, purpose="investment_memo_verifier",
            mission=self.mission,
            producer_routes=[producer["route_decision_ref"]])
        self.assertEqual(envelope["status"], "succeeded")

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
            "profile:deepseek-v4-flash": BrokerDefinitelyNotSent("connect failed")
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
        adapter = ChainAdapter({links[0]: BrokerDefinitelyNotSent("connect failed")})
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
            "profile:gpt-6-astra": BrokerDefinitelyNotSent("connect failed token=secret-one"),
            "profile:claude-fable-5-1": BrokerDefinitelyNotSent("alias is not installed"),
        })
        with self.assertRaisesRegex(CockpitModelError, "every model in the brain chain failed"):
            self._model(adapter, policy_version_ref=self.chain_policy).call(
                purpose="plan", request_id="four", prompt="what next?", mission=self.mission,
            )
        self.assertEqual(len(self._links()), 2)
        self.assertTrue(all(not link["served"] for link in self._links()))
        with Scheduler(self.root / "scheduler.sqlite") as scheduler:
            row = scheduler.connection.execute(
                "SELECT work_order_id FROM scheduler_formal_results "
                "ORDER BY created_at DESC LIMIT 1").fetchone()
            formal = scheduler.formal_result(row["work_order_id"])
        envelope = formal["result_envelope"]
        self.assertEqual(envelope["error"]["code"], "MODEL_CHAIN_EXHAUSTED")
        self.assertIn("transport_failure", envelope["error"]["message"])
        self.assertIn("alias is not installed", envelope["error"]["message"])
        self.assertNotIn("secret-one", json.dumps(envelope))
        self.assertEqual(
            envelope["metadata"]["chain_failures"][0]["message"],
            "the model call failed: connect failed token=[REDACTED]",
        )

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
        script = {"profile:gpt-6-astra": BrokerDefinitelyNotSent("connect failed")}
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
