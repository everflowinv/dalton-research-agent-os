"""Published products are readable without creating or changing authorities."""
import json

from dalton_core.cockpit_research_library import (_comparison_table_display,
                                                   research_library)
from tests.test_mission_deliverable import ACN, DeliverableHarness


class ResearchLibraryTests(DeliverableHarness):
    def test_closed_comparison_keeps_all_twenty_rows_and_eight_periods(self):
        periods = [f"{year}Q{quarter}" for year in (2024, 2025)
                   for quarter in range(1, 5)]
        tickers = ("ACN", "CTSH", "EPAM", "IBM", "DXC")
        metrics = ("revenue", "revenue_yoy_growth", "gross_margin", "operating_margin")
        lines = ["company\tmetric\t" + "\t".join(periods)]
        for ticker in tickers:
            for metric in metrics:
                values = (["16405819000"] * 8 if metric == "revenue"
                          else ["8.34%"] * 8)
                lines.append("\t".join((ticker, metric, *values)))
        lines += ["", "# 各公司利润率口径不同，不可直接横向比较。"]
        number = {
            "period": "2024Q1",
            "text": "ACN 2024Q1（期末 2024-02-29）revenue 16405819000",
            "cell": {"kind": "statement_accession",
                     "ref": "comparison-cell:company-sec-cik-0001467373:revenue:2024Q1",
                     "accession": "0001467373-24-000001"},
        }
        shown = _comparison_table_display(
            "cross_company_comparison", "\n".join(lines), [number])
        self.assertIsNotNone(shown)
        self.assertEqual(len(shown["headers"]), 10)
        self.assertEqual(shown["headers"][2:], [
            "2024年第一季度", "2024年第二季度", "2024年第三季度", "2024年第四季度",
            "2025年第一季度", "2025年第二季度", "2025年第三季度", "2025年第四季度",
        ])
        self.assertEqual(len(shown["rows"]), 20)
        self.assertTrue(all(len(row) == 10 for row in shown["rows"]))
        self.assertEqual(shown["rows"][0][2:], ["164.1 亿美元"] * 8)
        self.assertEqual(shown["rows"][1][2:], ["8.3%"] * 8)
        self.assertEqual(shown["note"], "各公司利润率口径不同，不可直接横向比较。")
        self.assertEqual(shown["original"], "\n".join(lines))
        invalid = lines.copy()
        invalid[1] = invalid[1].replace("16405819000", "unknown", 1)
        self.assertIsNone(_comparison_table_display(
            "cross_company_comparison", "\n".join(invalid), [number]))

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
        self.publish([{"title": "供给与成本", "body": "fixed_cost_removal仍需细化。",
                       "claim_refs": [], "numbers": [], "gaps": ["delivery_cost：缺少人员数量"]}])
        raw = research_library(self.store.connection, self.mission, ACN, localize=False)
        with patch("dalton_core.research_localization_store.localize_library", side_effect=lambda conn, value: __import__("copy").deepcopy(value)) as lookup:
            display = research_library(self.store.connection, self.mission, ACN)
        # The lookup receives the untouched snapshot, before any display fields.
        lookup_product = next(p for p in lookup.call_args.args[1]["products"] if p["kind"] == "initial_screen")
        self.assertNotIn("display_gaps", lookup_product["sections"][0])
        self.assertNotIn("display_body", lookup_product["sections"][0])
        original = next(p for p in raw["products"] if p["kind"] == "initial_screen")
        shown = next(p for p in display["products"] if p["kind"] == "initial_screen")
        self.assertNotIn("display_gaps", original["sections"][0])
        self.assertNotIn("display_body", original["sections"][0])
        self.assertEqual(shown["sections"][0]["gaps"], original["sections"][0]["gaps"])
        self.assertEqual(shown["sections"][0]["display_gaps"], ["交付成本：缺少人员数量"])
        self.assertEqual(shown["sections"][0]["display_body"], "固定成本剥离进度仍需细化。")
        self.assertEqual(shown["content_hash"], original["content_hash"])


class DisplayLabelTests(DeliverableHarness):
    """2026-10-07 inspection: 「第 ? 版」 and ``causal_chain:0`` headings."""

    def test_version_label_is_attached_after_the_localization_lookup(self):
        from unittest.mock import patch
        self.grant("deliverable")
        self.publish([{"title": "Business", "body": "A sourced business description.",
                       "claim_refs": [], "numbers": [], "gaps": []}])
        with patch("dalton_core.research_localization_store.localize_library",
                   side_effect=lambda conn, value: __import__("copy").deepcopy(value)) as lookup:
            display = research_library(self.store.connection, self.mission, ACN)
        looked_up = next(p for p in lookup.call_args.args[1]["products"]
                         if p["kind"] == "initial_screen")
        self.assertNotIn("version_label", looked_up)
        shown = next(p for p in display["products"] if p["kind"] == "initial_screen")
        self.assertEqual(shown["version"], 1)
        self.assertEqual(shown["version_label"], "第 1 版")
        raw = research_library(self.store.connection, self.mission, ACN, localize=False)
        self.assertNotIn("version_label", next(p for p in raw["products"]
                                               if p["kind"] == "initial_screen"))

    def test_version_label_never_shows_a_question_mark(self):
        from dalton_core.cockpit_research_library import version_label
        self.assertEqual(version_label(42, "company-dossier-version:x:42"), "第 42 版")
        self.assertEqual(version_label(None, "company-dossier-version:company-sec-cik-1:42"),
                         "第 42 版")
        self.assertEqual(version_label(None, "debate-map-version:d00d505b6da78a2e"),
                         "版本 d00d505b")
        self.assertEqual(version_label(None, None), "版本未标明")
        html = (__import__("pathlib").Path(__file__).resolve().parents[1]
                / "src" / "dalton_core" / "cockpit_control.html").read_text()
        self.assertNotIn('p.version||"?"', html)

    def test_causal_chain_sections_are_headed_by_their_own_titles(self):
        from dalton_core.cockpit_research_library import _attach_display, _section_label
        record = {"sections": [
            {"link_index": 0, "title": "预算与宏观：审批周期与滞后"},
            {"link_index": 1, "title": None}]}
        labels = [_section_label("industry_framework", record, "causal_chain:0", "正文"),
                  _section_label("industry_framework", record, "causal_chain:1",
                                 "新签订单领先收入两到四个季度，转化节奏决定可见度。"),
                  _section_label("industry_framework", record, "industry_characteristics", "")]
        self.assertEqual(labels, ["因果链一 · 预算与宏观：审批周期与滞后",
                                  "因果链二 · 新签订单领先收入两到四个季度", "行业特征"])
        # The localized overlay may keep the id, or rename it inconsistently;
        # the heading follows the record either way.  A reviewed Chinese title
        # of a non-chain section is kept.
        product = {"sections": [{"title": "causal_chain:0"}, {"title": "因果链条二：订单"},
                                {"title": "行业特征：合同复利与周期属性"}]}
        _attach_display(product, {"version": 3, "version_label": "第 3 版",
                                  "section_labels": labels})
        self.assertEqual([s.get("label") for s in product["sections"]],
                         [labels[0], labels[1], None])
        self.assertEqual(product["version_label"], "第 3 版")
