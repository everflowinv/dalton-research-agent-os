import json
import sqlite3
import unittest

from dalton_core.legacy_gate_disposition import legacy_gate_dispositions
from dalton_core.store import content_hash


class LegacyGateTests(unittest.TestCase):
    def test_supersession_is_same_mission_hash_bound_and_read_only(self):
        core = sqlite3.connect(":memory:")
        self.addCleanup(core.close)
        core.executescript("""
            CREATE TABLE mission_deliverable_versions(version_id TEXT,mission_version_ref TEXT,
                content_hash TEXT,created_at TEXT,kind TEXT,subject_ref TEXT);
            CREATE TABLE deep_insight_gate_versions(record_json TEXT,content_hash TEXT,
                company_ref TEXT,version_number INTEGER);
        """)
        for mission in ("mission:one", "mission:two"):
            core.execute("INSERT INTO mission_deliverable_versions VALUES(?,?,?,?,?,?)",
                         ("copy:"+mission,mission,"a"*64,"2026-10-09","deep_insight_gate","company:SYN"))
        for version, mission, corrupt in ((1,"mission:one",False),(2,"mission:one",True),(3,"mission:other",False)):
            record = {"id": f"gate:{version}", "version":version,"created_at":"2026-10-09",
                      "bindings":{"mission_version_ref":mission},"answers":[]}
            record["content_hash"] = content_hash(record)
            if corrupt:
                record["bindings"]["mission_version_ref"] = "tampered"
            core.execute("INSERT INTO deep_insight_gate_versions VALUES(?,?,?,?)",
                         (json.dumps(record),record["content_hash"],"company:SYN",version))
        before = core.total_changes
        core.execute("PRAGMA query_only=ON")
        result = {x["version_ref"]:x for x in legacy_gate_dispositions(core,"company:SYN")}
        self.assertEqual(result["copy:mission:one"]["status"], "superseded")
        self.assertEqual(result["copy:mission:one"]["superseded_by"], "gate:1")
        self.assertEqual(result["copy:mission:two"]["status"], "historical_copy")
        self.assertIsNone(result["copy:mission:two"]["replacement"])
        self.assertEqual(core.total_changes, before)


if __name__ == "__main__":
    unittest.main()
