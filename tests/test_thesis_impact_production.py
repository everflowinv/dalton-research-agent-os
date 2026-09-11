from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from dalton_core.bootstrap import bootstrap
from dalton_core.model_router import ModelRouter
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from dalton_core.thesis_impact_production import (
    ThesisImpactProductionConfig,
    ThesisImpactProductionError,
    ThesisImpactProductionRunner,
    thesis_impact_runtime_config,
)
from dalton_core.writer_server import (
    THESIS_IMPACT_OPERATIONS,
    Principal,
    WriterServer,
    load_principals,
    write_token_config,
)
from tests.test_model_router import policy as model_policy
from tests.test_model_router import profile as model_profile


class ThesisImpactProductionTests(unittest.TestCase):
    @staticmethod
    def _config(root: Path) -> dict:
        return {
            "scheduler_db": str(root / "scheduler.sqlite"),
            "model_router_db": str(root / "router.sqlite"),
            "writer_socket": str(root / "writer.sock"),
            "token_config": str(root / "tokens.json"),
            "broker_socket": str(root / "broker.sock"),
            "broker_auth_key": str(root / "broker.key"),
            "budget_db": str(root / "budget.sqlite"),
            "routing_policy_ref": "policy:shared",
            "assessment_routing_policy_ref": "policy:assessment",
            "verifier_routing_policy_ref": "policy:verifier",
            "budget_policy_version_id": "budget-policy:production:1",
            "day_cap_micros": 25_000_000,
            "credential_slot_refs": ["credential-slot:openai"],
            "broker_client_id": "client:dalton-core",
            "expected_agent_id": "chem",
            "company_thesis_refs": {},
            "max_targets": 25,
            "timeout_seconds": 180,
        }

    def test_idle_pass_uses_writer_boundary_and_makes_no_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_config = root / "writer-tokens.json"
            principal = Principal(
                "thesis-impact",
                "thesis-impact-test-token",
                THESIS_IMPACT_OPERATIONS,
                actor_ref="system:thesis-impact-model-worker",
            )
            write_token_config(token_config, [principal])
            server = WriterServer(
                root / "core.sqlite",
                root / "writer.sock",
                {principal.principal_id: principal},
                token_config_path=token_config,
                scheduler_path=root / "scheduler.sqlite",
            )
            server.start()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                config = ThesisImpactProductionConfig.from_mapping({
                    "scheduler_db": str(root / "scheduler.sqlite"),
                    "model_router_db": str(root / "router.sqlite"),
                    "writer_socket": str(root / "writer.sock"),
                    "token_config": str(token_config),
                    "broker_socket": str(root / "missing-broker.sock"),
                    "broker_auth_key": str(root / "missing-broker.key"),
                    "budget_db": str(root / "budget.sqlite"),
                    "routing_policy_ref": "policy:shared",
                    "assessment_routing_policy_ref": "policy:assessment",
                    "verifier_routing_policy_ref": "policy:verifier",
                    "budget_policy_version_id": "budget-policy:production:1",
                    "day_cap_micros": 25_000_000,
                    "credential_slot_refs": [
                        "credential-slot:openai",
                        "credential-slot:google",
                    ],
                    "broker_client_id": "client:dalton-core",
                    "expected_agent_id": "chem",
                    "company_thesis_refs": {},
                    "max_targets": 25,
                    "timeout_seconds": 180,
                })
                result = ThesisImpactProductionRunner(config).run_once()
                self.assertEqual(result, {
                    "status": "idle",
                    "target_count": 0,
                    "provider_call_count": 0,
                })
                with ThesisImpactBudgetStore(root / "budget.sqlite") as budget:
                    policy = budget.policy("budget-policy:production:1")
                self.assertEqual(policy["day_cap_micros"], 25_000_000)
            finally:
                server.stop()
                thread.join(timeout=3)

    def test_config_is_closed(self) -> None:
        with self.assertRaises(ThesisImpactProductionError):
            ThesisImpactProductionConfig.from_mapping({"scheduler_db": "/tmp/x"})

    def test_phase_retry_config_is_closed_and_unknown_recovery_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw = self._config(Path(directory)) | {
                "assessment_provider_retry": {
                    "max_same_profile_retries": 1,
                    "retry_backoff_seconds": 2,
                    "unknown_recovery": {
                        "max_fresh_work_orders": 1,
                        "retry_backoff_seconds": 2,
                        "max_elapsed_seconds": 600,
                    },
                },
            }
            with self.assertRaisesRegex(
                ThesisImpactProductionError, "does not support unknown-result recovery"
            ):
                ThesisImpactProductionConfig.from_mapping(raw)

    def test_writer_runtime_reads_only_the_enabled_canonical_service_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state" / "dalton-core"
            config_path = root / "config" / "service.json"
            config_path.parent.mkdir(parents=True)
            state.mkdir(parents=True)
            config_path.write_text(json.dumps({
                "thesis_impact": {
                    "enabled": False,
                    "interval_seconds": 60,
                    "config": self._config(state),
                },
            }))
            self.assertIsNone(thesis_impact_runtime_config(state))
            wire = json.loads(config_path.read_text())
            wire["thesis_impact"]["enabled"] = True
            config_path.write_text(json.dumps(wire))
            loaded = thesis_impact_runtime_config(state)
            self.assertEqual(loaded.assessment_routing_policy_ref, "policy:assessment")
            self.assertEqual(loaded.verifier_routing_policy_ref, "policy:verifier")

    def test_writer_binds_enabled_phase_execution_into_new_work_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state" / "dalton-core"
            state.mkdir(parents=True)
            config_path = root / "config" / "service.json"
            config_path.parent.mkdir(parents=True)
            router_path = state / "router.sqlite"
            assessment_ref = "model-routing-policy-version:impact-assessment:1"
            verifier_ref = "model-routing-policy-version:impact-verifier:1"
            shared_ref = "model-routing-policy-version:impact-shared:1"
            with ModelRouter(router_path) as router:
                assessment = model_profile(
                    "impact-assessment", slot="credential-slot:openai",
                    valid_until="2099-01-01T00:00:00+00:00",
                )
                verifier = model_profile(
                    "impact-verifier", provider="anthropic",
                    family="family-beta", slot="credential-slot:anthropic",
                    valid_until="2099-01-01T00:00:00+00:00",
                )
                router.register_profile(assessment)
                router.register_profile(verifier)
                for ref, identifier, allowed in (
                    (shared_ref, "model-routing-policy:impact-shared",
                     [assessment["id"], verifier["id"]]),
                    (assessment_ref, "model-routing-policy:impact-assessment",
                     [assessment["id"]]),
                    (verifier_ref, "model-routing-policy:impact-verifier",
                     [verifier["id"]]),
                ):
                    wire = model_policy(independence=["verify"])
                    wire.update({"policy_version_ref": ref, "id": identifier})
                    wire["filters"]["allowed_profile_ids"] = allowed
                    router.register_policy(wire)
            config = self._config(state) | {
                "model_router_db": str(router_path),
                "routing_policy_ref": shared_ref,
                "assessment_routing_policy_ref": assessment_ref,
                "verifier_routing_policy_ref": verifier_ref,
                "credential_slot_refs": [
                    "credential-slot:openai", "credential-slot:anthropic",
                ],
                "assessment_provider_retry": {
                    "max_same_profile_retries": 4,
                    "retry_backoff_seconds": 2,
                },
                "verifier_provider_retry": {
                    "max_same_profile_retries": 0,
                    "retry_backoff_seconds": 5,
                },
                "assessment_transport_retry": {
                    "queue_wait_seconds": 600,
                    "max_definitely_not_sent_retries": 1,
                    "retry_backoff_seconds": 2,
                },
                "verifier_transport_retry": {
                    "queue_wait_seconds": 7200,
                    "max_definitely_not_sent_retries": 0,
                    "retry_backoff_seconds": 0,
                },
            }
            config_path.write_text(json.dumps({
                "thesis_impact": {
                    "enabled": True,
                    "interval_seconds": 60,
                    "config": config,
                },
            }))
            principal = Principal(
                "thesis-impact", "thesis-impact-test-token",
                THESIS_IMPACT_OPERATIONS,
                actor_ref="system:thesis-impact-model-worker",
            )
            server = WriterServer(
                state / "core.sqlite", state / "writer.sock",
                {principal.principal_id: principal},
                scheduler_path=state / "scheduler.sqlite",
            )
            server.start()
            try:
                bindings = server._thesis_impact_control.model_execution_bindings
                self.assertEqual(
                    bindings["assessment"]["provider_retry"],
                    config["assessment_provider_retry"],
                )
                self.assertEqual(
                    bindings["verification"]["transport_retry"],
                    config["verifier_transport_retry"],
                )
                self.assertEqual(
                    bindings["assessment"]["adapter_timeout_seconds"], 180.0
                )
                self.assertNotEqual(
                    bindings["assessment"]["routing_policy_hash"],
                    bindings["verification"]["routing_policy_hash"],
                )
                self.assertGreaterEqual(server._scheduler.max_attempts, 5)
                self.assertGreaterEqual(server._scheduler.max_lease_seconds, 7380)
            finally:
                server.stop()

    def test_bootstrap_installs_scoped_principal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = bootstrap(root / "state", root / "service.json")
            principal = load_principals(result["token_config"])["thesis-impact"]
            self.assertEqual(principal.operations, THESIS_IMPACT_OPERATIONS)
            self.assertFalse(principal.is_unrestricted)
            self.assertEqual(
                principal.actor_ref, "system:thesis-impact-model-worker"
            )


if __name__ == "__main__":
    unittest.main()
