"""P13-M3: the sensitivity lane on a tick, and where it is registered.

The lane has no queue: what needs doing is a company whose driver model or
whose street estimate no longer matches the projection it already has, which is
one hash comparison. Its resting state is silence and nothing can get stuck in
it, so what these tests hold to account is that it settles the previous child
before starting another, that a run which failed does not consume the slot
every five minutes, and that it goes quiet once every table matches its model.

The registration tests are the other half. A lane that computes correctly and
is not wired into the deploy is a lane that will be discovered missing on a
live Core, several minutes after ``install.sh`` exited zero.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dalton_core.bootstrap import SCHEMA_DATABASES
from dalton_core.budget_pools import LANE_POOLS
from dalton_core.cockpit_plane import REGISTRY_LANE_LABELS
from dalton_core.forecast_sensitivity import (
    SensitivityProjectionAuthority,
    build_projection,
    fingerprint,
)
from dalton_core.forecast_sensitivity_cli import (
    missing_write_scope,
    pending_companies,
    projection_fingerprint,
)
from dalton_core.forecast_sensitivity_launcher import ForecastSensitivityLauncher
from dalton_core.lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from dalton_core.lane_registry import (
    LANE_MODULES,
    LaunchAgentContext,
    lane_for_operation,
)
from dalton_core.mission_sensitivity_lane import (
    LANE,
    LAUNCHER_KWARG,
    MissionSensitivityLaneCoordinator,
    argv_fragment,
    build_launcher,
)
from dalton_core.model_forecast_driver import ForecastModelAuthority
from dalton_core.store import DaltonStore
from tests.test_company_model_inputs import ACN
from tests.test_forecast_sensitivity import model

OPERATION = "dispatch_forecast_sensitivity"
DIGEST = "a" * 64


class FakeLauncher:
    def __init__(self):
        self.tickets: dict[str, dict] = {}
        self.started: list[dict] = []
        self.raise_on_start: Exception | None = None

    def start(self, *, company_ref, projection_digest):
        if self.raise_on_start is not None:
            raise self.raise_on_start
        # What the real launcher does: one child at a time, and a caller that
        # asks for a second while one is running gets a conflict rather than
        # two children racing to write the same projection chain.
        if any(item["status"] == "running" for item in self.tickets.values()):
            raise LaneChildConflict("a sensitivity child is already running")
        self.started.append({"company_ref": company_ref,
                             "projection_digest": projection_digest})
        ticket_id = f"forecast-sensitivity-run:{len(self.started):024d}"
        self.tickets[ticket_id] = {
            "id": ticket_id, "status": "running", "summary": None,
            "company_ref": company_ref, "projection_digest": projection_digest,
        }
        return {"id": ticket_id}

    def finish(self, ticket_id, *, status="succeeded", summary=None):
        self.tickets[ticket_id].update({"status": status, "summary": summary})

    def status(self, ticket_ref):
        if ticket_ref not in self.tickets:
            raise LaneChildTicketNotFound(ticket_ref)
        return self.tickets[ticket_ref]


class LaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.models = ForecastModelAuthority(self.store)
        self.projections = SensitivityProjectionAuthority(self.store)
        self.record = model()
        self.published = self.models.publish(
            {key: value for key, value in self.record.items()
             if key not in ("id", "content_hash")})
        self.launcher = FakeLauncher()
        self.mission = {"id": "coverage-mission-version:test:1",
                        "universe": [{"company_ref": ACN, "ticker": "ACN"}],
                        "autonomy": {"may_write": ["model_run"],
                                     "automation_principal": "automation:test"}}
        self.lane = MissionSensitivityLaneCoordinator(
            store=self.store, missions=None, models=self.models,
            projections=self.projections, launcher=self.launcher,
            mission=lambda: self.mission)

    def digest(self):
        return projection_fingerprint(self.store, self.models, ACN)[3]

    def publish_projection(self):
        record = self.models.latest(ACN)
        return self.projections.publish(build_projection(
            record, consensus_fingerprint=None, actor_ref="automation:test"))

    def test_a_company_with_no_projection_is_launched_and_named_by_what_it_is_of(self):
        outcome = self.lane.dispatch_once()
        self.assertEqual(outcome["status"], "launched")
        self.assertEqual(outcome["company_ref"], ACN)
        self.assertEqual(outcome["projection_digest"], self.digest())
        self.assertIsNone(outcome["settled"])
        self.assertEqual(self.launcher.started,
                         [{"company_ref": ACN, "projection_digest": self.digest()}])

    def test_the_previous_child_is_settled_before_another_starts(self):
        first = self.lane.dispatch_once()
        self.launcher.finish(first["ticket_ref"], summary={
            "sensitivity_status": "published", "projection_ref": "p:1",
            "drivers_selected": 4, "bridge_status": "unavailable"})
        self.publish_projection()
        second = self.lane.dispatch_once()
        self.assertEqual(second["status"], "idle")
        self.assertEqual(second["settled"]["sensitivity_status"], "published")
        self.assertEqual(second["settled"]["company_ref"], ACN)
        self.assertEqual(len(self.launcher.started), 1)

    def test_an_unchanged_model_and_an_unchanged_street_is_silence(self):
        self.publish_projection()
        outcome = self.lane.dispatch_once()
        self.assertEqual(outcome["status"], "idle")
        self.assertIn("matches its model", outcome["reason"])
        self.assertEqual(self.launcher.started, [])

    def test_a_child_still_running_is_reported_and_not_replaced(self):
        first = self.lane.dispatch_once()
        second = self.lane.dispatch_once()
        self.assertEqual(second["settled"],
                         {"status": "running", "ticket_ref": first["ticket_ref"]})
        self.assertEqual(second["status"], "busy")
        self.assertEqual(len(self.launcher.started), 1)

    def test_a_refused_run_is_held_rather_than_relaunched_every_tick(self):
        first = self.lane.dispatch_once()
        self.launcher.finish(first["ticket_ref"], summary={
            "sensitivity_status": "refused:the mission does not grant the "
                                  "model_run write scope"})
        held = self.lane.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertIn("model_run", held["reason"])
        self.assertEqual(len(self.launcher.started), 1)

    def test_a_failed_child_with_no_summary_spends_one_transient_retry(self):
        first = self.lane.dispatch_once()
        self.launcher.finish(first["ticket_ref"], status="orphaned")
        retried = self.lane.dispatch_once()
        self.assertEqual(retried["status"], "launched")
        self.assertEqual(retried["settled"]["failure"]["failure_class"], "transient")
        self.assertEqual(retried["settled"]["failure"]["failures"], 1)

    def test_dependency_failure_retries_same_projection_as_probe(self):
        first = self.lane.dispatch_once()
        self.launcher.finish(first["ticket_ref"], status="failed", summary={
            "failure_reason": "model_unavailable"})
        probe = self.lane.dispatch_once()
        self.assertEqual(probe["status"], "launched")
        self.assertEqual(probe["projection_digest"], first["projection_digest"])
        self.assertEqual(probe["settled"]["failure"]["failure_class"],
                         "dependency_unavailable")

    def test_a_busy_launcher_is_not_a_failed_tick(self):
        self.launcher.raise_on_start = LaneChildConflict("already running")
        outcome = self.lane.dispatch_once()
        self.assertEqual(outcome["status"], "busy")
        self.assertEqual(outcome["company_ref"], ACN)

    def test_a_rejected_launch_is_reported_by_name(self):
        self.launcher.raise_on_start = LaneChildRejected("no company")
        self.assertEqual(self.lane.dispatch_once()["status"], "rejected")

    def test_without_a_mission_the_lane_does_nothing(self):
        self.lane.mission = lambda: None
        self.assertEqual(self.lane.dispatch_once()["status"], "unconfigured")

    def test_a_company_outside_the_universe_is_not_projected(self):
        self.mission = dict(self.mission, universe=[
            {"company_ref": "company:sec-cik:0000051143", "ticker": "IBM"}])
        self.assertEqual(self.lane.dispatch_once()["status"], "idle")
        self.assertEqual(self.launcher.started, [])

    def test_an_unreadable_ledger_is_one_lane_failing_not_the_tick(self):
        class Broken:
            def companies(self):
                raise RuntimeError("the table is gone")

        self.lane.models = Broken()
        outcome = self.lane.dispatch_once()
        self.assertEqual(outcome["status"], "unavailable")
        self.assertIn("the table is gone", outcome["reason"])


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.models = ForecastModelAuthority(self.store)
        self.projections = SensitivityProjectionAuthority(self.store)
        record = model()
        self.models.publish({key: value for key, value in record.items()
                             if key not in ("id", "content_hash")})
        self.mission = {"id": "m", "universe": [{"company_ref": ACN}]}

    def pending(self, **kwargs):
        return pending_companies(self.store, None, self.models, self.projections,
                                 self.mission, **kwargs)

    def test_a_company_whose_model_moved_is_pending_again(self):
        self.assertEqual([row[0] for row in self.pending()], [ACN])
        record = self.models.latest(ACN)
        self.projections.publish(build_projection(record, actor_ref="automation:t"))
        self.assertEqual(self.pending(), [])

    def test_the_fingerprint_is_the_model_hash_and_the_street_together(self):
        record = self.models.latest(ACN)
        _, consensus, street, digest = projection_fingerprint(
            self.store, self.models, ACN)
        self.assertIsNone(street)
        self.assertEqual(digest, fingerprint(record, None))
        self.assertEqual(consensus["status"], "unavailable")

    def test_a_hand_run_still_goes_through_the_universe(self):
        with self.assertRaises(Exception) as caught:
            self.pending(company_ref="company:sec-cik:0000051143")
        self.assertIn("universe", str(caught.exception))

    def test_the_write_scope_is_model_run_and_not_forecast_line(self):
        self.assertIsNone(missing_write_scope(
            {"autonomy": {"may_write": ["model_run"]}}))
        refusal = missing_write_scope(
            {"autonomy": {"may_write": ["forecast_line"]}})
        self.assertIn("model_run", refusal)


class RegistrationTests(unittest.TestCase):
    def test_the_lane_is_registered_after_the_driver_model_lane(self):
        self.assertEqual(LANE.operation, OPERATION)
        self.assertEqual(LANE.order, 96)
        self.assertEqual(LANE.driver_key, "forecast_sensitivity")
        self.assertEqual(LANE.init_kwarg, LAUNCHER_KWARG)
        forecast = lane_for_operation("dispatch_company_model_forecast")
        self.assertEqual(forecast.order, 95)
        self.assertLess(forecast.order, LANE.order)

    def test_the_module_is_named_in_the_import_list(self):
        self.assertIn("dalton_core.mission_sensitivity_lane", LANE_MODULES)

    def test_the_lane_is_off_until_there_is_a_core_to_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.assertEqual(argv_fragment(LaunchAgentContext(state=state)), [])
            (state / "core.sqlite").write_bytes(b"")
            self.assertEqual(argv_fragment(LaunchAgentContext(state=state)),
                             ["--forecast-sensitivity-lane"])

    def test_the_launcher_is_built_only_by_its_own_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = type("Args", (), {"db": str(Path(tmp) / "core.sqlite"),
                                     "forecast_sensitivity_lane": False})()
            self.assertIsNone(build_launcher(args))
            args.forecast_sensitivity_lane = True
            launcher = build_launcher(args)
            self.assertIsInstance(launcher, ForecastSensitivityLauncher)
            self.assertTrue(launcher.configured)

    def test_a_run_is_named_by_the_company_and_what_the_table_is_of(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = ForecastSensitivityLauncher(state_dir=Path(tmp))
            with self.assertRaises(LaneChildRejected):
                launcher.start(company_ref="", projection_digest=DIGEST)
            with self.assertRaises(LaneChildRejected):
                launcher.start(company_ref=ACN, projection_digest="short")

    def test_the_lane_drinks_from_a_named_pool(self):
        self.assertEqual(LANE_POOLS[OPERATION], "coverage")

    def test_the_cockpit_has_a_word_for_it(self):
        self.assertIn("forecast_sensitivity", REGISTRY_LANE_LABELS)
        self.assertTrue(REGISTRY_LANE_LABELS["forecast_sensitivity"].strip())

    def test_the_schema_is_applied_at_install_rather_than_on_the_first_tick(self):
        # A schema that is only applied when a lane first constructs its
        # authority fails on a live Core minutes after install.sh exited zero,
        # as one lane reporting OperationalError in a heartbeat nobody watches.
        self.assertIn(("forecast_sensitivity_schema.sql", None), SCHEMA_DATABASES)

    def test_the_deploy_rehearsal_runs_this_migration(self):
        from scripts.rehearse_deploy import CORE_MIGRATIONS

        named = {item.schema for item in CORE_MIGRATIONS}
        self.assertIn("forecast_sensitivity_schema.sql", named)
        found = next(item for item in CORE_MIGRATIONS
                     if item.schema == "forecast_sensitivity_schema.sql")
        self.assertEqual(found.module, "dalton_core.forecast_sensitivity")
        self.assertEqual(found.symbol, "SensitivityProjectionAuthority")
        self.assertEqual(found.kind, "core")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
