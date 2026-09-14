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


if __name__ == "__main__":
    unittest.main()
