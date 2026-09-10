"""C2: four capacity pools a day, the borrow rule, and pool-aware settlement."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import dalton_core.claim_index_tagging  # noqa: F401 - registers the purpose
from dalton_core import budget_pools
from dalton_core.budget_pools import (
    BudgetPoolError,
    DEFAULT_SHARES,
    POOL_EXHAUSTED_STATUS,
    POOL_NAMES,
    allocate_borrow,
    borrow_open,
    borrowable_micros,
    classify_legacy_work_order,
    day_fraction_elapsed,
    lane_pools,
    mission_pool_scope,
    pool_caps,
    pool_for_operation,
    pool_for_purpose,
    pool_status,
)
from dalton_core.cockpit_model import (
    CockpitModel,
    CockpitModelError,
    CockpitModelPoolExhausted,
    lane_status_for,
)
from dalton_core.openclaw_model_adapter import BrokerDefinitelyNotSent
from dalton_core.lane_registry import (
    LaneRegistryError,
    LaneSpec,
    register_lane,
    registered_lanes,
    unregister_lane,
)
from dalton_core.model_router import ModelRouter
from dalton_core.openclaw_catalog_reconcile import sync_openclaw_model_catalog
from dalton_core.research_planner_setup import credential_slots_for, ensure_planner_policy
from dalton_core.store import content_hash
from dalton_core.thesis_impact_budget import (
    ThesisImpactBudgetStore,
    ThesisImpactBudgetValidationError,
    ThesisImpactDayBudgetExceeded,
)
from dalton_core.model_fallback_chain import tier_chain
from tests.test_cockpit_model_fallback import ChainAdapter
from tests.test_openclaw_catalog_reconcile import _config


DAY = "2026-09-09"
MORNING = datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc)
EVENING = datetime(2026, 9, 9, 18, 0, tzinfo=timezone.utc)
POLICY = "thesis-impact-day-budget-policy:c2:1"


def mission(pools: dict | None = None, *, daily: float = 10.0) -> dict:
    budget = {"max_daily_paid_calls": 100, "max_daily_cost_usd": daily,
              "max_alphaengine_calls_24h": 5}
    if pools is not None:
        budget["pools"] = pools
    return {
        "mission_ref": "coverage-mission:c2",
        "id": "coverage-mission-version:c2:1",
        "content_hash": content_hash({"mission": "c2"}),
        "budget": budget,
    }


class PoolCapTests(unittest.TestCase):
    def test_a_mission_without_pools_gets_the_default_split_and_says_so(self) -> None:
        caps = pool_caps(mission()["budget"])
        self.assertTrue(caps["defaulted"])
        self.assertEqual(caps["source"], "budget_pools.DEFAULT_SHARES")
        self.assertEqual(caps["caps_micros"], {
            "coverage": 5_500_000, "event_response": 1_500_000,
            "adhoc": 2_500_000, "maintenance": 500_000,
        })

    def test_the_default_shares_spend_the_whole_day_and_no_more(self) -> None:
        self.assertEqual(sum(DEFAULT_SHARES.values()), 1)
        caps = pool_caps(mission()["budget"])
        self.assertEqual(
            sum(caps["caps_micros"].values()), caps["day_cap_micros"]
        )

    def test_the_adhoc_share_is_the_boundary_the_plan_set(self) -> None:
        # 25% of max_daily_cost_usd, the number P14e's lane already enforces.
        from dalton_core.research_task import POOL_SHARE

        self.assertEqual(DEFAULT_SHARES["adhoc"], POOL_SHARE)

    def test_a_mission_that_declares_pools_is_believed(self) -> None:
        caps = pool_caps(mission({
            "coverage": 6, "event_response": 2, "adhoc": 1, "maintenance": 1,
        })["budget"])
        self.assertFalse(caps["defaulted"])
        self.assertEqual(caps["caps_micros"]["coverage"], 6_000_000)
        self.assertEqual(caps["shares"]["coverage"], "0.6000")

    def test_pools_that_oversubscribe_the_day_are_refused(self) -> None:
        with self.assertRaisesRegex(BudgetPoolError, "exceed"):
            pool_caps(mission({
                "coverage": 9, "event_response": 2, "adhoc": 1, "maintenance": 1,
            })["budget"])

    def test_a_partial_or_misnamed_split_is_refused_rather_than_repaired(self) -> None:
        with self.assertRaises(BudgetPoolError):
            pool_caps(mission({"coverage": 9, "adhoc": 1})["budget"])
        with self.assertRaises(BudgetPoolError):
            pool_caps(mission({
                "coverage": 9, "event_response": 0, "adhoc": 1, "research": 0,
            })["budget"])
        with self.assertRaises(BudgetPoolError):
            pool_caps(mission({
                "coverage": 9, "event_response": -1, "adhoc": 1, "maintenance": 1,
            })["budget"])

    def test_the_mission_field_to_add_is_named_once(self) -> None:
        self.assertEqual(budget_pools.MISSION_POOLS_FIELD, "pools")


class BorrowRuleTests(unittest.TestCase):
    def test_the_day_is_half_over_at_noon_utc(self) -> None:
        self.assertAlmostEqual(day_fraction_elapsed(MORNING, DAY), 0.375)
        self.assertFalse(borrow_open(MORNING, DAY))
        self.assertTrue(borrow_open(EVENING, DAY))
        # A day already past is entirely over, whatever the clock says.
        self.assertTrue(borrow_open(MORNING, "2026-09-08"))

    def test_only_coverage_borrows_and_only_late(self) -> None:
        caps = {"coverage": 100, "event_response": 100, "adhoc": 100, "maintenance": 100}
        spent = {"coverage": 100}
        self.assertEqual(
            borrowable_micros("coverage", spent, caps, now=MORNING, day=DAY), {}
        )
        self.assertEqual(
            borrowable_micros("coverage", spent, caps, now=EVENING, day=DAY),
            {"event_response": 100, "adhoc": 100, "maintenance": 100},
        )
        self.assertEqual(
            borrowable_micros("adhoc", spent, caps, now=EVENING, day=DAY), {}
        )

    def test_a_lender_does_not_offer_what_it_has_already_lent(self) -> None:
        caps = {"coverage": 100, "event_response": 100, "adhoc": 0, "maintenance": 0}
        offers = borrowable_micros(
            "coverage", {"coverage": 100}, caps, now=EVENING, day=DAY,
            lent={"event_response": 60},
        )
        self.assertEqual(offers, {"event_response": 40})

    def test_a_loan_is_all_or_nothing(self) -> None:
        self.assertEqual(allocate_borrow(50, {"adhoc": 30, "maintenance": 40}),
                         {"adhoc": 30, "maintenance": 20})
        self.assertEqual(allocate_borrow(150, {"adhoc": 30, "maintenance": 40}), {})


class LanePoolMappingTests(unittest.TestCase):
    def test_every_registered_lane_resolves_to_a_real_pool(self) -> None:
        assigned = lane_pools()
        self.assertTrue(assigned)
        for operation, pool in assigned.items():
            self.assertIn(pool, POOL_NAMES, operation)

    def test_the_research_task_lane_is_the_adhoc_pool(self) -> None:
        self.assertEqual(pool_for_operation("dispatch_research_task"), "adhoc")

    def test_a_lane_nobody_assigned_is_coverage(self) -> None:
        self.assertEqual(pool_for_operation("dispatch_something_new"), "coverage")

    def test_a_lane_may_declare_its_own_pool_and_it_wins(self) -> None:
        spec = register_lane(LaneSpec(
            operation="dispatch_c2_declared", order=9101,
            driver_key="c2_declared", budget_pool="maintenance", pool_share=0.5,
        ))
        self.addCleanup(unregister_lane, "dispatch_c2_declared")
        self.assertEqual(pool_for_operation("dispatch_c2_declared"), "maintenance")
        self.assertEqual(spec.pool_share, 0.5)

    def test_a_lane_that_says_nothing_keeps_todays_behaviour(self) -> None:
        for spec in registered_lanes():
            self.assertIsNone(spec.budget_pool, spec.operation)
            self.assertIsNone(spec.pool_share, spec.operation)

    def test_a_pool_that_is_not_a_pool_is_refused_at_registration(self) -> None:
        with self.assertRaisesRegex(LaneRegistryError, "not a budget pool"):
            LaneSpec(operation="dispatch_c2_bad", order=9102, budget_pool="petty_cash")
        with self.assertRaisesRegex(LaneRegistryError, "fraction of its pool"):
            LaneSpec(operation="dispatch_c2_bad", order=9103, pool_share=2.0)

    def test_purposes_map_to_pools_and_default_to_coverage(self) -> None:
        self.assertEqual(pool_for_purpose("plan"), "coverage")
        self.assertEqual(pool_for_purpose("quality"), "maintenance")
        self.assertEqual(pool_for_purpose("nothing_registered"), "coverage")

    def test_history_can_be_classified_without_pretending_it_was_pooled(self) -> None:
        self.assertEqual(
            classify_legacy_work_order("work:cockpit-quality-abc"), "maintenance")
        self.assertEqual(
            classify_legacy_work_order("work:cockpit-plan-abc"), "coverage")
        self.assertEqual(
            classify_legacy_work_order("work:extraction-window-abc"), "coverage")


class PoolAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = MORNING
        self.store = ThesisImpactBudgetStore(clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.store.register_policy(
            policy_version_id=POLICY, day_cap_micros=100_000_000)
        self.mission = mission()
        self.scope = {
            "mission_ref": self.mission["mission_ref"],
            "mission_version_ref": self.mission["id"],
            "mission_version_hash": self.mission["content_hash"],
            "max_daily_paid_calls": 100,
            "max_daily_cost_micros": 10_000_000,
        }

    def admit(self, name: str, pool: str | None, micros: int, *, lane: str = "lane:x"):
        binding = dict(self.scope)
        if pool is not None:
            binding.update(mission_pool_scope(self.mission, pool=pool, lane=lane))
        return self.store.admit(
            policy_version_id=POLICY, day=DAY, work_order_ref=f"work:{name}",
            attempt_number=1, phase="assessment", route_decision_ref="route:1",
            reserved_micros=micros, mission_binding=binding,
        )

    def test_an_admission_carries_its_pool_into_the_ledger(self) -> None:
        admitted = self.admit("one", "adhoc", 1_000_000)
        self.assertEqual(admitted["status"], "fresh")
        self.assertEqual(admitted["pool"], "adhoc")
        row = self.store.connection.execute(
            "SELECT pool, pool_lane FROM thesis_impact_day_admissions "
            "WHERE admission_id=?", (admitted["admission_id"],),
        ).fetchone()
        self.assertEqual((row["pool"], row["pool_lane"]), ("adhoc", "lane:x"))

    def test_a_spent_pool_is_returned_not_raised(self) -> None:
        self.admit("one", "adhoc", 2_400_000)
        refused = self.admit("two", "adhoc", 200_000)
        self.assertEqual(refused["status"], "rejected")
        self.assertEqual(refused["reason"], "pool_exhausted")
        self.assertEqual(refused["pool"], "adhoc")
        self.assertEqual((refused["spent"], refused["cap"]), (2_400_000, 2_500_000))
        # And it did not poison the identity the way an owner-cap rejection
        # does: tomorrow's pool refills, so the same work may be admitted.
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM thesis_impact_day_rejections"
            ).fetchone()[0], 0)

    def test_one_pool_running_out_does_not_stop_another(self) -> None:
        self.admit("one", "adhoc", 2_500_000)
        self.assertEqual(self.admit("two", "adhoc", 1)["status"], "rejected")
        self.assertEqual(self.admit("three", "maintenance", 400_000)["status"], "fresh")

    def test_the_owner_cap_still_raises_for_an_unpooled_admission(self) -> None:
        # Everything that predates C2 passes no pool and behaves exactly as it
        # did, including raising when the day policy's cap is exceeded.
        with self.assertRaises(ThesisImpactDayBudgetExceeded):
            self.admit("one", None, 200_000_000)

    def test_coverage_borrows_only_after_the_day_is_half_over(self) -> None:
        self.assertEqual(self.admit("one", "coverage", 5_600_000)["status"], "rejected")
        self.now = EVENING
        admitted = self.admit("two", "coverage", 5_600_000)
        self.assertEqual(admitted["status"], "fresh")
        self.assertEqual(admitted["borrowed_from"], {"event_response": 100_000})

    def test_another_pool_never_borrows_however_late_it_is(self) -> None:
        self.now = EVENING
        self.assertEqual(self.admit("one", "adhoc", 2_600_000)["status"], "rejected")

    def test_the_same_idle_dollar_is_not_lent_twice(self) -> None:
        self.now = EVENING
        first = self.admit("one", "coverage", 5_600_000)
        self.assertEqual(first["borrowed_from"], {"event_response": 100_000})
        second = self.admit("two", "coverage", 1_500_000)
        # The event pool's remaining 1.4M, then the next pool in order for the
        # rest -- never the 100k it had already lent.
        self.assertEqual(second["borrowed_from"],
                         {"event_response": 1_400_000, "adhoc": 100_000})
        status = pool_status(self.store, mission=self.mission, day=DAY, now=EVENING)
        self.assertEqual(status["pools"]["event_response"]["lent_micros"], 1_500_000)
        self.assertEqual(status["pools"]["event_response"]["remaining_micros"], 0)
        # Exactly the overage, borrowed once: 5.6 + 1.5 against a 5.5 cap.
        self.assertEqual(status["pools"]["coverage"]["borrowed_micros"], 1_600_000)

    def test_a_borrower_does_not_borrow_for_the_same_overage_twice(self) -> None:
        # The bug this asserts against: a loan raised the pool's spend but not
        # its cap, so every later admission saw the whole accumulated overage
        # again and borrowed for it again. Four ten-micro admissions against
        # an exhausted pool lent a hundred.
        self.now = EVENING
        self.admit("fill", "coverage", 5_500_000)
        for index in range(4):
            admitted = self.admit(f"over{index}", "coverage", 10)
            self.assertEqual(admitted["status"], "fresh")
        status = pool_status(self.store, mission=self.mission, day=DAY, now=EVENING)
        overage = status["pools"]["coverage"]["spent_micros"] - 5_500_000
        self.assertEqual(overage, 40)
        self.assertEqual(status["pools"]["coverage"]["borrowed_micros"], 40)
        self.assertEqual(
            sum(pool["lent_micros"] for pool in status["pools"].values()), 40)

    def test_pool_status_caps_the_refusals_it_returns_and_counts_them_all(self) -> None:
        self.admit("fill", "adhoc", 2_500_000)
        for index in range(25):
            self.assertEqual(
                self.admit(f"no{index}", "adhoc", 1)["status"], "rejected")
        status = pool_status(self.store, mission=self.mission, day=DAY,
                             now=MORNING, max_exhausted_lanes=5)
        self.assertEqual(status["exhausted_lane_count"], 25)
        self.assertEqual(len(status["exhausted_lanes"]), 5)
        self.assertEqual(status["exhausted_lanes"][0]["work_order_ref"],
                         "work:no24")

    def test_a_settlement_is_attributed_to_the_pool_of_its_admission(self) -> None:
        admitted = self.admit("one", "event_response", 1_000_000)
        settled = self.store.settle(admitted["admission_id"], actual_micros=400_000)
        self.assertEqual(settled["pool"], "event_response")
        row = self.store.connection.execute(
            "SELECT pool, actual_micros FROM thesis_impact_day_settlements"
        ).fetchone()
        self.assertEqual((row["pool"], row["actual_micros"]),
                         ("event_response", 400_000))
        status = pool_status(self.store, mission=self.mission, day=DAY, now=MORNING)
        # The pool now counts what was spent, not what was held.
        self.assertEqual(status["pools"]["event_response"]["spent_micros"], 400_000)

    def test_pool_report_reads_the_append_only_correction(self) -> None:
        admitted = self.admit("historical", "event_response", 1_000_000)
        settled = self.store.settle(admitted["admission_id"], actual_micros=1)
        self.store.correct_uncertain_settlement(
            admitted["admission_id"], settlement_id=settled["settlement_id"],
            corrected_micros=900_000, evidence_ref="result:historical",
            evidence_hash="c" * 64, actor_ref="operator:owner",
            idempotency_key="correction:historical-pool",
        )
        status = pool_status(self.store, mission=self.mission, day=DAY, now=MORNING)
        self.assertEqual(status["pools"]["event_response"]["spent_micros"], 900_000)
        unchanged = self.store.connection.execute(
            "SELECT actual_micros FROM thesis_impact_day_settlements WHERE admission_id=?",
            (admitted["admission_id"],),
        ).fetchone()[0]
        self.assertEqual(unchanged, 1)

    def test_pool_status_names_the_lanes_that_ran_out_today(self) -> None:
        self.admit("one", "adhoc", 2_500_000, lane="dispatch_research_task")
        self.admit("two", "adhoc", 1, lane="dispatch_research_task")
        status = pool_status(self.store, mission=self.mission, day=DAY, now=MORNING)
        self.assertTrue(status["caps_defaulted"])
        self.assertFalse(status["borrow_open"])
        self.assertEqual(status["pools"]["adhoc"]["remaining_micros"], 0)
        self.assertTrue(status["pools"]["adhoc"]["exhausted"])
        self.assertEqual(
            [(item["lane"], item["pool"]) for item in status["exhausted_lanes"]],
            [("dispatch_research_task", "adhoc")],
        )

    def test_spend_with_no_pool_is_reported_as_unpooled_not_as_coverage(self) -> None:
        self.admit("one", None, 3_000_000)
        status = pool_status(self.store, mission=self.mission, day=DAY, now=MORNING)
        self.assertEqual(status["unpooled_micros"], 3_000_000)
        self.assertEqual(status["pools"]["coverage"]["spent_micros"], 0)

    def test_a_binding_may_not_name_a_pool_without_the_days_caps(self) -> None:
        with self.assertRaises(ThesisImpactBudgetValidationError):
            self.store.admit(
                policy_version_id=POLICY, day=DAY, work_order_ref="work:bad",
                attempt_number=1, phase="assessment", route_decision_ref="route:1",
                reserved_micros=1, mission_binding={**self.scope, "pool": "adhoc"},
            )

    def test_caps_that_exceed_the_missions_day_cost_are_refused(self) -> None:
        with self.assertRaises(ThesisImpactBudgetValidationError):
            self.store.admit(
                policy_version_id=POLICY, day=DAY, work_order_ref="work:bad",
                attempt_number=1, phase="assessment", route_decision_ref="route:1",
                reserved_micros=1,
                mission_binding={
                    **self.scope, "pool": "adhoc",
                    "pool_caps_micros": {name: 9_000_000 for name in POOL_NAMES},
                },
            )

    def test_replaying_an_admission_says_which_pool_it_was_admitted_under(self) -> None:
        first = self.admit("one", "adhoc", 1_000_000)
        again = self.admit("one", "adhoc", 1_000_000)
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["pool"], "adhoc")
        self.assertEqual(again["admission_id"], first["admission_id"])


class CockpitPoolTests(unittest.TestCase):
    """The one place every cockpit-shaped model call is admitted."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.router_db = self.root / "router.sqlite"
        with ModelRouter(self.router_db) as router:
            sync_openclaw_model_catalog(router, _config(), checked_at=MORNING)
            self.policy = ensure_planner_policy(
                router, tier="cheap", now=MORNING,
                policy_id="model-routing-policy:c2-cockpit-cheap",
            )["policy_version_ref"]
            self.slots = credential_slots_for(router, list(tier_chain("cheap")))
            # A policy that pins one profile declares no chain, so a call
            # under it takes the single-shot route -- the other of the two
            # places an admission is made.
            self.pinned = ensure_planner_policy(
                router, profile_ids=["profile:deepseek-v4-flash"], now=MORNING,
                policy_id="model-routing-policy:c2-cockpit-pinned",
            )["policy_version_ref"]
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as budget:
            budget.register_policy(
                policy_version_id=POLICY, day_cap_micros=5_000_000)

    def _model(self, adapter: ChainAdapter, *, policy: str | None = None) -> CockpitModel:
        config = {
            "routing_policy_ref": policy or self.policy,
            "credential_slot_refs": list(self.slots),
            "model_router_db": str(self.router_db),
            "broker_socket": str(self.root / "none.sock"),
            "broker_auth_key": str(self.root / "none.key"),
            "broker_client_id": "client:dalton-core",
            "expected_agent_id": "chem",
            "budget_db": str(self.root / "budget.sqlite"),
            "budget_policy_ref": POLICY,
        }
        return CockpitModel(
            config, scheduler_db=str(self.root / "scheduler.sqlite"),
            adapter_factory=lambda router: adapter, clock=lambda: MORNING,
            max_output_tokens=500, max_cost_usd=0.5,
        )

    def _mission(self, pools: dict | None = None) -> dict:
        wire = mission(pools, daily=5.0)
        wire["created_at"] = MORNING.isoformat(timespec="microseconds")
        return wire

    def test_the_served_links_cost_is_settled_into_the_admissions_pool(self) -> None:
        adapter = ChainAdapter({
            "profile:deepseek-v4-flash": BrokerDefinitelyNotSent("connect failed")
        })
        answer = self._model(adapter).call(
            purpose="claim_index", request_id="one", prompt="tag these",
            mission=self._mission(),
        )
        self.assertEqual(answer["text"], "answered by profile:zai-glm-5-3-flash")
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as ledger:
            row = ledger.connection.execute(
                "SELECT pool, actual_micros FROM thesis_impact_day_settlements"
            ).fetchone()
            admission = ledger.connection.execute(
                "SELECT pool, pool_lane FROM thesis_impact_day_admissions"
            ).fetchone()
        # claim_index is catalog work, so the pool is maintenance at both ends
        # -- and the cost is the second link's, not the first's.
        self.assertEqual(admission["pool"], "maintenance")
        self.assertEqual(admission["pool_lane"], "claim_index")
        self.assertEqual(row["pool"], "maintenance")
        self.assertEqual(row["actual_micros"], answer["cost_micros"])

    def test_a_spent_pool_is_a_skip_with_a_word_a_lane_can_report(self) -> None:
        empty = {"coverage": 5.0, "event_response": 0, "adhoc": 0, "maintenance": 0}
        adapter = ChainAdapter({})
        with self.assertRaises(CockpitModelPoolExhausted) as caught:
            self._model(adapter).call(
                purpose="claim_index", request_id="two", prompt="tag these",
                mission=self._mission(empty),
            )
        self.assertEqual(caught.exception.lane_status, POOL_EXHAUSTED_STATUS)
        self.assertEqual(caught.exception.pool, "maintenance")
        self.assertEqual(caught.exception.cap, 0)
        # Nothing was called, nothing was charged, and the refusal is durable.
        self.assertEqual(adapter.served, [])
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as ledger:
            self.assertEqual(
                ledger.connection.execute(
                    "SELECT COUNT(*) FROM thesis_impact_day_settlements"
                ).fetchone()[0], 0)
            refusals = json.loads(
                ledger.connection.execute(
                    "SELECT record_json FROM model_budget_pool_rejections"
                ).fetchone()["record_json"])
        self.assertEqual(refusals["pool_lane"], "claim_index")

    def test_the_single_shot_route_carries_its_pool_too(self) -> None:
        adapter = ChainAdapter({})
        answer = self._model(adapter, policy=self.pinned).call(
            purpose="plan", request_id="three", prompt="what next?",
            mission=self._mission(),
        )
        self.assertEqual(answer["text"], "answered by profile:deepseek-v4-flash")
        self.assertEqual(answer["cost_status"], "estimated")
        with ThesisImpactBudgetStore(self.root / "budget.sqlite") as ledger:
            admission = ledger.connection.execute(
                "SELECT pool, pool_lane FROM thesis_impact_day_admissions"
            ).fetchone()
            settlement = ledger.connection.execute(
                "SELECT pool, actual_micros FROM thesis_impact_day_settlements"
            ).fetchone()
        self.assertEqual((admission["pool"], admission["pool_lane"]),
                         ("coverage", "plan"))
        self.assertEqual(settlement["pool"], "coverage")
        self.assertEqual(settlement["actual_micros"], answer["cost_micros"])

    def test_the_single_shot_route_skips_rather_than_fails_on_a_spent_pool(self) -> None:
        # Every pool at zero, so the answer does not depend on what time of
        # day the suite happens to run: coverage may borrow, but only from a
        # pool that has something to lend.
        empty = {name: 0 for name in POOL_NAMES}
        adapter = ChainAdapter({})
        with self.assertRaises(CockpitModelPoolExhausted) as caught:
            self._model(adapter, policy=self.pinned).call(
                purpose="plan", request_id="four", prompt="what next?",
                mission=self._mission(empty),
            )
        self.assertEqual(caught.exception.lane_status, POOL_EXHAUSTED_STATUS)
        self.assertEqual(caught.exception.pool, "coverage")
        self.assertEqual(adapter.served, [])


