import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.workspace import create_workspace_manifest
from dalton_core.workspace_fleet import fleet_overview


class FleetOverviewTests(unittest.TestCase):
    def test_readonly_registry_does_not_touch_databases_or_merge_budgets(self):
        with tempfile.TemporaryDirectory() as folder:
            host = Path(folder)
            release = host / "release"
            release.mkdir()
            for slug, port in (("analyst-a", 18011), ("analyst-b", 18012)):
                create_workspace_manifest(host, slug, port,
                                          "release:sha256:" + "a" * 64, release)
            before = {str(p): p.read_bytes() for p in host.rglob("*") if p.is_file()}
            with patch("sqlite3.connect", side_effect=AssertionError("database accessed")):
                result = fleet_overview(host)
            self.assertEqual([r["slug"] for r in result["workspaces"]], ["analyst-a", "analyst-b"])
            self.assertTrue(all(r["runtime_health"] == "not_checked" for r in result["workspaces"]))
            self.assertEqual(result["aggregate_budget"], "not_computed")
            self.assertEqual(before, {str(p): p.read_bytes() for p in host.rglob("*") if p.is_file()})
            manifest = host / "workspaces" / "analyst-b" / "workspace.json"
            raw = json.loads(manifest.read_text())
            raw["cockpit_port"] = 18011
            manifest.write_text(json.dumps(raw))
            damaged = fleet_overview(host)
            self.assertEqual(damaged["workspaces"][1]["state"], "invalid")
            self.assertIsNone(damaged["workspaces"][1]["local_cockpit_url"])

    def test_missing_host_is_not_created_by_discovery(self):
        with tempfile.TemporaryDirectory() as folder:
            host = Path(folder) / "missing"
            self.assertEqual(fleet_overview(host)["workspaces"], [])
            self.assertFalse(host.exists())
