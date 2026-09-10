import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from dalton_core.investment_memo_launcher import InvestmentMemoLauncher
from dalton_core.mission_investment_memo_lane import (
    MissionInvestmentMemoLaneCoordinator,
    argv_fragment,
    company_signature,
)


class InvestmentMemoLaneTests(unittest.TestCase):
    def test_launcher_carries_the_independent_pair(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            launcher = InvestmentMemoLauncher(state_dir=root, model_config_path=root / "producer.json",
                                               verifier_model_config_path=root / "verifier.json",
                                               scheduler_db=root / "scheduler.sqlite")
            command = launcher._command(ticket_dir=root / "ticket", company_ref="company:ACN")
            self.assertEqual(command[command.index("--model-config") + 1], str((root / "producer.json").resolve()))
            self.assertEqual(command[command.index("--verifier-model-config") + 1], str((root / "verifier.json").resolve()))
            self.assertIn("company:ACN", command)

    def test_registry_uses_dossier_pair_and_never_half_enables(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            context = SimpleNamespace(state=state)
            self.assertEqual(argv_fragment(context), [])
            (state / "dossier-model-config.json").write_text("{}")
            self.assertEqual(argv_fragment(context), [])
            (state / "company-dossier-verifier-model-config.json").write_text("{}")
            argv = argv_fragment(context)
            self.assertEqual(argv, ["--investment-memo-model-config", str(state / "dossier-model-config.json"),
                                    "--investment-memo-verifier-model-config", str(state / "company-dossier-verifier-model-config.json")])


class MemoFairnessTests(unittest.TestCase):
    class Launcher:
        def __init__(self, root, *, failed_company=None):
            self.state_dir = root
            self.model_config_path = root / "producer.json"
            self.verifier_model_config_path = root / "verifier.json"
            self.model_config_path.write_text("{}")
            self.verifier_model_config_path.write_text("{}")
            self.failed_company = failed_company
            self.failure_summary = None
            self.started = []

        def start(self, *, signature, company_ref=None, recovery_ref=None):
            cached = next((item for item in self.started
                           if item["signature"] == signature
                           and item["company_ref"] == company_ref
                           and item["recovery_ref"] == recovery_ref), None)
            if cached is not None:
                return cached
            ticket = {"id": "memo-run:" + str(len(self.started)),
                      "signature": signature, "company_ref": company_ref,
                      "recovery_ref": recovery_ref}
            self.started.append(ticket)
            return ticket

        def status(self, ticket_ref):
            ticket = next(item for item in self.started if item["id"] == ticket_ref)
            if ticket["company_ref"] == self.failed_company:
                return {**ticket, "status": "failed", "summary": (
                    self.failure_summary or {"memo_status": "draft_refused",
                                             "reason": "unsupported claim"})}
            return {**ticket, "status": "succeeded", "summary": {
                "memo_status": "nothing_new"}}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.launcher = self.Launcher(self.root, failed_company="company:A")
        self.inputs = {
            "company:A": self.frozen("company:A", "a" * 64),
            "company:B": self.frozen("company:B", "b" * 64),
        }

    @staticmethod
    def frozen(company_ref, input_hash):
        return {"status": "ready", "company": {"company_ref": company_ref},
                "mission": {"id": "mission-version:1", "content_hash": "1" * 64},
                "playbook": {"id": "playbook-version:1", "content_hash": "2" * 64},
                "input_bindings": [{"kind": "forecast_model_versions",
                                    "ref": "model:" + company_ref,
                                    "hash": input_hash}]}

    def coordinator(self):
        return MissionInvestmentMemoLaneCoordinator(
            connection=None, launcher=self.launcher,
            companies=lambda: ["company:A", "company:B"],
            frozen_input=lambda company_ref: self.inputs[company_ref],
        )

    def test_content_refused_first_company_does_not_starve_second(self):
        coordinator = self.coordinator()
        self.assertEqual(coordinator.dispatch_once()["company_ref"], "company:A")
        second = coordinator.dispatch_once()
        self.assertEqual(second["company_ref"], "company:B")
        self.assertIn("company:A", second["held"])

    def test_persisted_content_hold_survives_restart_without_repeated_work(self):
        coordinator = self.coordinator()
        coordinator.dispatch_once()
        coordinator.dispatch_once()
        restarted = self.coordinator()
        result = restarted.dispatch_once()
        self.assertEqual(result["company_ref"], "company:B")
        self.assertEqual([item["company_ref"] for item in self.launcher.started],
                         ["company:A", "company:B"])

    def test_input_change_moves_only_its_company_signature(self):
        a_before = company_signature(self.inputs["company:A"], self.launcher)
        b_before = company_signature(self.inputs["company:B"], self.launcher)
        self.inputs["company:A"] = self.frozen("company:A", "c" * 64)
        self.assertNotEqual(company_signature(self.inputs["company:A"], self.launcher),
                            a_before)
        self.assertEqual(company_signature(self.inputs["company:B"], self.launcher),
                         b_before)

    def test_verifier_provider_contract_change_releases_the_company_identity(self):
        with patch(
            "dalton_core.cockpit_model.verifier_provider_contract_fingerprint",
            return_value="a" * 64,
        ):
            before = company_signature(self.inputs["company:A"], self.launcher)
        with patch(
            "dalton_core.cockpit_model.verifier_provider_contract_fingerprint",
            return_value="b" * 64,
        ):
            after = company_signature(self.inputs["company:A"], self.launcher)
        self.assertNotEqual(before, after)

    def test_all_held_companies_are_quiet(self):
        self.launcher.failed_company = "company:A"
        coordinator = self.coordinator()
        coordinator.dispatch_once()
        coordinator.dispatch_once()
        self.launcher.failed_company = "company:B"
        coordinator.dispatch_once()
        held = coordinator.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertEqual(set(held["held"]), {"company:A", "company:B"})

    def test_missing_prerequisites_are_skipped_without_a_ticket(self):
        self.inputs["company:A"] = {"status": "held", "reason": "missing forecast"}
        result = self.coordinator().dispatch_once()
        self.assertEqual(result["company_ref"], "company:B")
        self.assertEqual([item["company_ref"] for item in self.launcher.started],
                         ["company:B"])

    def test_missing_frozen_input_is_skipped_without_crashing_or_a_ticket(self):
        coordinator = MissionInvestmentMemoLaneCoordinator(
            connection=None, launcher=self.launcher,
            companies=lambda: ["company:A"], frozen_input=lambda _company: None,
        )
        result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["skipped"], {"company:A": "frozen input unavailable"})
        self.assertEqual(self.launcher.started, [])

    def test_broker_capacity_failure_is_recoverable_not_content_terminal(self):
        self.launcher.failure_summary = {
            "memo_status": "verification_failed",
            "verification": {"status": "refused", "lane_status": "held",
                             "reason": "capacity_busy; recovery cooldown has not elapsed"},
        }
        coordinator = MissionInvestmentMemoLaneCoordinator(
            connection=None, launcher=self.launcher,
            companies=lambda: ["company:A"],
            frozen_input=lambda company_ref: self.inputs[company_ref],
        )
        coordinator.dispatch_once()
        retried = coordinator.dispatch_once()
        self.assertEqual(retried["status"], "launched")
        self.assertEqual(len(self.launcher.started), 2)