class LaneStatusTests(unittest.TestCase):
    """What a lane says when the cockpit refuses for want of pool, not route."""

    def refusal(self, pool: str = "coverage") -> CockpitModelPoolExhausted:
        return CockpitModelPoolExhausted(
            "the coverage pool is spent",
            {"pool": pool, "day": DAY, "spent": 10, "cap": 10,
             "reason": "pool_exhausted"},
        )

    def test_a_pool_refusal_keeps_its_word_and_anything_else_falls_back(self) -> None:
        self.assertEqual(
            lane_status_for(self.refusal(), "model_unavailable"),
            POOL_EXHAUSTED_STATUS)
        self.assertEqual(
            lane_status_for(CockpitModelError("no route"), "model_unavailable"),
            "model_unavailable")

    def test_the_planner_child_reports_a_budget_decision_not_an_outage(self) -> None:
        # End to end through the real child: the cockpit call raises, and the
        # summary the tick's parent reads says which kind of no it was.
        from unittest.mock import patch

        from dalton_core.coverage_mission import CoverageMissionAuthority
        from dalton_core.research_planner_cli import run_planner
        from dalton_core.store import DaltonStore
        from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir(mode=0o700)
            store = DaltonStore(str(state / "core.sqlite"))
            missions = CoverageMissionAuthority(store)
            fixtures = bootstrap_method_authorities(store)
            params = mission_params(fixtures)
            missions.create_mission(params.pop("mission_ref"), **params)
            store.close()
            config = root / "model.json"
            config.write_text("{}", encoding="utf-8")
            with patch("dalton_core.research_planner_cli.CockpitModel") as model:
                model.return_value.call.side_effect = self.refusal()
                summary = run_planner(
                    state_dir=state, model_config_path=config,
                    summary_dir=root / "out", scheduler_db=None,
                    plans_dir=state / "discovery-plans", dry_run=False,
                )
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["plan_status"], POOL_EXHAUSTED_STATUS)
        self.assertIn("CockpitModelPoolExhausted", summary["failure_reason"])

    def test_the_judgement_lane_says_which_gate_stopped_it(self) -> None:
        # P14a keeps its own book of what this lane spent and gates on it; the
        # day ledger's event_response pool is the same money from the other
        # side and can refuse first. Both say the same word.
        from dalton_core.event_judgement import POOL_NAME, POOL_SHARE

        self.assertEqual(POOL_NAME, "event_response")
        self.assertEqual(POOL_SHARE, DEFAULT_SHARES["event_response"])
        self.assertEqual(budget_pools.PURPOSE_POOLS["event_judgement"], POOL_NAME)
        self.assertEqual(budget_pools.PURPOSE_POOLS["thesis_reflection"], POOL_NAME)
        self.assertEqual(
            pool_for_operation("dispatch_event_judgement"), POOL_NAME)


