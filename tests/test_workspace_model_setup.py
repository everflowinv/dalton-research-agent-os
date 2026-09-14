import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.model_deployment import POLICY_REF, install_openclaw_catalog
from dalton_core.model_router import ModelRouter
from dalton_core.store import content_hash
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from dalton_core.workspace import create_workspace_manifest
from dalton_core.workspace_model_setup import (
    WorkspaceModelSetupError,
    EXPECTED_CONFIG_NAMES,
    export_runtime_template,
    install_runtime_template,
)


class WorkspaceModelSetupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "legacy-state"
        self.source.mkdir()
        now = datetime(2026, 9, 14, tzinfo=timezone.utc)
        install_openclaw_catalog(self.source / "model-router.sqlite", checked_at=now)
        self.router_keeper = ModelRouter(self.source / "model-router.sqlite")
        self.budget_keeper = ThesisImpactBudgetStore(
            self.source / "thesis-impact-budget.sqlite", clock=lambda: now)
        self.budget_keeper.register_policy(
            policy_version_id="budget-policy:workspace-default:1",
            day_cap_micros=5_000_000)
        self.socket = self.root / "host-model.sock"
        self.key = self.root / "host-model.key"
        self.shared_policy = self.root / "shared-call-budget-policy.json"
        policy = {"schema_version": "dalton-shared-call-budget-policy-0.1",
                  "default_max_cost_usd": 1.0, "purpose_max_cost_usd": {},
                  "revision": 1, "prior_hash": None,
                  "updated_at": "2026-09-14T00:00:00+00:00",
                  "actor_ref": "human:owner"}
        self.shared_policy.write_text(json.dumps({**policy, "content_hash": content_hash(policy)}))
        base = {
            "routing_policy_ref": POLICY_REF,
            "credential_slot_refs": ["credential-slot:openclaw:openai"],
            "model_router_db": str(self.source / "model-router.sqlite"),
            "broker_socket": str(self.socket), "broker_auth_key": str(self.key),
            "broker_client_id": "client:workspace-test", "expected_agent_id": "main",
            "budget_db": str(self.source / "thesis-impact-budget.sqlite"),
            "budget_policy_ref": "budget-policy:workspace-default:1",
            "shared_call_budget_policy_path": str(self.shared_policy),
        }
        for name in EXPECTED_CONFIG_NAMES:
            (self.source / name).write_text(
                json.dumps(base), encoding="utf-8")
        self.bundle_path = self.root / "runtime-template.json"

    def tearDown(self):
        self.router_keeper.close()
        self.budget_keeper.close()
        self.temp.cleanup()

    def _workspace(self, *, exact_broker_paths=True):
        host = self.root / "fleet"
        release = self.root / "release"
        release.mkdir(exist_ok=True)
        shared = [release, self.shared_policy]
        if exact_broker_paths:
            shared += [self.socket, self.key]
        return create_workspace_manifest(
            host, "fresh", 18991, "release:sha256:" + "a" * 64, release,
            shared_readonly_paths=shared,
        )

    def test_export_and_install_redeclare_only_empty_local_authorities(self):
        bundle = export_runtime_template(self.source, self.bundle_path)
        rendered = self.bundle_path.read_text(encoding="utf-8")
        self.assertNotIn(str(self.source), rendered)
        self.assertNotIn("key bytes", rendered)
        workspace = self._workspace()
        receipt = install_runtime_template(workspace.manifest_path, self.bundle_path)
        self.assertEqual(receipt["model_calls"], 0)
        self.assertEqual(len(receipt["configs"]), 21)
        config = json.loads((workspace.state_dir / "claim-index-model-config.json").read_text())
        self.assertEqual(config["model_router_db"], str(workspace.state_dir / "model-router.sqlite"))
        self.assertEqual(config["budget_db"], str(workspace.state_dir / "thesis-impact-budget.sqlite"))
        self.assertEqual(config["broker_socket"], str(self.socket.resolve()))
        self.assertEqual(config["shared_call_budget_policy_path"],
                         str(self.shared_policy.resolve()))
        import sqlite3
        with sqlite3.connect(workspace.state_dir / "thesis-impact-budget.sqlite") as budget:
            self.assertEqual(budget.execute(
                "SELECT count(*) FROM thesis_impact_day_admissions").fetchone()[0], 0)
            self.assertEqual(budget.execute(
                "SELECT count(*) FROM thesis_impact_budget_policies").fetchone()[0], 1)

    def test_install_requires_exact_shared_readonly_broker_bindings(self):
        export_runtime_template(self.source, self.bundle_path)
        workspace = self._workspace(exact_broker_paths=False)
        with self.assertRaisesRegex(WorkspaceModelSetupError, "exact shared_readonly_paths"):
            install_runtime_template(workspace.manifest_path, self.bundle_path)

    def test_hash_tamper_and_cross_state_source_authority_are_rejected(self):
        bundle = export_runtime_template(self.source, self.bundle_path)
        bundle["broker"]["socket_path"] = str(self.root / "other.sock")
        self.bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
        with self.assertRaisesRegex(WorkspaceModelSetupError, "hash is invalid"):
            install_runtime_template(self._workspace().manifest_path, self.bundle_path)
        config_path = self.source / "claim-index-model-config.json"
        config = json.loads(config_path.read_text())
        config["budget_db"] = str(self.root / "foreign" / "budget.sqlite")
        config_path.write_text(json.dumps(config))
        with self.assertRaisesRegex(WorkspaceModelSetupError, "inside source state"):
            export_runtime_template(self.source, self.bundle_path)


if __name__ == "__main__":
    unittest.main()
