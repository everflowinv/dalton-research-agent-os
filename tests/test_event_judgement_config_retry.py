"""A Cockpit route change retries a refused batch without overlapping children."""
import tempfile
import unittest
from pathlib import Path

from dalton_core.event_judgement_launcher import EventJudgementLauncher
from dalton_core.mission_event_judgement_lane import MissionEventJudgementLaneCoordinator
from tests.test_mission_event_judgement_lane import FakeLauncher


class ConfigurationRetryTests(unittest.TestCase):
    def test_changed_verifier_retries_same_event_after_old_child_settles(self):
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            judge, verifier = state / "judge.json", state / "verifier.json"
            judge.write_text('{"routing_policy_ref":"judge:1"}')
            verifier.write_text('{"routing_policy_ref":"verifier:1"}')
            real = EventJudgementLauncher(
                state_dir=state, judge_model_config=judge, verifier_model_config=verifier,
            )
            launcher = FakeLauncher()
            launcher.configuration_signature = real.configuration_signature
            coordinator = MissionEventJudgementLaneCoordinator(
                launcher=launcher, mission=lambda: {"id": "mission:14"},
                pending=lambda mission: "event:unchanged",
            )
            first = coordinator.dispatch_once()
            verifier.write_text('{"routing_policy_ref":"verifier:2"}')
            self.assertEqual(coordinator.dispatch_once()["status"], "busy")
            self.assertEqual(len(launcher.started), 1)
            launcher.settle(first["ticket_ref"], {"judged": 0, "refused": 1})
            second = coordinator.dispatch_once()
            self.assertEqual(second["status"], "launched")
            self.assertEqual(second["settled"]["refused"], 1)
            self.assertNotEqual(first["batch_ref"], second["batch_ref"])
            launcher.settle(second["ticket_ref"], {"judged": 0, "refused": 1})
            self.assertEqual(coordinator.dispatch_once()["status"], "idle")
            self.assertEqual(len(launcher.started), 2)

    def test_unreadable_configuration_preserves_the_tick_and_previous_ticket(self):
        launcher = FakeLauncher()
        def unreadable():
            raise OSError("configuration unavailable")
        launcher.configuration_signature = unreadable
        coordinator = MissionEventJudgementLaneCoordinator(
            launcher=launcher, mission=lambda: {"id": "mission:14"},
            pending=lambda mission: "event:1",
        )
        self.assertEqual(coordinator.dispatch_once()["status"], "unavailable")
        self.assertEqual(launcher.started, [])
