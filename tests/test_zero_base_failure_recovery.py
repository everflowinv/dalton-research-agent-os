"""Bound the real review child and retain its per-company refusals."""
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.mission_zero_base_lane import MissionZeroBaseLaneCoordinator
from dalton_core.store import DaltonStore
from dalton_core.zero_base_review_cli import run_zero_base, main
from dalton_core.zero_base_review_launcher import ZeroBaseReviewLauncher
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_mission_zero_base_lane import FakeLauncher

NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


class InvalidContentModel:
    def __init__(self):
        self.calls = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        return {"text": "{}", "model": "test", "provider": "test"}


class ChildRecoveryTests(unittest.TestCase):
    def test_selected_company_real_refusal_is_terminal_until_inputs_move(self):
        with TemporaryDirectory() as directory:
            state = Path(directory)
            store = DaltonStore(str(state / "core.sqlite"))
            authority = CoverageMissionAuthority(store)
            params = mission_params(bootstrap_method_authorities(store))
            params["autonomy"]["may_write"].append("deliverable")
            mission = authority.create_mission(params.pop("mission_ref"), **params)
            companies = [member["company_ref"] for member in mission["universe"]]
            store.close()
            producer = InvalidContentModel()
            with patch("dalton_core.zero_base_review_cli.screen_passed_companies",
                       return_value=companies):
                summary = run_zero_base(
                    state_dir=state, summary_dir=state / "ticket",
                    company_refs=[companies[1]], model=producer,
                    verifier_model=InvalidContentModel(), family_resolver=lambda _: "test",
                    now=NOW,
                )
            self.assertEqual(summary["status"], "succeeded", summary)
            self.assertEqual(summary["refused"], 1)
            self.assertEqual(len(producer.calls), 1)
            outcome = summary["reviews"][0]
            self.assertEqual(outcome["company_ref"], companies[1])
            self.assertEqual(len(outcome["inputs_hash"]), 64)
            due = [{key: outcome[key] for key in (
                "company_ref", "trigger", "period_label", "inputs_hash")}]
            launcher = FakeLauncher()

            def build():
                return MissionZeroBaseLaneCoordinator(
                    launcher=launcher, mission=lambda: mission,
                    lane_state=lambda _m, _n: {"due": due, "checks_digest": ""},
                    clock=lambda: NOW, failure_ledger_dir=state,
                )

            lane = build()
            started = lane.dispatch_once()
            ticket = launcher.tickets[started["ticket_ref"]]
            ticket.update(status="succeeded", summary=summary)
            self.assertEqual(lane.dispatch_once()["status"], "terminal")
            self.assertEqual(build().dispatch_once()["status"], "terminal")
            self.assertEqual(len(launcher.started), 1)
            due[0]["inputs_hash"] = "b" * 64
            restarted = build()
            self.assertEqual(restarted.dispatch_once()["status"], "launched")
            self.assertEqual(restarted.budget.terminal_items(), [])

    def test_launcher_and_cli_preserve_the_selected_company_subset(self):
        with TemporaryDirectory() as directory:
            launcher = ZeroBaseReviewLauncher(state_dir=directory)
            command = launcher._command(ticket_dir=Path(directory),
                                        company_refs=["company:one", "company:two"])
            selected = [command[i + 1] for i, value in enumerate(command)
                        if value == "--company-ref"]
            self.assertEqual(selected, ["company:one", "company:two"])
            with patch("dalton_core.zero_base_review_cli.run_zero_base",
                       return_value={"status": "succeeded"}) as run:
                self.assertEqual(main(command[3:]), 0)
            self.assertEqual(run.call_args.kwargs["company_refs"], selected)

    def test_launcher_persists_the_provider_contract_in_the_ticket_record(self):
        with TemporaryDirectory() as directory:
            launcher = ZeroBaseReviewLauncher(state_dir=directory)
            class Process:
                pid = 424242
                def poll(self): return None
            with patch("dalton_core.lane_child_launcher.subprocess.Popen", return_value=Process()):
                ticket = launcher.start(mode="checks", batch_ref="checks:1")
            path = launcher._ticket_path(ticket["id"])
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(record["verifier_provider_contract"]), 64)
            self.assertEqual(record["verifier_provider_contract"],
                             ticket["verifier_provider_contract"])
            self.assertEqual(len(record["configuration_signature"]), 64)

    def test_launcher_configuration_signature_changes_with_model_selection(self):
        with TemporaryDirectory() as directory:
            state = Path(directory)
            producer = state / "producer.json"
            verifier = state / "verifier.json"
            policy = state / "tracking.json"
            producer.write_text('{"routing_policy_ref":"policy:a"}', encoding="utf-8")
            verifier.write_text('{"routing_policy_ref":"policy:v"}', encoding="utf-8")
            policy.write_text('{"schema_version":"tracking-policy-v1"}', encoding="utf-8")
            launcher = ZeroBaseReviewLauncher(
                state_dir=state, model_config=producer,
                verifier_model_config=verifier, policy_path=policy,
            )
            before = launcher.configuration_signature()
            producer.write_text('{"routing_policy_ref":"policy:b"}', encoding="utf-8")
            after = launcher.configuration_signature()
            self.assertNotEqual(before, after)

    def test_unchanged_top_grant_does_not_append_every_tick(self):
        with TemporaryDirectory() as directory:
            mission = {"id": "m", "autonomy": {"may_write": []}}
            lane = MissionZeroBaseLaneCoordinator(
                launcher=FakeLauncher(), mission=lambda: mission,
                lane_state=lambda *_: {}, failure_ledger_dir=directory,
            )
            for _ in range(10):
                self.assertEqual(lane.dispatch_once()["status"], "ungranted")
            self.assertEqual(len(lane.budget.permission_items()), 1)
            self.assertEqual(len(lane.budget.ledger.events()), 1)
