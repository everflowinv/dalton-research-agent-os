"""P13-M2: the driver-model lane on a tick, and the launcher that names its runs.

The lane has no queue. What needs doing is derived from the ledger each tick --
a company with a specification and no model, or a model whose estimated quarter
has since been filed -- so its resting state is silence and nothing can get
stuck in it. What these tests hold to account is that it settles the previous
child before starting another, that a run which failed does not consume the
slot every five minutes, and that it goes quiet once there is nothing left.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dalton_core.company_model_forecast import model_digest
from dalton_core.company_model_inputs import build_model_inputs
from dalton_core.lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from dalton_core.lane_registry import LaunchAgentContext, lane_for_operation
from dalton_core.mission_model_forecast_lane import (
    LANE,
    LAUNCHER_KWARG,
    MissionModelForecastLaneCoordinator,
    argv_fragment,
    build_launcher,
)
from dalton_core.model_forecast_driver import ForecastModelAuthority, build_forecast_model
from dalton_core.model_forecast_launcher import ModelForecastLauncher
from dalton_core.store import DaltonStore
from tests.test_company_model_inputs import ACN, FakeMissions
from tests.test_model_forecast_driver import ledger, spec

DIGEST = "a" * 64


class LaneMissions(FakeMissions):
    """The statements ledger plus the two specification reads the lane makes."""

    def __init__(self, lines, specification):
        super().__init__(lines)
        self.specification = specification

    def company_model_specs(self, company_ref=None):
        return [{"company_ref": ACN, "spec_id": self.specification["spec_id"]}]

    def latest_company_model_spec(self, company_ref):
        return dict(self.specification) if company_ref == ACN else None


class TwoCompanyMissions:
    """Two companies, one of which cannot be modelled at all.

    IBM's live specification binds every revenue driver to nothing filed --
    adoption, price mix and rate mix genuinely are not in GAAP -- so its model
    cannot be built. It also sorts first.
    """

    def __init__(self, ledgers, specs):
        self.ledgers = dict(ledgers)
        self.specs = dict(specs)

    def company_model_specs(self, company_ref=None):
        return [{"company_ref": ref, "spec_id": self.specs[ref]["spec_id"]}
                for ref in sorted(self.specs)]

    def latest_company_model_spec(self, company_ref):
        spec = self.specs.get(company_ref)
        return None if spec is None else dict(spec)

    def statement_series_lines(self, company_ref, concept, statement=None):
        return [row for row in self.ledgers.get(company_ref, [])
                if row["concept"] == concept]

    def statement_filings(self, company_ref=None):
        refs = sorted(self.ledgers) if company_ref is None else [company_ref]
        return [{"ingest_id": f"ingest:{ref}", "company_ref": ref,
                 "entity_name": ref, "accession": "0001467373-26-000032",
                 "report_date": "2026-05-31", "form": "10-Q",
                 "line_count": len(self.ledgers.get(ref, []))}
                for ref in refs if self.ledgers.get(ref)]

    def statement_lines(self, ingest_id, statement=None):
        rows = self.ledgers.get(ingest_id.split(":", 1)[1], [])
        return [row for row in rows
                if statement is None or row["statement"] == statement]


class FakeLauncher:
    def __init__(self):
        self.tickets: dict[str, dict] = {}
        self.started: list[dict] = []
        self.raise_on_start: Exception | None = None

    def start(self, *, company_ref, model_digest):
        if self.raise_on_start is not None:
            raise self.raise_on_start
        self.started.append({"company_ref": company_ref, "model_digest": model_digest})
        ticket_id = f"model-forecast-run:{len(self.started):024d}"
        self.tickets[ticket_id] = {
            "id": ticket_id, "status": "running", "summary": None,
            "company_ref": company_ref, "model_digest": model_digest,
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
        self.spec = spec()
        self.missions = LaneMissions(ledger().lines, self.spec)
        self.models = ForecastModelAuthority(self.store)
        self.launcher = FakeLauncher()
        self.mission = {"id": "coverage-mission-version:test:1",
                        "universe": [{"company_ref": ACN, "ticker": "ACN"}]}
        self.lane = MissionModelForecastLaneCoordinator(
            missions=self.missions, models=self.models, launcher=self.launcher,
            mission=lambda: self.mission)

    def digest(self):
        return model_digest(self.spec, build_model_inputs(self.missions, self.spec))

    def test_a_company_with_no_model_is_launched_and_named_by_what_it_is_about(self):
        outcome = self.lane.dispatch_once()
        self.assertEqual(outcome["status"], "launched")
        self.assertEqual(outcome["company_ref"], ACN)
        self.assertEqual(outcome["model_digest"], self.digest())
        self.assertIsNone(outcome["settled"])
        self.assertEqual(self.launcher.started,
                         [{"company_ref": ACN, "model_digest": self.digest()}])

    def test_the_previous_child_is_settled_before_another_starts(self):
        first = self.lane.dispatch_once()
        self.launcher.finish(first["ticket_ref"], summary={
            "forecast_status": "published", "model_version_ref": "v:1",
            "forecast_lines_written": 4})
        # The model now exists, so the tick that settles is also the tick that
        # finds nothing left to do.
        self.models.publish(build_forecast_model(
            self.spec, build_model_inputs(self.missions, self.spec)))
        second = self.lane.dispatch_once()
        self.assertEqual(second["status"], "idle")
        self.assertEqual(second["settled"]["forecast_status"], "published")
        self.assertEqual(second["settled"]["company_ref"], ACN)
        self.assertEqual(self.launcher.started, [{"company_ref": ACN,
                                                  "model_digest": self.digest()}])

    def test_a_child_still_running_is_reported_and_not_replaced(self):
        first = self.lane.dispatch_once()
        second = self.lane.dispatch_once()
        self.assertEqual(second["settled"],
                         {"status": "running", "ticket_ref": first["ticket_ref"]})

    def test_a_refused_run_is_held_rather_than_relaunched_every_tick(self):
        first = self.lane.dispatch_once()
        self.launcher.finish(first["ticket_ref"], summary={
            "forecast_status": "refused:the mission does not grant the "
                               "forecast_line write scope"})
        held = self.lane.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertIn("forecast_line", held["reason"])
        self.assertEqual(len(self.launcher.started), 1)

    def test_an_economic_invariant_refusal_is_held_like_any_other_refusal(self):
        # P17b. The run *succeeded*: the gate refused, the reasons are on the
        # record, nothing was published. Relaunching the identical digest next
        # tick would refuse identically, so this holds until the assumptions
        # move.
        first = self.lane.dispatch_once()
        self.launcher.finish(first["ticket_ref"], summary={
            "forecast_status": "unavailable:economic_invariants",
            "failure_reason": "assumption_band: assumption:x assumes 0.20, "
                              "outside the filed range 0.78 to 0.82"})
        held = self.lane.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertIn("outside the filed range", held["reason"])
        self.assertEqual(len(self.launcher.started), 1)

    def test_a_failed_child_with_no_summary_is_still_attributable(self):
        first = self.lane.dispatch_once()
        self.launcher.finish(first["ticket_ref"], status="orphaned")
        held = self.lane.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertIn("orphaned", held["reason"])

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

    def test_a_company_outside_the_universe_is_not_modelled(self):
        self.mission = {"id": "m", "universe": [
            {"company_ref": "company:sec-cik:0000051143", "ticker": "IBM"}]}
        self.assertEqual(self.lane.dispatch_once()["status"], "idle")


class StarvationTests(unittest.TestCase):
    """One company that cannot be modelled must not stand in front of the rest.

    This is the specification lane's production bug in a different costume: a
    chooser that always returns the first candidate, plus a coordinator that
    holds a failed candidate, is a lane that hands back the same broken company
    every tick while the others wait forever. It cost nothing in money and all
    of the progress, and it was caught by reading a heartbeat rather than by a
    test. Hence this one.
    """

    IBM = "company:sec-cik:0000051143"

    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.spec = spec()
        # IBM's specification rests on nothing filed, so its model refuses.
        broken = {**spec(drivers=[{
            "ref": "adoption", "label": "Platform adoption", "kind": "volume",
            "basis_concept": None, "unit": "seats",
            "because": "Adoption is the engine and GAAP does not report it."}]),
            "spec_id": "company-model-spec:ibm"}
        self.missions = TwoCompanyMissions(
            {ACN: ledger().lines, self.IBM: ledger().lines},
            {ACN: self.spec, self.IBM: broken})
        self.launcher = FakeLauncher()
        self.models = ForecastModelAuthority(self.store)
        self.lane = MissionModelForecastLaneCoordinator(
            missions=self.missions, models=self.models, launcher=self.launcher,
            mission=lambda: {"id": "m", "universe": [
                {"company_ref": ACN}, {"company_ref": self.IBM}]})

    def test_a_company_that_cannot_be_modelled_is_skipped_not_repeated(self):
        first = self.lane.dispatch_once()
        self.assertEqual(first["company_ref"], self.IBM)
        self.launcher.finish(first["ticket_ref"], summary={
            "forecast_status": "refused:no revenue driver rests on a filed concept"})
        second = self.lane.dispatch_once()
        self.assertEqual(second["status"], "launched")
        self.assertEqual(second["company_ref"], ACN)
        self.assertIn(self.IBM, second["held"])
        self.launcher.finish(second["ticket_ref"], summary={
            "forecast_status": "published"})
        self.models.publish(build_forecast_model(
            self.spec, build_model_inputs(self.missions, self.spec)))
        # With one held and one done, the lane says so rather than looping.
        third = self.lane.dispatch_once()
        self.assertEqual(third["status"], "held")
        self.assertIn("no revenue driver", third["reason"])
        self.assertEqual([item["company_ref"] for item in self.launcher.started],
                         [self.IBM, ACN])


class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state = Path(self._dir.name)

    def launcher(self, **kwargs):
        made = ModelForecastLauncher(state_dir=self.state, **kwargs)
        self.addCleanup(made.close)
        return made

    def test_the_command_carries_the_company_and_no_model_configuration(self):
        launcher = self.launcher()
        command = launcher._command(ticket_dir=self.state, company_ref=ACN)
        self.assertIn("dalton_core.company_model_forecast_cli", command)
        self.assertIn(ACN, command)
        self.assertNotIn("--model-config", command)
        # Nothing to configure, so nothing to be gated on.
        self.assertTrue(launcher.configured)

    def test_the_same_company_and_inputs_is_the_same_ticket(self):
        launcher = self.launcher()
        first = launcher.start(company_ref=ACN, model_digest=DIGEST)
        launcher.wait(timeout=30)
        again = launcher.start(company_ref=ACN, model_digest=DIGEST)
        launcher.wait(timeout=30)
        self.assertEqual(first["id"], again["id"])
        moved = launcher.start(company_ref=ACN, model_digest="b" * 64)
        launcher.wait(timeout=30)
        self.assertNotEqual(moved["id"], first["id"])

    def test_the_ticket_records_what_the_run_was_about(self):
        launcher = self.launcher()
        ticket = launcher.start(company_ref=ACN, model_digest=DIGEST)
        launcher.wait(timeout=30)
        self.assertEqual(ticket["company_ref"], ACN)
        self.assertEqual(ticket["model_digest"], DIGEST)
        self.assertEqual(launcher.status(ticket["id"])["model_digest"], DIGEST)

    def test_a_request_that_names_nothing_is_refused_before_spawning(self):
        launcher = self.launcher()
        for kwargs in ({"company_ref": "", "model_digest": DIGEST},
                       {"company_ref": ACN, "model_digest": "short"},
                       {"company_ref": ACN, "model_digest": None}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(LaneChildRejected):
                    launcher.start(**kwargs)


class RegistrationTests(unittest.TestCase):
    """The lane says itself once, and the shared machinery derives the rest."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state = Path(self._dir.name)

    def test_the_spec_is_registered_and_runs_after_the_specification_lane(self):
        spec_lane = lane_for_operation("dispatch_company_model_spec")
        self.assertIs(lane_for_operation("dispatch_company_model_forecast"), LANE)
        # The specification decides the drivers this model rests on, so it has
        # to have run first on the tick that produces both.
        self.assertGreater(LANE.order, spec_lane.order)
        self.assertEqual(LANE.driver_key, "company_model_forecast")
        self.assertEqual(LANE.init_kwarg, LAUNCHER_KWARG)
        self.assertEqual(LANE.param_fields, frozenset())
        self.assertTrue(LANE.core_discovery)

    def test_the_lane_is_off_until_there_is_a_core_to_read(self):
        # Every lane's LaunchAgent fragment is gated on the thing it needs.
        # This one needs nothing installed -- no connector, no model
        # configuration -- so what it is gated on is the only thing it does
        # need, and the invariant that a lane is off until its prerequisite
        # exists holds here too.
        self.assertEqual(argv_fragment(LaunchAgentContext(state=self.state)), [])
        (self.state / "core.sqlite").write_bytes(b"")
        self.assertEqual(argv_fragment(LaunchAgentContext(state=self.state)),
                         ["--model-forecast-lane"])

    def test_the_launcher_is_built_from_the_flag_and_nothing_else(self):
        class Args:
            db = str(self.state / "core.sqlite")
            model_forecast_lane = False

        self.assertIsNone(build_launcher(Args()))
        Args.model_forecast_lane = True
        launcher = build_launcher(Args())
        self.addCleanup(launcher.close)
        self.assertIsInstance(launcher, ModelForecastLauncher)
        self.assertEqual(launcher.state_dir, self.state.resolve())


if __name__ == "__main__":
    unittest.main()
