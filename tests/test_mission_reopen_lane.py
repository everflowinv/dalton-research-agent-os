"""P14d's weekly check: assess, propose once, and otherwise say nothing."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

from dalton_core.deliverable_reopen import GateReopenAuthority
from dalton_core.lane_registry import LANE_MODULES, load_lanes, registered_lanes
from dalton_core.mission_reopen_lane import (
    CHECKPOINT_KIND,
    LANE,
    LANE_STATE_KEY,
    MissionReopenLaneCoordinator,
    main,
    may_propose_reopen,
    passed_companies,
)
from tests.p14a_fixtures import ACN, AUTOMATION, CTSH, OWNER
from tests.test_deliverable_reopen import ReopenHarness


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class RegistrationTests(unittest.TestCase):
    def test_the_lane_is_registered_once_at_its_own_order(self):
        load_lanes()
        self.assertIn("dalton_core.mission_reopen_lane", LANE_MODULES)
        lanes = {spec.operation: spec for spec in registered_lanes()}
        self.assertIs(lanes["dispatch_mission_reopen"], LANE)
        self.assertEqual(LANE.order, 118)
        self.assertEqual(LANE.driver_key, "mission_reopen")
        # Between the judgement lane and the first feed lane, and with no
        # launcher: it spawns nothing, so it declares nothing to spawn.
        orders = sorted(spec.order for spec in registered_lanes())
        self.assertEqual(len(orders), len(set(orders)))
        self.assertIsNone(LANE.launcher_factory)
        self.assertIsNone(LANE.init_kwarg)
        self.assertIsNone(LANE.argv_fragment)
        # It makes no model call, so it declares no budget pool.
        self.assertIsNone(LANE.budget_pool)

    def test_the_lane_has_a_name_a_person_can_read(self):
        from dalton_core.cockpit_plane import REGISTRY_LANE_LABELS

        self.assertIn("mission_reopen", REGISTRY_LANE_LABELS)
        self.assertTrue(REGISTRY_LANE_LABELS["mission_reopen"])


class GrantTests(unittest.TestCase):
    def test_the_grant_that_matters_is_the_checkpoint(self):
        self.assertFalse(may_propose_reopen(None))
        self.assertFalse(may_propose_reopen({}))
        self.assertFalse(may_propose_reopen({"autonomy": {"human_checkpoints": []}}))
        self.assertFalse(may_propose_reopen(
            {"autonomy": {"human_checkpoints": "gate_reopen"}}))
        self.assertTrue(may_propose_reopen(
            {"autonomy": {"human_checkpoints": [CHECKPOINT_KIND]}}))


class LaneTests(ReopenHarness):
    def setUp(self):
        super().setUp()
        self.clock = Clock(datetime(2026, 9, 21, tzinfo=timezone.utc))
        self.version = self.publish()
        self.pass_gate(self.version)

    def coordinator(self, mission=None):
        record = mission if mission is not None else self.mission
        return MissionReopenLaneCoordinator(
            mission=lambda: record, connection=self.store.connection,
            authority=GateReopenAuthority(self.store), clock=self.clock,
        )

    def test_a_mission_without_the_checkpoint_proposes_nothing_and_says_why(self):
        result = self.coordinator().dispatch_once()
        self.assertEqual(result["status"], "ungranted")
        self.assertIn(CHECKPOINT_KIND, result["reason"])
        self.assertEqual(self.reopens.proposals(), [])

    def test_no_mission_at_all_is_unconfigured_rather_than_an_error(self):
        coordinator = MissionReopenLaneCoordinator(
            mission=lambda: None, connection=self.store.connection,
            authority=self.reopens, clock=self.clock,
        )
        self.assertEqual(coordinator.dispatch_once()["status"], "unconfigured")

    def test_only_companies_whose_screen_passed_are_looked_at(self):
        self.assertEqual(passed_companies(self.store.connection, self.mission), [ACN])
        self.grant(checkpoints=(CHECKPOINT_KIND,))
        result = self.coordinator().dispatch_once()
        self.assertEqual([entry["company_ref"] for entry in result["looked"]], [ACN])
        self.assertNotIn(CTSH, json.dumps(result))

    def test_nothing_moved_means_nothing_proposed(self):
        self.grant(checkpoints=(CHECKPOINT_KIND,))
        result = self.coordinator().dispatch_once()
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["proposed"], [])
        self.assertEqual(result["looked"][0]["status"], "no_flip")

    def test_one_proposal_a_week_and_the_ledger_holds_the_line_after_a_restart(self):
        self.grant(checkpoints=(CHECKPOINT_KIND,))
        self.thicken(lines=250)
        coordinator = self.coordinator()
        first = coordinator.dispatch_once()
        self.assertEqual(first["status"], "proposed")
        self.assertEqual(len(first["proposed"]), 1)
        self.assertEqual(first["looked"][0]["flipped"], ["statements"])

        # Same week, same process: held without redoing the arithmetic.
        held = coordinator.dispatch_once()
        self.assertEqual(held["held"], 1)
        self.assertEqual(held["proposed"], [])

        # Next week, and a new process that has forgotten everything: the
        # authority's (company, assessment hash) key is what actually holds.
        self.clock.advance(days=8)
        restarted = self.coordinator().dispatch_once()
        self.assertEqual(restarted["proposed"], [])
        self.assertEqual(restarted["looked"][0]["status"], "already_proposed")
        self.assertEqual(len(self.reopens.proposals(ACN)), 1)

        # Evidence that thickened again is a different assessment, and a
        # second proposal is the right answer to it.
        self.thicken(lines=140)
        self.clock.advance(days=8)
        again = self.coordinator().dispatch_once()
        self.assertEqual(len(again["proposed"]), 1)
        self.assertEqual(len(self.reopens.proposals(ACN)), 2)

    def test_the_dispatch_handler_builds_its_coordinator_once(self):
        self.grant(checkpoints=(CHECKPOINT_KIND,))
        from dalton_core.mission_reopen_lane import dispatch

        class FakeServer:
            def __init__(self, store, missions):
                self.store = store
                self.lane_state = {}
                self.coverage_mission = missions

        server = FakeServer(self.store, self.missions)
        first = dispatch(server, {})
        self.assertEqual(first["status"], "idle")
        held = server.lane_state[LANE_STATE_KEY]
        dispatch(server, {})
        self.assertIs(server.lane_state[LANE_STATE_KEY], held)

    def test_a_writer_with_no_core_is_unconfigured(self):
        from dalton_core.mission_reopen_lane import dispatch

        class Bare:
            store = None
            lane_state: dict = {}

        self.assertEqual(dispatch(Bare(), {})["status"], "unconfigured")


class CliTests(ReopenHarness):
    def test_the_cli_reads_the_core_and_writes_nothing(self):
        version = self.publish()
        self.pass_gate(version)
        self.thicken(lines=250)
        path = str(self.state_dir / "core.sqlite")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(main(["--core", path, "--json"]), 0)
        assessments = json.loads(buffer.getvalue())
        self.assertEqual([a["company_ref"] for a in assessments], [ACN])
        self.assertEqual(assessments[0]["flipped"], ["statements"])
        self.assertEqual(self.reopens.proposals(), [])

        plain = io.StringIO()
        with redirect_stdout(plain):
            main(["--core", path, "--company", ACN])
        text = plain.getvalue()
        self.assertIn("翻了", text)
        self.assertIn("缺(0) -> 有", text)

        empty = io.StringIO()
        with redirect_stdout(empty):
            main(["--core", path, "--company", CTSH])
        self.assertIn("not_passed", empty.getvalue())


if __name__ == "__main__":
    unittest.main()
