import json
import os
import tempfile
import unittest
from pathlib import Path

from dalton_core.annual_report_runtime import (
    DRAFT_MODEL_CONFIG_NAME,
    VERIFIER_MODEL_CONFIG_NAME,
)
from dalton_core.annual_report_setup import install
from dalton_core.store import canonical_json


class AnnualReportSetupTests(unittest.TestCase):
    def test_first_install_inherits_current_roles_and_reinstall_preserves_owner_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            service = root / "service.json"
            service.write_text(json.dumps({"core_db": str(state / "core.sqlite")}), encoding="utf-8")
            common = {
                "credential_slot_refs": ["credential-slot:model:test"],
                "model_router_db": str(state / "router.sqlite"),
                "broker_socket": str(state / "broker.sock"),
                "broker_auth_key": str(state / "broker.key"),
                "broker_client_id": "client:dalton-core",
                "expected_agent_id": "chem",
                "budget_db": str(state / "budget.sqlite"),
                "budget_policy_ref": "budget-policy:test:1",
                "call_budget": {
                    "max_input_tokens": 2000, "max_output_tokens": 200,
                    "max_cost_usd": 2.0, "timeout_seconds": 40,
                },
                "run_budget": {"max_units": 2, "max_seconds": 90},
            }
            draft_source = state / "dossier-model-config.json"
            verifier_source = state / "company-dossier-verifier-model-config.json"
            for path, route in (
                (draft_source, "routing-policy:draft:1"),
                (verifier_source, "routing-policy:verifier:1"),
            ):
                path.write_text(canonical_json({**common, "routing_policy_ref": route}) + "\n")
                os.chmod(path, 0o600)

            first = install(service)
            self.assertEqual(first["status"], "installed")
            self.assertEqual(len(first["created"]), 2)
            draft_target = state / DRAFT_MODEL_CONFIG_NAME
            verifier_target = state / VERIFIER_MODEL_CONFIG_NAME
            self.assertEqual(
                json.loads(draft_target.read_text())["routing_policy_ref"],
                "routing-policy:draft:1",
            )
            self.assertEqual(
                json.loads(verifier_target.read_text())["routing_policy_ref"],
                "routing-policy:verifier:1",
            )

            owner_config = json.loads(draft_target.read_text())
            owner_config["provider_retry"] = {
                "max_same_profile_retries": 7,
                "retry_backoff_seconds": 3601,
            }
            draft_target.write_text(canonical_json(owner_config) + "\n", encoding="utf-8")
            os.chmod(draft_target, 0o600)
            before = draft_target.read_bytes()
            second = install(service)
            self.assertEqual(second["status"], "preserved")
            self.assertEqual(draft_target.read_bytes(), before)
            self.assertEqual(len(second["preserved"]), 2)


if __name__ == "__main__":
    unittest.main()
