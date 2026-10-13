"""2026-10-04: a new plan's query is a new search, and a due search is not starved.

ws-7d, 2026-10-03 19:00Z: the owner approved AlphaEngine plan v3, whose first
transcript query is "{name} {quarter_label}" ("Alphabet Q3 2025"). Sixteen
hours later no v3 search had been dispatched:

* the cadence looked at the last dispatch for (company, spec) whatever plan
  made it, so the v2 searches of 03:53-04:58Z held v3 off for the rediscovery
  interval; and
* the AlphaEngine coordinator ended 172 of 192 ticks "deferred: tick budget
  exhausted". Measured on read-only copies, settlement took well under half a
  second; the share went to the acquisition step, whose budget check counted
  trailing-24h calls by scanning every connector attempt on this Core and on
  the peer Core (2.8 s and 4.4 s cold), and the search was never reached.

The tick ledger now keeps per-step seconds and why the search did not launch.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core import mission_source_discovery as msd
from dalton_core.connector import ConnectorStore
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.mission_source_discovery import (
    DISCOVERY_DEFERRED_REASON,
    SEARCH_STARVATION_TICKS,
    MissionSourceDiscoveryCoordinator,
    build_discovery_plan,
    discovery_coordinator_state,
    search_blocked_reason,
)
from dalton_core.store import DaltonStore
from dalton_core.tick_ledger import bounded_counts
# Through the module, so this file does not collect the harness's tests again.
from tests import test_mission_source_discovery as base
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

SPEC = "earnings-call-transcripts"
V2_VARIANTS = [
    {"query_template": "{name} {quarter} earnings call transcript", "filters": {}},
    {"query_template": "{aliases} earnings call", "filters": {}},
]
V3_VARIANTS = [{"query_template": "{name} {quarter_label}", "filters": {}}, *V2_VARIANTS]


class Selection:
    """Nothing selected or consumed: a page waits for selection, as on live."""

    def currently_consumed(self, **_kwargs):
        return []

    def current_selections(self, **_kwargs):
        return {}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class PlanChangeCadenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.clock = base.Clock()
        self.h = base.SearchHarness(Path(temp.name), [
            {"doc_id": base.NEW_DOC.split(":")[1]}], clock=self.clock)
        self.addCleanup(self.h.close)
        self.state = bootstrap_method_authorities(self.h.core)
        self.missions = CoverageMissionAuthority(self.h.core)
        params = mission_params(self.state)
        v1 = self.missions.create_mission(params.pop("mission_ref"), **params)
        params = mission_params(self.state)
        params["autonomy"]["may_write"] = list(params["autonomy"]["may_write"]) + ["source_discovery"]
        for item in params["source_plan"]:
            if item["source_ref"] == "source:alphaengine":
                item["status"] = "connected"
        params.update({"version_id": "coverage-mission-version:us-it-services:2",
                       "prior_version_ref": v1["id"],
                       "idempotency_key": "coverage-mission:us-it-services:2"})
        self.mission = self.missions.create_mission(params.pop("mission_ref"), **params)
        self.company = self.mission["universe"][0]["company_ref"]

    def plan(self, plan_id: str, variants: list[dict]) -> dict:
        return build_discovery_plan(
            plan_id=plan_id, created_at=base.NOW.isoformat(timespec="microseconds"),
            mission_ref="coverage-mission:us-it-services", source_ref="source:alphaengine",
            max_calls_24h=30,
            companies={self.company: {"name": "Alphabet", "ticker": "GOOGL",
                                      "aliases": ["Google"]}},
            specs=[{"spec_ref": SPEC, "document_type": "meeting_minutes",
                    "query_variants": variants, "lookback_days": 400,
                    "rediscovery_interval_days": 7, "retry_interval_days": 1}])

    def coordinator(self, plan: dict) -> tuple[MissionSourceDiscoveryCoordinator, object]:
        search = base.FakeSearchLauncher(self.h, self.missions, plan)
        return MissionSourceDiscoveryCoordinator(
            store=self.h.core, missions=self.missions, plan=plan,
            search_launcher=search, acquisition_launcher=None, clock=self.clock,
            selection_launcher=Selection()), search

    def search_once(self, plan: dict, *, fail: bool = False):
        coordinator, search = self.coordinator(plan)
        search.fail_next = fail
        result = coordinator.launch_discovery()
        self.assertEqual(result["status"], "launched", result)
        coordinator.settle_dispatches()
        return search

    def test_found_quarter_advances_real_request_and_does_not_repeat_next_tick(self):
        for company in self.mission["universe"][:2]:
            with self.subTest(company=company["company_ref"]):
                self.company = company["company_ref"]
                plan = self.plan("discovery-plan:targets:" + company["ticker"].lower(),
                                 [V3_VARIANTS[0]])
                coordinator, search = self.coordinator(plan)
                gaps = ("FY2026-Q1", "FY2026-Q2")
                with patch.object(coordinator, "_period_state", return_value=(gaps, frozenset())):
                    self.assertEqual(coordinator.launch_discovery()["status"], "launched")
                    coordinator.settle_dispatches()
                self.clock.advance(hours=1)
                with patch.object(coordinator, "_period_state", return_value=(gaps, frozenset({gaps[0]}))):
                    moved = coordinator.launch_discovery()
                    self.assertEqual(moved["status"], "launched", moved)
                    self.assertEqual(search.starts[-1]["missing_periods"], [gaps[1]])
                    self.assertIsNone(search.starts[-1]["cursor"])
                    coordinator.settle_dispatches()
                    discoveries = self.missions.source_discoveries(
                        self.mission["id"], company_ref=self.company, spec_ref=SPEC)
                    self.assertEqual(discoveries[0]["parameters"]["query"], "Alphabet Q2 2026")
                    with patch.object(coordinator.selection_launcher, "currently_consumed", return_value=[]) as selected:
                        held = coordinator.launch_discovery()
                    self.assertEqual(held["status"], "idle", held)
                    self.assertEqual(len(search.starts), 2)
                    self.assertEqual(selected.call_args.kwargs["missing_periods_by_company"],
                                     {self.company: list(gaps)})
                    restarted, restarted_search = self.coordinator(plan)
                    with patch.object(restarted, "_period_state", return_value=(gaps, frozenset({gaps[0]}))):
                        self.assertEqual(restarted.launch_discovery()["status"], "idle")
                        self.assertEqual(restarted_search.starts, [])

    def test_all_missing_quarters_wait_for_candidates_without_bare_name_search(self):
        coordinator, search = self.coordinator(self.plan("discovery-plan:all-candidates", V3_VARIANTS))
        gaps = ("FY2026-Q1", "FY2026-Q2")
        with patch.object(coordinator, "_period_state", return_value=(gaps, frozenset(gaps))):
            held = coordinator.launch_discovery()
            self.assertEqual(held["status"], "idle", held)
            self.assertIn("候选资料", held["skipped"][0]["reason"])
            self.assertEqual(search.starts, [])
        # A failed acquisition stops being a candidate in the stage projection.
        with patch.object(coordinator, "_period_state", return_value=(gaps, frozenset({gaps[1]}))):
            self.assertEqual(coordinator.launch_discovery()["status"], "launched")
            self.assertEqual(search.starts[-1]["missing_periods"], [gaps[0]])

    def test_a_new_plan_with_a_new_first_query_is_searched_at_once(self) -> None:
        v2 = self.plan("discovery-plan:ws:alphaengine:2", V2_VARIANTS)
        v3 = self.plan("discovery-plan:ws:alphaengine:3", V3_VARIANTS)
        self.search_once(v2)
        self.clock.advance(hours=14)
        # The old plan is held by its own cadence, as before ...
        old, _ = self.coordinator(v2)
        held = old.launch_discovery()
        self.assertEqual(held["status"], "idle", held)
        self.assertIn("rediscovered", held["skipped"][0]["reason"])
        # ... and the approved v3 asks its new first question now.
        new, search = self.coordinator(v3)
        launched = new.launch_discovery()
        self.assertEqual(launched["status"], "launched", launched)
        self.assertEqual(search.starts[-1]["variant_index"], 0)
        self.assertEqual(search.starts[-1]["cursor"], None)
        new.settle_dispatches()
        # Once v3 has searched, v3's own cadence applies again: no hot loop.
        again = new.launch_discovery()
        self.assertEqual(again["status"], "idle", again)
        self.assertIn("rediscovered", again["skipped"][0]["reason"])

    def test_a_plan_change_that_left_the_first_query_alone_keeps_the_cadence(self) -> None:
        v2 = self.plan("discovery-plan:ws:alphaengine:2", V2_VARIANTS)
        # A new plan id (and hash) with this spec's queries unchanged.
        v2b = self.plan("discovery-plan:ws:alphaengine:2b", V2_VARIANTS)
        self.assertNotEqual(v2["content_hash"], v2b["content_hash"])
        self.search_once(v2)
        self.clock.advance(hours=14)
        coordinator, search = self.coordinator(v2b)
        result = coordinator.launch_discovery()
        self.assertEqual(result["status"], "idle", result)
        self.assertIn("rediscovered", result["skipped"][0]["reason"])
        self.assertEqual(search.starts, [])

    def test_a_failure_under_the_old_plan_still_owns_the_retry_interval(self) -> None:
        v2 = self.plan("discovery-plan:ws:alphaengine:2", V2_VARIANTS)
        v3 = self.plan("discovery-plan:ws:alphaengine:3", V3_VARIANTS)
        self.search_once(v2, fail=True)
        self.clock.advance(hours=2)
        coordinator, search = self.coordinator(v3)
        result = coordinator.launch_discovery()
        self.assertEqual(result["status"], "idle", result)
        self.assertIn("retry interval", result["skipped"][0]["reason"])
        self.assertEqual(search.starts, [])
        self.assertIn("retry interval", search_blocked_reason(result))


class Stubbed(MissionSourceDiscoveryCoordinator):
    """A coordinator whose steps cost what the test says, on a fake clock."""

    def __init__(self, clock: Clock, *, held: float = 0.0, acquire: float = 0.0,
                 search: float = 0.5, settle: float = 0.0, carry: float = 0.0,
                 discovery: dict | None = None) -> None:
        self.clock_ = clock
        self.costs = {"held": held, "acquire": acquire, "search": search,
                      "settle": settle, "carry": carry}
        self.order: list[str] = []
        self.discovery = discovery or {"status": "launched", "dispatch_ref": "d"}
        self.tick_budget_seconds = 20.0
        self._retry_failures_now = False
        self._search_deferred_ticks = 0
        self._tick_shared_view = msd._NO_TICK_CACHE
        self.selection_launcher = None
        self.source_ref = "source:alphaengine"
        self.plan = {"id": "discovery-plan:x", "content_hash": "0" * 64,
                     "mission_ref": "coverage-mission:x"}
        outer = self

        class Missions:
            def backfill_document_reviews(self, *_a, **_k):
                outer.spend("held")
                return []

        self.missions = Missions()

    def spend(self, step: str) -> None:
        self.order.append(step)
        self.clock_.now += self.costs[step]

    def settle_dispatches(self, deadline=None):
        self.spend("settle")
        return []

    def recover_local_web_discoveries(self, **_k):
        return []

    def settle_documents(self, deadline=None):
        return []

    def carry_forward(self, deadline=None):
        self.spend("carry")
        return []

    def backfill_hosts(self, **_k):
        return {"status": "not_applicable"}

    def settle_already_held(self, deadline=None):
        # Not interruptible by the deadline, like a slow reconciliation.
        self.spend("held")
        return []

    def _acquire_within_budget(self, settled, deadline=None):
        self.spend("acquire")
        return [{"status": "busy"}], self.clock_.now >= deadline, settled

    def launch_discovery(self):
        self.spend("search")
        return dict(self.discovery)


class StarvationTests(unittest.TestCase):
    SHARE = 20.0 / 3

    def setUp(self) -> None:
        self.clock = Clock()
        guard = patch.object(msd, "_monotonic", self.clock)
        guard.start()
        self.addCleanup(guard.stop)

    def tick(self, coordinator: Stubbed) -> dict:
        started = self.clock.now
        result = coordinator.dispatch_once(started + self.SHARE)
        self.clock.now = started + 300.0
        return result

    def test_a_due_search_still_runs_when_acquisition_exhausts_the_share(self) -> None:
        # The live shape: acquisition alone spends more than the share.
        coordinator = Stubbed(self.clock, acquire=9.0)
        states = []
        for _ in range(SEARCH_STARVATION_TICKS + 1):
            coordinator.order.clear()
            states.append(discovery_coordinator_state(self.tick(coordinator)))
        for state in states[:SEARCH_STARVATION_TICKS]:
            self.assertEqual(state["status"], "deferred")
            self.assertEqual(state["search_blocked_reason"],
                             f"deferred: {DISCOVERY_DEFERRED_REASON}")
        last = states[-1]
        self.assertEqual(last["status"], "launched", last)
        self.assertTrue(last["search_first"])
        self.assertNotIn("search_blocked_reason", last)
        # The search ran before the acquisition that would have eaten its time.
        self.assertLess(coordinator.order.index("search"), coordinator.order.index("acquire"))
        # And the next tick goes back to acquisitions first.
        coordinator.order.clear()
        state = discovery_coordinator_state(self.tick(coordinator))
        self.assertNotIn("search_first", state)
        self.assertEqual(coordinator.order[-1], "acquire")

    def test_a_due_search_still_runs_when_settlement_exhausts_the_share(self) -> None:
        # The 2026-09-29 shape: reconciliation at the head of the tick.
        coordinator = Stubbed(self.clock, held=8.0)
        for _ in range(SEARCH_STARVATION_TICKS):
            self.assertEqual(discovery_coordinator_state(self.tick(coordinator))["status"],
                             "deferred")
        coordinator.order.clear()
        result = self.tick(coordinator)
        self.assertEqual(result["discovery"]["status"], "launched")
        self.assertEqual(coordinator.order[:2], ["settle", "search"])

    def test_the_search_first_tick_still_respects_the_deadline(self) -> None:
        # Settling its own dispatches already spent the share: nothing starts.
        coordinator = Stubbed(self.clock, settle=7.0)
        for _ in range(SEARCH_STARVATION_TICKS + 1):
            result = self.tick(coordinator)
        self.assertEqual(result["discovery"]["status"], "deferred")
        self.assertNotIn("search", coordinator.order)

    def test_a_healthy_tick_keeps_acquisition_first(self) -> None:
        coordinator = Stubbed(self.clock, acquire=1.0)
        for _ in range(4):
            coordinator.order.clear()
            self.tick(coordinator)
            self.assertEqual(coordinator.order[-2:], ["acquire", "search"])


class LedgerStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        guard = patch.object(msd, "_monotonic", self.clock)
        guard.start()
        self.addCleanup(guard.stop)

    def test_per_step_seconds_and_the_blocked_reason_reach_the_ledger(self) -> None:
        idle = {"status": "idle", "skipped": [
            {"company_ref": "company:ticker:googl", "spec_ref": SPEC,
             "reason": "rediscovered 1d ago; interval 7d"},
            {"company_ref": "company:ticker:meta", "spec_ref": "sell-side-reports",
             "reason": "broker_research already holds 9 of 3; not searching past 6"}]}
        coordinator = Stubbed(self.clock, settle=0.25, carry=0.5, held=1.0, acquire=2.0,
                              search=0.75, discovery=idle)
        result = coordinator.dispatch_once(self.clock.now + 20.0)
        self.assertEqual(result["tick_steps"]["seconds"], {
            "settle": 0.25, "carry": 0.5, "held": 2.0, "acquire": 2.0, "search": 0.75})
        state = discovery_coordinator_state(result)
        self.assertEqual(
            {key: state[key] for key in ("settle_seconds", "carry_seconds", "held_seconds",
                                         "acquire_seconds", "search_seconds")},
            {"settle_seconds": 0.25, "carry_seconds": 0.5, "held_seconds": 2.0,
             "acquire_seconds": 2.0, "search_seconds": 0.75})
        self.assertTrue(state["search_blocked_reason"].startswith(
            f"googl/{SPEC}: rediscovered 1d ago; interval 7d; meta/sell-side-reports"))
        kept = bounded_counts({"status": "idle", "coordinators": {"alphaengine": state}})
        recorded = kept["coordinators"]["alphaengine"]
        for key in ("settle_seconds", "carry_seconds", "held_seconds", "acquire_seconds",
                    "search_seconds", "search_blocked_reason"):
            self.assertEqual(recorded[key], state[key])
        # The lane's own twelve counted keys are not displaced by the new data.
        self.assertNotIn("tick_steps", bounded_counts(result))

    def test_a_coordinator_skipped_before_its_turn_says_why(self) -> None:
        state = discovery_coordinator_state({
            "status": "deferred", "reason": DISCOVERY_DEFERRED_REASON})
        self.assertEqual(state["search_blocked_reason"], DISCOVERY_DEFERRED_REASON)


class SharedWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.core = DaltonStore(str(Path(temp.name) / "core.sqlite"))
        self.addCleanup(self.core.close)
        ConnectorStore(self.core)

    def test_the_peer_spend_is_read_once_per_tick(self) -> None:
        coordinator = object.__new__(MissionSourceDiscoveryCoordinator)
        coordinator.store = self.core
        coordinator.clock = lambda: base.NOW
        coordinator._tick_shared_view = msd._NO_TICK_CACHE
        reads = []

        def view(path, *, as_of):
            reads.append(path)
            return {"cap": 100, "spent_elsewhere": len(reads)}

        with patch("dalton_core.shared_daily_budget.shared_alphaengine_view", view):
            coordinator._shared_alphaengine_window()
            coordinator._shared_alphaengine_window()
            self.assertEqual(len(reads), 2)  # outside a tick, every check reads
            coordinator._tick_shared_view = msd._TICK_CACHE_UNREAD
            first = coordinator._shared_alphaengine_window()
            second = coordinator._shared_alphaengine_window()
            self.assertEqual(len(reads), 3)
            self.assertEqual(first, second)

    def test_the_trailing_call_count_seeks_attempts_by_start_time(self) -> None:
        from dalton_core.bounded_alphaengine_probe import count_recent_alphaengine_calls

        statements: list[str] = []
        self.core.connection.set_trace_callback(statements.append)
        try:
            count_recent_alphaengine_calls(self.core.connection, as_of=base.NOW)
        finally:
            self.core.connection.set_trace_callback(None)
        sql = next(text for text in statements if "connector_physical_attempts" in text)
        details = [row[3] for row in self.core.connection.execute(
            f"EXPLAIN QUERY PLAN {sql}").fetchall()]
        joined = "\n".join(details)
        self.assertIn("idx_connector_physical_attempts_started_at", joined)
        self.assertFalse([d for d in details if d.startswith("SCAN a")], joined)


if __name__ == "__main__":
    unittest.main()
