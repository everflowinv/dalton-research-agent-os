from __future__ import annotations
import importlib.util, json, sqlite3, tempfile, unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("workspace_catalog_export", ROOT / "scripts/export_workspace_connection_catalog.py")
MODULE = importlib.util.module_from_spec(SPEC); assert SPEC.loader; SPEC.loader.exec_module(MODULE)

class WorkspaceConnectionCatalogExportTests(unittest.TestCase):
    def test_excludes_retired_and_optionally_intersects_broker_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); model = root / "model.sqlite"; core = root / "core.sqlite"
            with closing(sqlite3.connect(model)) as db:
                db.execute("CREATE TABLE model_endpoint_profile_versions(profile_id TEXT,version INTEGER,profile_json TEXT)")
                base = {"provider":"p", "family":"f", "adapter_ref":"adapter:x", "credential_slot_ref":"slot:x",
                        "capabilities":["research"], "modalities":["text"]}
                rows = [{**base,"id":"profile:live","model":"live"},
                        {**base,"id":"profile:gone","model":"gone","status":"retired"},
                        {**base,"id":"profile:not-brokered","model":"other"}]
                db.executemany("INSERT INTO model_endpoint_profile_versions VALUES(?,?,?)",
                               [(row["id"], 1, json.dumps(row)) for row in rows])
                db.commit()
            with closing(sqlite3.connect(core)) as db:
                db.execute("CREATE TABLE connector_profile_versions(connector_ref TEXT,version_number INTEGER,record_json TEXT)")
                source = {"id":"profile:sec", "connector_ref":"connector:sec", "capability_id":"capability:sec",
                          "auth_mode":"none", "credential_slot_refs":[], "allowed_operations":["facts"],
                          "allowed_hosts":["data.sec.gov"], "adapter_ref":"adapter:sec"}
                db.execute("INSERT INTO connector_profile_versions VALUES(?,?,?)", ("connector:sec",1,json.dumps(source)))
                db.commit()
            output = root / "catalog.json"
            result = MODULE.export_catalog(model, core, root / "broker.sock", output, {"profile:live"})
            catalog = json.loads(output.read_text())
            self.assertEqual([row["id"] for row in catalog["models"]], ["profile:live"])
            self.assertEqual([row["id"] for row in catalog["sources"]], ["profile:sec"])
            self.assertTrue(result["broker_catalog_filtered"]); self.assertFalse(result["connectivity_verified"])
            self.assertFalse(result["credential_values_copied"]); self.assertNotIn("retirement", output.read_text())

if __name__ == "__main__": unittest.main()
