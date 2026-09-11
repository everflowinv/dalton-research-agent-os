from __future__ import annotations

import json
import unittest

from dalton_core.cockpit_research_library import research_library
from dalton_core.company_dossier import CompanyDossierAuthority, SECTIONS
from dalton_core.store import content_hash
from tests import test_company_dossier as dossier_fixtures
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

    def test_dossier_progress_counts_typed_units_not_placeholder_sections(self):
        authority = CompanyDossierAuthority(self.store)
        for complete in (False, True):
            with self.subTest(complete=complete):
                ref = "claim-version:complete" if complete else "claim-version:partial"
                candidate = dossier_fixtures.body(
                    company_ref=self.company,
                    drafted_sections={unit: dossier_fixtures.drafted(unit, ref)
                                      for unit in (SECTIONS if complete else ("business_model",))},
                    classification_block=dossier_fixtures.classification(ref if complete else None),
                    variant_block=dossier_fixtures.variant(ref if complete else None),
                )
                candidate["bindings"]["mission_version_ref"] = self.mission["id"]
                published = authority.publish(candidate)
                before = self.store.connection.total_changes
                self.store.connection.execute("PRAGMA query_only=ON")
                try:
                    result = research_library(self.store.connection, self.mission, self.company)
                finally:
                    self.store.connection.execute("PRAGMA query_only=OFF")
                dossier = next(item for item in result["products"] if item["kind"] == "dossier")
                self.assertEqual(self.store.connection.total_changes, before)
                self.assertEqual(dossier["version_ref"], published["id"])
                self.assertEqual(dossier["status"], "available")
                self.assertEqual(dossier["completeness"]["status"],
                                 "all_units_drafted" if complete else "partial")
                self.assertEqual(dossier["completeness"]["drafted_units"], 12 if complete else 1)
                self.assertEqual(dossier["completeness"]["total_units"], 12)
                self.assertEqual(len(dossier["completeness"]["unavailable_units"]), 0 if complete else 11)

    def test_hash_valid_decision_must_bind_its_row_company_stage_and_mission(self):
        memo_fixtures.RealInvestmentMemoDecisionTests.test_real_store_scheduler_router_publish_and_approve(self)
        core = self.store.connection
        product = next(item for item in research_library(core, self.mission, self.company)["products"]
                       if item["kind"] == "investment_memo")
        ref = product["approval"]["decision_record_ref"]
        original = core.execute(
            "SELECT record_json,content_hash FROM coverage_mission_stage_records WHERE record_id=?",
            (ref,),
        ).fetchone()
        # This deliberately corrupts only an isolated fixture, preserving a valid JSON hash.
        for row in core.execute("SELECT name FROM sqlite_master WHERE type='trigger' "
                                "AND tbl_name='coverage_mission_stage_records'").fetchall():
            core.execute('DROP TRIGGER "' + row["name"].replace('"', '""') + '"')
        for field, value in (("id", "stage-record:wrong"), ("company_ref", "company:other"),
                             ("mission_version_ref", "mission-version:other"),
                             ("mission_version_hash", "0" * 64), ("stage_ref", "initial_screen"),
                             ("status", "gate_failed"), ("actor_ref", "automation:coverage-mission")):
            with self.subTest(field=field):
                decision = json.loads(original["record_json"])
                decision[field] = value
                decision["content_hash"] = content_hash(
                    {key: val for key, val in decision.items() if key != "content_hash"})
                core.execute("UPDATE coverage_mission_stage_records SET record_json=?,content_hash=? "
                             "WHERE record_id=?", (json.dumps(decision), decision["content_hash"], ref))
                item = next(item for item in research_library(core, self.mission, self.company)["products"]
                            if item["kind"] == "investment_memo")
                self.assertEqual(item["status"], "invalid")
                self.assertNotEqual(item.get("approval", {}).get("status"), "approved")
        core.execute("UPDATE coverage_mission_stage_records SET record_json=?,content_hash=? "
                     "WHERE record_id=?", (original["record_json"], original["content_hash"], ref))


if __name__ == "__main__":
    unittest.main()
