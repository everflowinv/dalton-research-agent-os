import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.connector_governance import YFINANCE_CALENDAR_CAPABILITY_ID
from dalton_core.coverage_admission import CoverageAdmissionAuthority
from dalton_core.store import DaltonStore, content_hash
from dalton_core.workspace_creation import create_blank_workspace
from dalton_core.workspace_runtime_setup import install


class WorkspaceRuntimeSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.temporary.name)
        self.release = self.root / "release"
        self.release.mkdir()
        source = {
            "id": "connector-profile:yfinance:1",
            "connector_ref": "connector:yahoo-finance",
            "capability_id": YFINANCE_CALENDAR_CAPABILITY_ID,
            "auth_mode": "none",
            "credential_slot_refs": [],
            "allowed_operations": ["calendar"],
            "allowed_hosts": ["query1.finance.yahoo.com"],
            "transport": {"kind": "connector", "endpoint_ref": "adapter:yfinance",
                          "socket_path": None, "config_path": None},
        }
        body = {"schema_version": "dalton-shared-connection-catalog-0.1",
                "models": [], "sources": [source]}
        catalog = {**body, "content_hash": content_hash(body)}
        self.catalog_path = self.root / "connections.json"
        self.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        receipt = create_blank_workspace(
            self.root / "fleet", "blank", 18891,
            "release:sha256:" + "a" * 64, self.release,
            request_id="request-123", display_name="Blank",
            shared_readonly_paths=(self.catalog_path, self.release),
            connection_catalog={"path": str(self.catalog_path),
                                "content_hash": catalog["content_hash"]},
        )
        self.manifest = Path(receipt["manifest_path"])
        self.state = Path(receipt["config_path"]).parent.parent / "state/dalton-core"

    def tearDown(self):
        self.temporary.cleanup()

    def test_installs_real_mission_neutral_foundations_and_local_connection_authority(self):
        result = install(self.manifest, actor_ref="human:owner@example.com")
        self.assertEqual(result["setup_state"], "awaiting_mission")
        for name in ("market-proxy-mappings.json", "tracking-policy.json",
                     "research-foundation.json"):
            self.assertTrue((self.state / name).is_file())
        governance = json.loads((self.state / "connector-governance"
                                 / "yfinance-calendar-v1.json").read_text())
        self.assertEqual(governance["status"], "approved")
        self.assertEqual(governance["approved_by"], "human:owner@example.com")
        self.assertNotIn("lumos", json.dumps(governance))
        foundation = json.loads((self.state / "research-foundation.json").read_text())
        self.assertEqual(foundation["mission_defaults"]["source_plan"], [{
            "source_ref": "source:yahoo-finance", "role": "shared connected evidence source",
            "status": "connected"}])
        self.assertTrue(foundation["methods"]["playbook"]["binding"]["ref"].startswith(
            "research-playbook-version:"))
        template = foundation["methods"]["driver_pack_template"]["value"]
        with DaltonStore(self.state / "core.sqlite") as store:
            driver_pack = CoverageAdmissionAuthority(store).register_driver_pack(
                "driver-pack:test", industry_ref="industry:test", title="Test",
                actor_ref="human:owner@example.com", version_id="driver-pack-version:test:1",
                prior_version_ref=None, idempotency_key="driver-pack:test:1", **template)
        self.assertEqual(driver_pack["industry_ref"], "industry:test")
        self.assertEqual(foundation["methods"]["driver_templates"]["content_hash"],
                         __import__("dalton_core.driver_template", fromlist=["REGISTRY_HASH"]).REGISTRY_HASH)
        serialized = "\n".join(path.read_text(encoding="utf-8")
                               for path in self.state.rglob("*.json"))
        for legacy in ("coverage-mission:us-it-services", "ACN", "CTSH", "EPAM", "IBM"):
            self.assertNotIn(legacy, serialized)
        with sqlite3.connect(self.state / "core.sqlite") as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM coverage_mission_versions").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM research_playbook_versions").fetchone()[0], 1)

    def test_replay_preserves_owner_edits_and_does_not_copy_research(self):
        first = install(self.manifest, actor_ref="human:owner@example.com")
        proxy = self.state / "market-proxy-mappings.json"
        proxy.write_text('{"owner":"edit"}\n', encoding="utf-8")
        second = install(self.manifest, actor_ref="human:owner@example.com")
        self.assertEqual(proxy.read_text(), '{"owner":"edit"}\n')
        self.assertEqual(second["files"]["market-proxy-mappings.json"], "preserved")
        self.assertFalse(first["research_state_copied"])
        self.assertFalse(second["legacy_approvals_copied"])

    def test_requires_workspace_owner_actor(self):
        with self.assertRaisesRegex(Exception, "workspace owner"):
            install(self.manifest, actor_ref="automation:setup")

    def test_operation_specific_host_connectors_share_their_registered_source_identity(self):
        from dalton_core.workspace_runtime_setup import _source_ref
        for vendor, operations in (("company-wiki", ("get_document", "list_documents")),
                                   ("sales-notes", ("get_note", "list_notes"))):
            for operation in operations:
                self.assertEqual(_source_ref({"connector_ref": f"connector:host-tool:{vendor}:{operation}"}),
                                 f"source:{vendor}")

    def test_cockpit_first_goal_plans_once_and_publishes_existing_authorities(self):
        import os
        from unittest.mock import patch
        from dalton_core.cockpit_plane import CockpitConfig, CockpitPlane, CockpitConflict
        from dalton_core.workspace import load_workspace_manifest
        from dalton_core.workspace_mission_setup import publish_first_mission_to_store
        install(self.manifest, actor_ref="human:owner@example.com")
        workspace = load_workspace_manifest(self.manifest)
        model_calls = []
        class Model:
            def call_setup(self, **kwargs):
                model_calls.append(kwargs)
                return {"text": json.dumps({"industry": {"name": "semiconductors"},
                    "suggested_companies": [{"ticker": "ASML"}],
                    "research_questions": ["How durable is pricing power?"],
                    "title": "ASML pricing", "objective": "Understand pricing power"}),
                    "cost_micros": 2500, "replayed": False}
        def governance(_tokens, _socket, *, actor_ref, operation, params):
            self.assertEqual(operation, "publish_first_workspace_mission")
            self.assertEqual(params["workspace_manifest"]["workspace_id"], workspace.workspace_id)
            with DaltonStore(self.state / "core.sqlite") as store:
                return publish_first_mission_to_store(store, workspace,
                    proposal=params["proposal"], proposal_hash=params["proposal_hash"],
                    actor_ref=actor_ref, method_foundation=params["method_foundation"])
        config = CockpitConfig(core_db=self.state / 'core.sqlite', state_dir=self.state,
            heartbeat_path=self.state / 'run/heartbeat.json', scheduler_db=self.state / 'scheduler.sqlite',
            journal_path=self.state / 'cockpit/journal.sqlite')
        with patch.dict(os.environ, DALTON_WORKSPACE_MANIFEST=str(self.manifest)):
            plane = CockpitPlane(config, writer_socket=workspace.writer_socket,
                token_config=self.state / 'writer-tokens.json', governance_call=governance)
            try:
                with patch.object(plane, '_model_instance', return_value=Model()):
                    first = plane._initial_goal_draft('owner@example.com', 'Research ASML', 'first-goal')
                    repeated = plane._initial_goal_draft('owner@example.com', 'Research ASML', 'first-goal')
                self.assertEqual(len(model_calls), 1)
                self.assertTrue(repeated['replayed'])
                self.assertEqual(first['cost_usd'], .0025)
                self.assertEqual(plane.overview()['initial_goal']['status'], 'open')
                with self.assertRaises(CockpitConflict):
                    plane.publish_draft('owner@example.com', {'draft_id': first['draft_id'],
                        'draft_hash': '0' * 64, 'request_id': 'confirm'})
                result = plane.publish_draft('owner@example.com', {'draft_id': first['draft_id'],
                    'draft_hash': first['draft_hash'], 'request_id': 'confirm'})
                self.assertEqual(result['status'], 'published')
                self.assertEqual(result['version'], 1)
                with sqlite3.connect(self.state / 'core.sqlite') as core:
                    self.assertEqual(core.execute('SELECT count(*) FROM coverage_mission_pointer').fetchone()[0], 1)
            finally:
                plane.close()


if __name__ == "__main__":
    unittest.main()
