import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.annual_report_runtime import (
    DRAFT_MODEL_CONFIG_NAME,
    VERIFIER_MODEL_CONFIG_NAME,
)
from dalton_core.annual_report_setup import install
from dalton_core.store import canonical_json


class AnnualReportSetupTests(unittest.TestCase):
    def test_common_role_retry_seed_keeps_annual_recovery_and_owner_overrides(self):
        from dalton_core.provider_retry import DEFAULT_RETURNED_PROVIDER_RETRY
        inherited_policies = (
            dict(DEFAULT_RETURNED_PROVIDER_RETRY),
            {"max_same_profile_retries": 5, "retry_backoff_seconds": 17},
            {"max_same_profile_retries": 0, "retry_backoff_seconds": 0,
             "unknown_recovery": {"max_fresh_work_orders": 0,
                                  "retry_backoff_seconds": 5,
                                  "max_elapsed_seconds": 3600}},
        )
        for inherited in inherited_policies:
            with self.subTest(policy=inherited), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                service = state / "service.json"
                service.write_text(json.dumps({"core_db": str(state / "core.sqlite")}))
                common = {
                    "credential_slot_refs": ["credential-slot:model:test"],
                    "model_router_db": str(state / "router.sqlite"),
                    "broker_socket": str(state / "broker.sock"),
                    "broker_auth_key": str(state / "broker.key"),
                    "broker_client_id": "client:dalton-core", "expected_agent_id": "chem",
                    "budget_db": str(state / "budget.sqlite"),
                    "budget_policy_ref": "budget-policy:test:1",
                    "provider_retry": inherited,
                }
                seeds = {}
                for name, route in (
                    ("dossier-model-config.json", "routing-policy:draft:1"),
                    ("company-dossier-verifier-model-config.json", "routing-policy:verifier:1"),
                ):
                    path = state / name
                    path.write_text(canonical_json({**common, "routing_policy_ref": route}) + "\n")
                    path.chmod(0o600)
                    seeds[name] = path.read_bytes()
                install(service)
                for name in (DRAFT_MODEL_CONFIG_NAME, VERIFIER_MODEL_CONFIG_NAME):
                    target = state / name
                    policy = json.loads(target.read_text())["provider_retry"]
                    self.assertEqual({k: policy[k] for k in inherited}, inherited)
                    self.assertEqual(policy["unknown_recovery"]["max_fresh_work_orders"],
                                     0 if "unknown_recovery" in inherited else 2)
                installed = {name: (state / name).read_bytes()
                             for name in (DRAFT_MODEL_CONFIG_NAME, VERIFIER_MODEL_CONFIG_NAME)}
                self.assertEqual(install(service)["created"], [])
                self.assertEqual({name: (state / name).read_bytes() for name in installed}, installed)
                self.assertEqual({name: (state / name).read_bytes() for name in seeds}, seeds)

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
            for target in (draft_target, verifier_target):
                installed = json.loads(target.read_text())
                self.assertEqual(installed["provider_retry"], {
                    "max_same_profile_retries": 1,
                    "retry_backoff_seconds": 2,
                    "unknown_recovery": {
                        "max_fresh_work_orders": 2,
                        "retry_backoff_seconds": 30,
                        "max_elapsed_seconds": 7200,
                    },
                })
                self.assertEqual(installed["transport_retry"], {
                    "max_definitely_not_sent_retries": 1,
                    "queue_wait_seconds": 600,
                    "retry_backoff_seconds": 2,
                })

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

    def test_exclusive_publish_preserves_and_validates_racing_owner_file(self):
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
                "broker_client_id": "client:dalton-core", "expected_agent_id": "chem",
                "budget_db": str(state / "budget.sqlite"),
                "budget_policy_ref": "budget-policy:test:1",
                "call_budget": {"max_input_tokens": 2000, "max_output_tokens": 200,
                                "max_cost_usd": 2.0, "timeout_seconds": 40},
                "run_budget": {"max_units": 2, "max_seconds": 90},
            }
            for name, route in (
                ("dossier-model-config.json", "routing-policy:draft:1"),
                ("company-dossier-verifier-model-config.json", "routing-policy:verifier:1"),
            ):
                path = state / name
                path.write_text(canonical_json({**common, "routing_policy_ref": route}) + "\n")
                os.chmod(path, 0o600)

            target = state / DRAFT_MODEL_CONFIG_NAME
            real_link = os.link
            won = False

            def racing_link(source, destination):
                nonlocal won
                if Path(destination).name == target.name and not won:
                    won = True
                    owner = json.loads(Path(source).read_text())
                    owner["provider_retry"] = {
                        "max_same_profile_retries": 9,
                        "retry_backoff_seconds": 17,
                    }
                    target.write_text(canonical_json(owner) + "\n", encoding="utf-8")
                    os.chmod(target, 0o600)
                    raise FileExistsError(destination)
                return real_link(source, destination)

            with patch("dalton_core.annual_report_setup.os.link", side_effect=racing_link):
                result = install(service)
            self.assertIn(str(target.resolve()), result["preserved"])
            self.assertEqual(
                json.loads(target.read_text())["provider_retry"]["max_same_profile_retries"],
                9,
            )


if __name__ == "__main__":
    unittest.main()
