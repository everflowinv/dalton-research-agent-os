"""Published products are readable without creating or changing authorities."""
import json

from dalton_core.cockpit_research_library import research_library
from tests.test_mission_deliverable import ACN, DeliverableHarness


class ResearchLibraryTests(DeliverableHarness):
    def test_published_document_is_readable_under_query_only(self):
        self.grant("deliverable")
        published = self.publish([{"title": "Business", "body": "A sourced business description.",
                                   "claim_refs": [], "numbers": [], "gaps": ["Consensus unavailable"]}])
        before = self.store.connection.total_changes
        self.store.connection.execute("PRAGMA query_only=ON")
        result = research_library(self.store.connection, self.mission, ACN)
        product = next(p for p in result["products"] if p["kind"] == "initial_screen")
        self.assertEqual(product["version_ref"], published["id"])
        self.assertEqual(product["status"], "available")
        self.assertEqual(product["sections"][0]["gaps"], ["Consensus unavailable"])
        self.assertEqual(product["mission_binding"], "current")
        self.assertNotIn("approved", product)
        self.assertEqual(before, self.store.connection.total_changes)
        self.assertTrue(all(p["status"] == "missing" for p in result["products"]
                            if p["kind"] != "initial_screen"))

    def test_absent_products_do_not_appear_published(self):
        result = research_library(self.store.connection, self.mission, ACN)
        self.assertEqual(len(result["products"]), 5)
        self.assertTrue(all(p["status"] == "missing" and not p["sections"]
                            for p in result["products"]))

    def test_other_company_is_not_read_through_this_mission(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            research_library(self.store.connection, self.mission, "company:outside")

    def test_corrupt_document_fails_independently_of_other_products(self):
        self.grant("deliverable")
        self.publish([{"title": "Business", "body": "Original description.",
                       "claim_refs": [], "numbers": [], "gaps": []}])
        # Deliberately corrupt an isolated fixture, never a live authority.
        rows = self.store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='mission_deliverable_versions'").fetchall()
        for row in rows:
            self.store.connection.execute('DROP TRIGGER "' + row["name"].replace('"', '""') + '"')
        row = self.store.connection.execute("SELECT record_json FROM mission_deliverable_versions").fetchone()
        record = json.loads(row["record_json"])
        record["sections"][0]["body"] = "Corrupted description."
        self.store.connection.execute("UPDATE mission_deliverable_versions SET record_json=?", (json.dumps(record),))
        result = research_library(self.store.connection, self.mission, ACN)
        self.assertEqual([p["status"] for p in result["products"]],
                         ["missing", "missing", "missing", "invalid", "missing"])

    def test_gap_wording_does_not_change_raw_snapshot_or_review_lookup(self):
        from unittest.mock import patch
        self.grant("deliverable")
        self.publish([{"title": "供给与成本", "body": "交付成本仍需细化。",
                       "claim_refs": [], "numbers": [], "gaps": ["delivery_cost：缺少人员数量"]}])
        raw = research_library(self.store.connection, self.mission, ACN, localize=False)
        with patch("dalton_core.research_localization_store.localize_library", side_effect=lambda conn, value: __import__("copy").deepcopy(value)) as lookup:
            display = research_library(self.store.connection, self.mission, ACN)
        # The lookup receives the untouched snapshot, before any display fields.
        lookup_product = next(p for p in lookup.call_args.args[1]["products"] if p["kind"] == "initial_screen")
        self.assertNotIn("display_gaps", lookup_product["sections"][0])
        original = next(p for p in raw["products"] if p["kind"] == "initial_screen")
        shown = next(p for p in display["products"] if p["kind"] == "initial_screen")
        self.assertNotIn("display_gaps", original["sections"][0])
        self.assertEqual(shown["sections"][0]["gaps"], original["sections"][0]["gaps"])
        self.assertEqual(shown["sections"][0]["display_gaps"], ["交付成本：缺少人员数量"])
        self.assertEqual(shown["content_hash"], original["content_hash"])
