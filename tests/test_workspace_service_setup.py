import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.bootstrap import bootstrap
from dalton_core.service import ServiceConfig
from dalton_core.store import content_hash
from dalton_core.workspace import create_workspace_manifest
from dalton_core.workspace_service_setup import (
    WorkspaceServiceSetupError,
    export_service_template,
    install_service_template,
)


class WorkspaceServiceSetupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.socket = self.root / "host" / "model.sock"
        self.key = self.root / "host" / "model.key"
        self.openclaw = self.root / "host" / "openclaw.json"
        self.source_state = self.root / "source-state"
        self.source_config = self.root / "source-service.json"
        bootstrap(self.source_state, self.source_config)
        raw = json.loads(self.source_config.read_text())
        planner_config = {
            "doctrine_pack_version_hash": "a" * 64,
            "doctrine_pack_version_ref": "doctrine-pack-version:default:1",
            "filed_window_days": 400, "max_probes_per_tick": 1,
            "max_response_bytes": 8388608,
            "observation_mandate_version_ref": "mandate-version:default:1",
            "planner_broker_auth_key": str(self.key),
            "planner_broker_client_id": "client:dalton-core",
            "planner_broker_socket": str(self.socket),
            "planner_call_budget": {"max_cost_usd": 3.0, "max_input_tokens": 250000,
                                    "max_output_tokens": 4000, "timeout_seconds": 300},
            "planner_credential_slot_refs": ["credential-slot:openclaw:deepseek"],
            "planner_expected_agent_id": "chem", "planner_max_cost_usd": 0.5,
            "planner_model_router_db": str(self.source_state / "model-router.sqlite"),
            "planner_routing_policy_ref": "model-routing-policy-version:planner:1",
            "scheduler_db": str(self.source_state / "scheduler.sqlite"),
            "timeout_seconds": 60.0,
            "token_config": str(self.source_state / "writer-tokens.json"),
            "user_agent": "Dalton test", "writer_socket": str(self.source_state / "run/writer.sock"),
        }
        thesis_config = {
            "assessment_routing_policy_ref": "model-routing-policy-version:assessment:1",
            "broker_auth_key": str(self.key), "broker_client_id": "client:dalton-core",
            "broker_socket": str(self.socket),
            "budget_db": str(self.source_state / "thesis-impact-budget.sqlite"),
            "budget_policy_version_id": "budget-policy:default:1",
            "company_thesis_refs": {"company:secret": "thesis:secret"},
            "credential_slot_refs": ["credential-slot:openclaw:openai"],
            "day_cap_micros": 25000000, "expected_agent_id": "chem", "max_targets": 25,
            "model_router_db": str(self.source_state / "model-router.sqlite"),
            "routing_policy_ref": "model-routing-policy-version:shared:1",
            "scheduler_db": str(self.source_state / "scheduler.sqlite"), "timeout_seconds": 180,
            "token_config": str(self.source_state / "writer-tokens.json"),
            "verifier_routing_policy_ref": "model-routing-policy-version:verifier:1",
            "writer_socket": str(self.source_state / "run/writer.sock"),
        }
        raw.update({
            "bounded_planner": {"enabled": True, "interval_seconds": 300,
                                "config": planner_config},
            "thesis_impact": {"enabled": False, "interval_seconds": 300,
                              "config": thesis_config},
            "document_extraction": {"max_windows_per_tick": 30,
                                    "numeric_windows_per_tick": 10,
                                    "discovery_windows_per_tick": 10},
            "agenda": {"enabled": False, "interval_seconds": 3600, "config": {}},
            "weekly_brief": {"enabled": False, "interval_seconds": 300, "config": {}},
            "outbox": {"enabled": False, "interval_seconds": 60, "config": {}},
            "control": {"enabled": False, "config": {"cockpit": {
                "openclaw_config_path": str(self.openclaw)}}},
        })
        self.source_config.write_text(json.dumps(raw))
        self.template = self.root / "service-template.json"

    def tearDown(self):
        self.temp.cleanup()

    def _workspace(self, include_broker=True, slug="fresh"):
        release = self.root / "release"
        release.mkdir(exist_ok=True)
        shared = [release, self.socket, self.key, self.openclaw] if include_broker else [release]
        workspace = create_workspace_manifest(
            self.root / "fleet", slug, 18881 if slug == "fresh" else 18882,
            "release:sha256:" + "b" * 64,
            release, shared_readonly_paths=shared)
        bootstrap(workspace.state_dir, workspace.config_path,
                  workspace_manifest=workspace.manifest_path)
        return workspace

    def test_export_scrubs_subjects_and_install_makes_runnable_local_engine(self):
        template = export_service_template(self.source_config, self.template)
        rendered = self.template.read_text()
        self.assertNotIn(str(self.source_state), rendered)
        self.assertNotIn("company:secret", rendered)
        self.assertEqual(template["shared_readonly_paths"],
                         [str(self.socket.resolve()), str(self.key.resolve()),
                          str(self.openclaw.resolve())])
        workspace = self._workspace()
        receipt = install_service_template(workspace.manifest_path, self.template)
        self.assertEqual(receipt["research_state"], "empty")
        installed = json.loads(workspace.config_path.read_text())
        self.assertTrue(installed["bounded_planner"]["enabled"])
        self.assertEqual(installed["bounded_planner"]["config"]["scheduler_db"],
                         str(workspace.state_dir / "scheduler.sqlite"))
        self.assertEqual(installed["thesis_impact"]["config"]["company_thesis_refs"], {})
        self.assertFalse(installed["outbox"]["enabled"])
        self.assertFalse(installed["weekly_brief"]["enabled"])
        sync = json.loads((workspace.state_dir / "model-catalog-sync.json").read_text())
        self.assertEqual(sync["model_router_db"], str(workspace.state_dir / "model-router.sqlite"))
        ServiceConfig.from_file(workspace.config_path)

    def test_exact_broker_declaration_and_template_hash_are_required(self):
        value = export_service_template(self.source_config, self.template)
        with self.assertRaisesRegex(WorkspaceServiceSetupError, "exact shared_readonly_paths"):
            install_service_template(self._workspace(include_broker=False).manifest_path,
                                     self.template)
        value["operating"]["bounded_planner"]["interval_seconds"] = 1
        self.template.write_text(json.dumps(value))
        with self.assertRaisesRegex(WorkspaceServiceSetupError, "hash is invalid"):
            install_service_template(self._workspace(slug="fresh-two").manifest_path,
                                     self.template)


if __name__ == "__main__":
    unittest.main()
