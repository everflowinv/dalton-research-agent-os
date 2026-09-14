import json
import plistlib
import tempfile
import unittest
from pathlib import Path

from dalton_core.bootstrap import bootstrap
from dalton_core.service import ServiceConfig
from dalton_core.macos_launchagent import render
from dalton_core.store import content_hash
from dalton_core.workspace import create_workspace_manifest
from dalton_core.workspace_control_setup import configure_workspace_control
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
                "openclaw_config_path": str(self.openclaw)},
                "research_review": {
                    "candidate_staging_path": str(self.source_state / "research-review/staging.sqlite"),
                    "document_extraction_model_config_path": str(
                        self.source_state / "document-extraction-model-config.json"),
                    "reconcile_interval_seconds": 73,
                    "transcript_review_directory": str(self.source_state / "research-review/inbox"),
                }}},
        })
        self.source_config.write_text(json.dumps(raw))
        (self.source_state / "document-research-config.json").write_text(json.dumps({
            "schema_version": "document-research-config-0.1",
            "purpose": "mission_directed_document_research",
            "inventory_preview_chars": 600,
            "spool_dir": str(self.source_state / "transcript-spool"),
            "enabled_sources": ["source:public-web"],
            "policy": {
                "schema_version": "document-research-policy-0.1",
                "policy_ref": "document-research-policy:test:1",
                "allowed_purposes": ["mission_directed_document_research"],
                "allowed_access_policy_refs": ["policy:access:public-web"],
                "max_query_terms": 16, "max_query_term_chars": 240,
                "max_results": 24, "max_read_chars": 100000,
                "max_question_chars": 12000, "max_context_before_chars": 2000,
                "max_context_after_chars": 4000, "content_hash": "a" * 64,
            },
            "source_reading_limits": {
                "alphaengine_max_document_chars": 10000000,
                "public_web_max_source_chars": 10000000,
                "public_web_max_pdf_pages": 2000,
                "public_web_max_decompressed_bytes": 100000000,
            },
        }))
        self.template = self.root / "service-template.json"

    def tearDown(self):
        self.temp.cleanup()

    def _workspace(self, include_broker=True, slug="fresh"):
        release = self.root / "release"
        release.mkdir(exist_ok=True)
        tailscale = self.root / "tailscale"
        tailscale.touch(exist_ok=True)
        shared = [release, tailscale, self.socket, self.key, self.openclaw] if include_broker else [release, tailscale]
        workspace = create_workspace_manifest(
            self.root / "fleet", slug, 18881 if slug == "fresh" else 18882,
            "release:sha256:" + "b" * 64,
            release, shared_readonly_paths=shared)
        bootstrap(workspace.state_dir, workspace.config_path,
                  workspace_manifest=workspace.manifest_path)
        (workspace.state_dir / "research-foundation.json").write_text(json.dumps({
            "mission_defaults": {"source_plan": [
                {"source_ref": "source:public-web", "status": "connected"},
                {"source_ref": "source:sec-edgar", "status": "connected"},
                {"source_ref": "source:guidepoint", "status": "connected"},
            ]},
        }))
        configure_workspace_control(
            workspace.manifest_path, owner_login="owner@example.com",
            tailscale_host="test.tail00000.ts.net", tailscale_executable=tailscale)
        return workspace

    def test_export_scrubs_subjects_and_install_makes_runnable_local_engine(self):
        template = export_service_template(self.source_config, self.template)
        rendered = self.template.read_text()
        self.assertNotIn(str(self.source_state), rendered)
        self.assertNotIn("company:secret", rendered)
        planner = template["operating"]["bounded_planner"]["config"]
        self.assertIsNone(planner["observation_mandate_version_ref"])
        self.assertIsNone(planner["doctrine_pack_version_ref"])
        self.assertIsNone(planner["doctrine_pack_version_hash"])
        self.assertEqual(template["shared_readonly_paths"],
                         [str(self.socket.resolve()), str(self.key.resolve()),
                          str(self.openclaw.resolve())])
        workspace = self._workspace()
        # The model template is installed before the service template in the
        # production sequence.
        for name in ("research-planner-model-config.json",
                     "document-extraction-model-config.json",
                     "discovery-selection-model-config.json",
                     "claim-index-model-config.json",
                     "registered-annual-report-draft-model-config.json",
                     "registered-annual-report-verifier-model-config.json"):
            (workspace.state_dir / name).touch()
        receipt = install_service_template(workspace.manifest_path, self.template)
        self.assertEqual(receipt["research_state"], "empty")
        installed = json.loads(workspace.config_path.read_text())
        self.assertTrue(installed["bounded_planner"]["enabled"])
        self.assertEqual(installed["bounded_planner"]["config"]["scheduler_db"],
                         str(workspace.state_dir / "scheduler.sqlite"))
        self.assertIsNone(installed["bounded_planner"]["config"][
            "observation_mandate_version_ref"])
        self.assertEqual(installed["thesis_impact"]["config"]["company_thesis_refs"], {})
        self.assertFalse(installed["outbox"]["enabled"])
        self.assertFalse(installed["weekly_brief"]["enabled"])
        review = installed["control"]["config"]["research_review"]
        self.assertEqual(review["reconcile_interval_seconds"], 73)
        self.assertEqual(review["candidate_staging_path"],
                         str(workspace.state_dir / "research-review/candidate-staging.sqlite"))
        self.assertEqual(json.loads((workspace.state_dir / "document-research-config.json").read_text())[
            "spool_dir"], str(workspace.state_dir / "transcript-spool"))
        self.assertEqual(json.loads((workspace.state_dir / "document-research-config.json").read_text())[
            "enabled_sources"], ["source:public-web", "source:sec-edgar"])
        self.assertEqual(installed["control"]["config"]["cockpit"]["model_config_path"],
                         str(workspace.state_dir / "research-planner-model-config.json"))
        sync = json.loads((workspace.state_dir / "model-catalog-sync.json").read_text())
        self.assertEqual(sync["model_router_db"], str(workspace.state_dir / "model-router.sqlite"))
        ServiceConfig.from_file(workspace.config_path)
        plists = render(
            self.root / "agents", self.root / "release/bin", workspace.state_dir,
            workspace.config_path, self.root / "logs",
            label_namespace="space.lumos.dalton.workspace.fresh",
            workspace_manifest_path=workspace.manifest_path)
        writer = plistlib.loads(Path(plists["writer"]).read_bytes())["ProgramArguments"]
        for flag in ("--candidate-staging", "--document-extraction-model-config",
                     "--discovery-selection-model-config", "--claim-index-model-config",
                     "--mission-document-research-lane", "--mission-annual-research-lane"):
            self.assertIn(flag, writer)
        # Extension installation is stable and keeps every path workspace-local.
        install_service_template(workspace.manifest_path, self.template)
        configure_workspace_control(
            workspace.manifest_path, owner_login="owner@example.com",
            tailscale_host="test.tail00000.ts.net", tailscale_executable=self.root / "tailscale")
        self.assertEqual(installed["workspace"],
                         json.loads(workspace.config_path.read_text())["workspace"])

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
