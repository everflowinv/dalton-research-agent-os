from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from dalton_core.bootstrap import bootstrap
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from dalton_core.thesis_impact_production import (
    ThesisImpactProductionConfig,
    ThesisImpactProductionError,
    ThesisImpactProductionRunner,
)
from dalton_core.writer_server import (
    THESIS_IMPACT_OPERATIONS,
    Principal,
    WriterServer,
    load_principals,
    write_token_config,
)


class ThesisImpactProductionTests(unittest.TestCase):
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
