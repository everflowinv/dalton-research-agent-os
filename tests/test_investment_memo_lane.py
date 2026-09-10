import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from dalton_core.investment_memo_launcher import InvestmentMemoLauncher
from dalton_core.mission_investment_memo_lane import argv_fragment


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
