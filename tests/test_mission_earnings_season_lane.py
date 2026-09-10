"""P14f: the lane, its selection, and what it does when it is not installed.

The lane's whole job is to answer "is there a window open that nobody has
written about", and to answer it from the ledgers rather than from memory. The
tests below are about the two ways that can go wrong: naming work that is
already done, and failing to name work that is not.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
from pathlib import Path

from dalton_core import earnings_season as season
from dalton_core import mission_earnings_season_lane as lane
from dalton_core.budget_pools import pool_for_operation
from dalton_core.lane_registry import LANE_MODULES, load_lanes, registered_lanes
from dalton_core.mission_deliverable import MissionDeliverableAuthority
from dalton_core.research_event import ResearchEventAuthority, record_event
from tests.p14a_fixtures import ACN, AUTOMATION, CTSH, P14aHarness
from tests.test_earnings_season import TODAY, calendar_entry


class FakeLauncher:
    def __init__(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._temp.name)
        self.writer_model_config = self.state_dir / "writer.json"
        self.verifier_model_config = self.state_dir / "verifier.json"
        self.writer_model_config.write_text("{}")
        self.verifier_model_config.write_text("{}")
        self.policy_path = None
        self.started: list[str] = []
        self.tickets: dict[str, dict] = {}

    def start(self, *, batch_ref, company_ref=None, occurrence_ref=None, window=None):
        self.started.append(batch_ref)
        ticket = {"id": f"ticket:{len(self.started)}", "batch_ref": batch_ref,
                  "signature": batch_ref, "company_ref": company_ref,
                  "occurrence_ref": occurrence_ref, "window": window,
                  "status": "running", "summary": {}}
        self.tickets[ticket["id"]] = ticket
        return ticket

    def status(self, ticket_ref):
        return self.tickets[ticket_ref]

    def finish(self, ticket_ref, **summary):
        self.tickets[ticket_ref].update(
            {"status": "succeeded", "summary": summary})


class CoordinatorTests(unittest.TestCase):
    mission = {"id": "coverage-mission-version:us-it-services:1"}

    def coordinator(self, pending):
        self.launcher = FakeLauncher()
        self.addCleanup(self.launcher._temp.cleanup)
        return lane.MissionEarningsSeasonLaneCoordinator(
            launcher=self.launcher, mission=lambda: self.mission,
            pending=lambda _mission: pending,
        )

    def test_nothing_due_launches_nothing_and_says_why(self):
        result = self.coordinator(None).dispatch_once()
        self.assertEqual(result["status"], "idle")
        self.assertIn("unwritten preview or calibration", result["reason"])
        self.assertEqual(self.launcher.started, [])

    def test_a_due_window_launches_one_child(self):
        result = self.coordinator("earnings-occurrence:abc:preview").dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(len(self.launcher.started), 1)

    def test_the_same_window_is_not_dispatched_twice(self):
        coordinator = self.coordinator("earnings-occurrence:abc:preview")
        first = coordinator.dispatch_once()
        self.launcher.finish(first["ticket_ref"], season_status="written", previews=1)
        second = coordinator.dispatch_once()
        self.assertEqual(second["status"], "idle")
        self.assertEqual(second["settled"]["previews"], 1)
        self.assertEqual(len(self.launcher.started), 1)

    def test_contract_change_reopens_the_same_window(self):
        coordinator = self.coordinator("earnings-occurrence:abc:preview")
        with patch.object(lane, "verifier_provider_contract_fingerprint", return_value="a" * 64):
            first = coordinator.dispatch_once()
            self.launcher.finish(first["ticket_ref"], season_status="refused")
            self.assertEqual(coordinator.dispatch_once()["status"], "idle")
        with patch.object(lane, "verifier_provider_contract_fingerprint", return_value="b" * 64):
            self.assertEqual(coordinator.dispatch_once()["status"], "launched")

    def test_no_mission_is_unconfigured_rather_than_a_crash(self):
        self.mission = None
        result = self.coordinator("x").dispatch_once()
        self.assertEqual(result["status"], "unconfigured")

    def test_a_failing_selector_does_not_take_the_tick_with_it(self):
        launcher = FakeLauncher()
        self.addCleanup(launcher._temp.cleanup)

        def explode(_mission):
            raise sqlite_error()

        coordinator = lane.MissionEarningsSeasonLaneCoordinator(
            launcher=launcher, mission=lambda: {"id": "m"}, pending=explode)
        result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("RuntimeError", result["reason"])

    def test_refused_first_window_does_not_starve_another_company(self):
        rows = [
            {"company_ref": "company:A", "occurrence_ref": "occurrence:A",
             "window": "preview", "input_hash": "a" * 64},
            {"company_ref": "company:B", "occurrence_ref": "occurrence:B",
             "window": "preview", "input_hash": "b" * 64},
        ]
        coordinator = self.coordinator(rows)
        first = coordinator.dispatch_once()
        self.launcher.finish(first["ticket_ref"], season_status="refused", windows=[{
            "company_ref": "company:A", "occurrence_ref": "occurrence:A",
            "window": "preview", "status": "refused", "reason": "refused: unsupported",
        }])
        second = coordinator.dispatch_once()
        self.assertEqual(second["company_ref"], "company:B")
        self.assertIn("occurrence:A:preview", second["held"])

    def test_persisted_window_hold_survives_restart(self):
        row = {"company_ref": "company:A", "occurrence_ref": "occurrence:A",
               "window": "preview", "input_hash": "a" * 64}
        coordinator = self.coordinator([row])
        first = coordinator.dispatch_once()
        self.launcher.finish(first["ticket_ref"], season_status="refused", windows=[{
            **row, "status": "refused", "reason": "refused: unsupported",
        }])
        self.assertEqual(coordinator.dispatch_once()["status"], "held")
        restarted = lane.MissionEarningsSeasonLaneCoordinator(
            launcher=self.launcher, mission=lambda: self.mission,
            pending=lambda _mission: [row],
        )
        self.assertEqual(restarted.dispatch_once()["status"], "held")
        self.assertEqual(len(self.launcher.started), 1)

    def test_input_change_releases_only_the_affected_window(self):
        rows = [{"company_ref": "company:A", "occurrence_ref": "occurrence:A",
                 "window": "preview", "input_hash": "a" * 64}]
        coordinator = self.coordinator(rows)
        first = coordinator.dispatch_once()
        self.launcher.finish(first["ticket_ref"], season_status="refused", windows=[{
            **rows[0], "status": "refused", "reason": "refused: unsupported",
        }])
        self.assertEqual(coordinator.dispatch_once()["status"], "held")
        rows[0] = {**rows[0], "input_hash": "c" * 64}
        self.assertEqual(coordinator.dispatch_once()["status"], "launched")

    def test_more_than_eight_held_windows_do_not_hide_the_ninth(self):
        rows = [{"company_ref": f"company:{number}",
                 "occurrence_ref": f"occurrence:{number}", "window": "preview",
                 "input_hash": f"{number:064x}"} for number in range(9)]
        coordinator = self.coordinator(rows)
        for number in range(8):
            launched = coordinator.dispatch_once()
            self.assertEqual(launched["company_ref"], f"company:{number}")
            self.launcher.finish(launched["ticket_ref"], season_status="refused", windows=[{
                **rows[number], "status": "refused", "reason": "refused: unsupported",
            }])
        ninth = coordinator.dispatch_once()
        self.assertEqual(ninth["company_ref"], "company:8")

    def test_model_configuration_change_releases_the_exact_window(self):
        row = {"company_ref": "company:A", "occurrence_ref": "occurrence:A",
               "window": "preview", "input_hash": "a" * 64}
        coordinator = self.coordinator([row])
        first = coordinator.dispatch_once()
        self.launcher.finish(first["ticket_ref"], season_status="refused", windows=[{
            **row, "status": "refused", "reason": "refused: unsupported",
        }])
        self.assertEqual(coordinator.dispatch_once()["status"], "held")
        self.launcher.writer_model_config.write_text('{"routing_policy_ref":"new"}')
        self.assertEqual(coordinator.dispatch_once()["status"], "launched")

    def test_missing_ticket_is_bounded_without_a_second_status_read(self):
        from dalton_core.lane_child_launcher import LaneChildTicketNotFound

        row = {"company_ref": "company:A", "occurrence_ref": "occurrence:A",
               "window": "preview", "input_hash": "a" * 64}
        coordinator = self.coordinator([row])
        coordinator.dispatch_once()
        calls = 0

        def missing(_ticket_ref):
            nonlocal calls
            calls += 1
            raise LaneChildTicketNotFound("gone")

        self.launcher.status = missing
        result = coordinator.dispatch_once()
        self.assertEqual(calls, 1)
        self.assertIn(result["status"], ("launched", "held"))


def sqlite_error() -> RuntimeError:
    return RuntimeError("the calendar table is not on this Core")


class SelectionTests(P14aHarness):
    grants = ("market_event", "observation", "stage_record", "deliverable")

    def setUp(self) -> None:
        super().setUp()
        self.events = ResearchEventAuthority(self.store)
        self.deliverables = MissionDeliverableAuthority(self.store)

    def record(self, *, company_ref=ACN, expected="2026-10-01", confirmed=False,
               window="preview", anchor=None):
        """A calendar event on the ledger, built the way C1 builds one."""

        from dalton_core.catalyst_calendar import calendar_event_payload

        entry = calendar_entry(
            company_ref=company_ref, expected=expected, anchor=anchor or expected,
            confidence="confirmed" if confirmed else "estimated")
        return record_event(
            self.events, company_ref=company_ref, kind="calendar",
            occurred_at=f"{TODAY}T00:00:00+00:00",
            source_refs=["catalyst-calendar-version:1"],
            payload=calendar_event_payload(
                entry, window=window, version_ref="catalyst-calendar-version:1"),
            mission=self.mission, actor_ref=AUTOMATION,
        )

    def test_only_a_screened_company_is_looked_at(self):
        self.record(company_ref=CTSH)
        self.assertEqual(
            lane.due_occurrences(self.store, self.missions, self.mission, now=TODAY),
            [])
        self.pass_screen(CTSH)
        due = lane.due_occurrences(
            self.store, self.missions, self.mission, now=TODAY)
        self.assertEqual([row["company_ref"] for row in due], [CTSH])

    def test_a_window_already_written_about_is_not_named_again(self):
        self.pass_screen(ACN)
        self.record()
        due = lane.due_occurrences(self.store, self.missions, self.mission, now=TODAY)
        self.assertEqual(len(due), 1)
        self.deliverables.publish(
            kind="earnings_preview", subject_ref=ACN, mission=self.mission,
            playbook=self.playbook, template_ref="template:earnings-preview:p14f:v1",
            sections=[{"title": "t", "body": "已经写过了。", "claim_refs": [],
                       "numbers": [], "gaps": []}],
            summary="fixture", actor_ref=AUTOMATION,
            idempotency_key=season.idempotency_key_for("preview", due[0]),
        )
        self.assertEqual(
            lane.due_occurrences(self.store, self.missions, self.mission, now=TODAY),
            [])

    def test_a_calibration_sorts_ahead_of_a_preview(self):
        self.pass_screen(ACN)
        self.pass_screen(CTSH)
        self.record()
        self.record(company_ref=CTSH, expected="2026-09-08", confirmed=True,
                    window="calibration")
        named = lane.newest_due(self.store, self.missions, self.mission)
        self.assertTrue(named.endswith(":calibration"))

    def test_nothing_due_is_nothing_named(self):
        self.pass_screen(ACN)
        self.assertIsNone(lane.newest_due(self.store, self.missions, self.mission))


class RegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        load_lanes()

    def test_the_lane_is_registered_at_its_own_order(self):
        spec = next(item for item in registered_lanes()
                    if item.operation == "dispatch_earnings_season")
        self.assertEqual(spec.order, 117)
        self.assertEqual(spec.driver_key, "earnings_season")
        self.assertEqual(spec.init_kwarg, lane.LAUNCHER_KWARG)

    def test_the_module_is_in_the_registry_list(self):
        self.assertIn("dalton_core.mission_earnings_season_lane", LANE_MODULES)

    def test_launcher_targets_one_exact_occurrence_window(self):
        from dalton_core.earnings_season_launcher import EarningsSeasonLauncher

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            launcher = EarningsSeasonLauncher(
                state_dir=root, writer_model_config=root / "writer.json",
                verifier_model_config=root / "verifier.json",
            )
            command = launcher._command(
                ticket_dir=root / "ticket", company_ref="company:A",
                occurrence_ref="occurrence:A", window="calibration",
            )
        self.assertEqual(command[command.index("--company-ref") + 1], "company:A")
        self.assertEqual(command[command.index("--occurrence-ref") + 1], "occurrence:A")
        self.assertEqual(command[command.index("--window") + 1], "calibration")

    def test_it_spends_from_the_event_response_pool(self):
        self.assertEqual(
            pool_for_operation("dispatch_earnings_season"), "event_response")

    def test_the_cockpit_has_a_word_for_it(self):
        from dalton_core.cockpit_plane import REGISTRY_LANE_LABELS

        self.assertIn("earnings_season", REGISTRY_LANE_LABELS)

    def test_a_writer_without_the_lane_says_so_rather_than_failing(self):
        class Server:
            lane_state: dict = {}

            def lane_launcher(self, _name):
                return None

        result = lane.dispatch(Server(), {})
        self.assertEqual(result["status"], "unconfigured")
        self.assertIn("earnings-season", result["reason"])

    def test_no_configuration_no_launcher_and_no_argv(self):
        class Args:
            db = "/tmp/core.sqlite"
            earnings_season_model_config = None
            earnings_season_verifier_model_config = None

        self.assertIsNone(lane.build_launcher(Args()))

    def test_a_writer_with_only_one_configuration_installs_neither(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / lane.WRITER_MODEL_CONFIG).write_text("{}", encoding="utf-8")

            class Context:
                pass

            context = Context()
            context.state = state
            self.assertEqual(lane.argv_fragment(context), [])
            (state / lane.VERIFIER_MODEL_CONFIG).write_text("{}", encoding="utf-8")
            self.assertEqual(len(lane.argv_fragment(context)), 4)

    def test_importing_this_module_does_not_pull_in_the_writer(self):
        # The registry's own rule, in a fresh interpreter because that is the
        # only place an import cycle shows up: a lane module that imported
        # writer_server would have writer_server fold in a half-built registry,
        # and the lane would be dispatched by the tick and refused by the
        # writer for the life of the process.
        import dalton_core

        root = Path(dalton_core.__file__).resolve().parents[1]
        script = textwrap.dedent("""
            import sys
            import dalton_core.mission_earnings_season_lane as lane  # noqa: F401
            from dalton_core.lane_registry import registered_lanes
            assert any(spec.operation == "dispatch_earnings_season"
                       for spec in registered_lanes()), "the lane did not register"
            print(",".join(sorted(
                module for module in sys.modules
                if module in ("dalton_core.writer_server",
                              "dalton_core.bounded_planner_driver",
                              "dalton_core.macos_launchagent")
            )))
        """)
        finished = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            env={"PYTHONPATH": str(root), "PATH": "/usr/bin:/bin"}, timeout=120,
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertEqual(finished.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