class PreC2ReplayTests(unittest.TestCase):
    """A call admitted before the migration, replayed after it."""

    def test_an_admission_without_pool_keys_replays_with_them(self) -> None:
        store = ThesisImpactBudgetStore(clock=lambda: MORNING)
        self.addCleanup(store.close)
        store.register_policy(policy_version_id=POLICY, day_cap_micros=100_000_000)
        wire = mission()
        scope = {
            "mission_ref": wire["mission_ref"],
            "mission_version_ref": wire["id"],
            "mission_version_hash": wire["content_hash"],
            "max_daily_paid_calls": 100,
            "max_daily_cost_micros": 10_000_000,
        }
        args = dict(
            policy_version_id=POLICY, day=DAY, work_order_ref="work:inflight",
            attempt_number=1, phase="assessment", route_decision_ref="route:1",
            reserved_micros=1_000,
        )
        before = store.admit(**args, mission_binding=scope)
        self.assertEqual(before["status"], "fresh")
        after = store.admit(
            **args,
            mission_binding={**scope, **mission_pool_scope(wire, pool="coverage")},
        )
        self.assertEqual(after["status"], "duplicate")
        self.assertEqual(after["admission_id"], before["admission_id"])

    def test_a_binding_that_really_changed_is_still_a_conflict(self) -> None:
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetConflict

        store = ThesisImpactBudgetStore(clock=lambda: MORNING)
        self.addCleanup(store.close)
        store.register_policy(policy_version_id=POLICY, day_cap_micros=100_000_000)
        wire = mission()
        scope = {
            "mission_ref": wire["mission_ref"],
            "mission_version_ref": wire["id"],
            "mission_version_hash": wire["content_hash"],
            "max_daily_paid_calls": 100,
            "max_daily_cost_micros": 10_000_000,
        }
        args = dict(
            policy_version_id=POLICY, day=DAY, work_order_ref="work:changed",
            attempt_number=1, phase="assessment", route_decision_ref="route:1",
            reserved_micros=1_000,
        )
        store.admit(**args, mission_binding=scope)
        with self.assertRaises(ThesisImpactBudgetConflict):
            store.admit(**args, mission_binding={**scope, "max_daily_paid_calls": 7})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
