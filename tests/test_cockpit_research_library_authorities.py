from __future__ import annotations

import json
import unittest

from dalton_core.cockpit_research_library import research_library
from tests import test_debate_map as debate_fixtures
from tests import test_investment_memo_decision_real as memo_fixtures

SUBJECT = debate_fixtures.SUBJECT
valid_debate = debate_fixtures.valid_debate


class DebateLibraryAuthorityTests(unittest.TestCase):
    setUp = debate_fixtures.DebateMapAuthorityTests.setUp
    publish = debate_fixtures.DebateMapAuthorityTests.publish
    def test_positions_keep_the_authority_vocabulary(self):
        self.publish([valid_debate()], refs=["cv-a"])
        result = research_library(self.store.connection, self.mission, SUBJECT)
        item = next(row for row in result["products"] if row["kind"] == "debate_map")
        self.assertEqual(item["status"], "available")
        by_title = {row["title"]: row for row in item["sections"]}
        market = next(row for title, row in by_title.items() if title.endswith("市场看法"))
        ours = next(row for title, row in by_title.items() if title.endswith("我们的判断"))
        self.assertEqual(market["position"], {"available": True, "lean": "bear"})
        self.assertEqual(ours["position"], {"state": "held", "side": "bull"})
        industry = next(row for row in result["products"]
                        if row["kind"] == "industry_framework")
        self.assertEqual(industry["subject_ref"], self.mission["industry_ref"])

    def test_corrupt_debate_isolated_as_invalid(self):
        self.publish([valid_debate()], refs=["cv-a"])
        for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='debate_map_versions'"):
            self.store.connection.execute(
                'DROP TRIGGER "' + row["name"].replace('"', '""') + '"')
        stored = self.store.connection.execute(
            "SELECT version_id,record_json FROM debate_map_versions").fetchone()
        record = json.loads(stored["record_json"])
        record["debates"][0]["question"] = "corrupted"
        self.store.connection.execute(
            "UPDATE debate_map_versions SET record_json=? WHERE version_id=?",
            (json.dumps(record), stored["version_id"]),
        )
        result = research_library(self.store.connection, self.mission, SUBJECT)
        debate = next(row for row in result["products"] if row["kind"] == "debate_map")
        self.assertEqual(debate["status"], "invalid")
        self.assertTrue(all(row["kind"] == "debate_map" or row["status"] != "invalid"
                            for row in result["products"]))


class MemoLibraryAuthorityTests(unittest.TestCase):
    setUp = memo_fixtures.RealInvestmentMemoDecisionTests.setUp

    def test_publication_and_exact_human_approval_are_separate(self):
        memo_fixtures.RealInvestmentMemoDecisionTests.test_real_store_scheduler_router_publish_and_approve(self)
        result = research_library(self.store.connection, self.mission, self.company)
        memo = next(row for row in result["products"] if row["kind"] == "investment_memo")
        self.assertEqual(memo["status"], "available")
        self.assertEqual(memo["approval"]["status"], "approved")
        self.assertTrue(memo["approval"]["actor_ref"].startswith("human:"))


if __name__ == "__main__":
    unittest.main()
