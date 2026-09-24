"""A surface product never repeats itself, and a localization that does is refused.

Live 2026-09-24: 15 of 17 event-judgement localizations had a second section
that was the first one cut at 400 characters (``a1e694389873``), and the
verifier passed every one -- correctly, because the *source* said the same
thing twice: a no_change judgement's ``effect.reason`` is ``because[:400]``.
"""

from __future__ import annotations

import json
import sqlite3
import unittest

from dalton_core.final_surface_products import final_surface_products, truncated_copy_of
from dalton_core.research_localization import (
    ResearchLocalizationError,
    truncated_copy_pairs,
    validate_no_truncated_copies,
)

COMPANY = "company:a"
MISSION = {"mission_ref": "mission:m", "industry_ref": "industry:i",
           "universe": [{"company_ref": COMPANY}]}
BECAUSE = ("首选解读：这条销售评论只反映业绩前的仓位与情绪，现有预测驱动全部维持。"
           "本条新增的已核实结论都来自交易台或第三方。" * 12)


class SurfaceProjectionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript("""
        CREATE TABLE event_judgements (
          judgement_id TEXT, company_ref TEXT, record_json TEXT, content_hash TEXT,
          created_at TEXT);
        CREATE TABLE research_cycle_reflection_versions (
          version_id TEXT, mission_ref TEXT, iso_week TEXT, version_number INTEGER,
          content_hash TEXT, record_json TEXT);
        """)

    def judgement(self, identity, body):
        self.db.execute("INSERT INTO event_judgements VALUES (?,?,?,?,?)",
                        (identity, COMPANY, json.dumps(body, ensure_ascii=False),
                         identity + "-hash", "2026-09-24T11:00:00"))

    def test_a_no_change_judgement_is_one_section_not_the_same_one_twice_cut(self):
        self.judgement("judgement:no-change", {
            "because": BECAUSE, "note": None,
            "effect": {"kind": "no_change", "status": "recorded",
                       "reason": BECAUSE[:400]}})
        product = final_surface_products(self.db, MISSION, COMPANY)[0]
        self.assertEqual([section["body"] for section in product["sections"]], [BECAUSE])
        validate_no_truncated_copies(product)

    def test_a_distinct_effect_reason_is_still_shown(self):
        self.judgement("judgement:research", {
            "because": BECAUSE, "note": None,
            "effect": {"kind": "research", "status": "refused",
                       "reason": "the research task admission entry point is not installed"}})
        product = final_surface_products(self.db, MISSION, COMPANY)[0]
        self.assertEqual(len(product["sections"]), 2)

    def test_the_whole_copy_replaces_a_cut_copy_that_came_first(self):
        self.judgement("judgement:order", {
            "because": BECAUSE[:300] + "…", "note": BECAUSE, "effect": {}})
        product = final_surface_products(self.db, MISSION, COMPANY)[0]
        self.assertEqual([section["body"] for section in product["sections"]], [BECAUSE])

    def test_reflection_table_keys_and_dashes_are_not_sent_for_translation(self):
        body = {"narrative": {"title": "我们把时间花在哪", "prose": "这一周花费很少。",
                              "table": [
                                  {"pool": "document_extraction", "lane": "—",
                                   "note": "抽取占了一半花费"},
                                  {"pool": "document-extraction", "lane": "—",
                                   "note": "抽取占了一半花费"}]},
                "policy_suggestions": [], "authority_note": "只读。"}
        self.db.execute(
            "INSERT INTO research_cycle_reflection_versions VALUES (?,?,?,?,?,?)",
            ("reflection:1", "mission:m", "2026-W38", 1, "h",
             json.dumps(body, ensure_ascii=False)))
        product = next(item for item in final_surface_products(self.db, MISSION, COMPANY)
                       if item["kind"] == "surface_cycle_reflection")
        bodies = [section["body"] for section in product["sections"]]
        self.assertEqual(bodies, ["我们把时间花在哪", "这一周花费很少。", "只读。",
                                  "抽取占了一半花费"])


class TruncatedCopyCheckTests(unittest.TestCase):
    def test_the_predicate(self):
        self.assertTrue(truncated_copy_of(BECAUSE[:400], BECAUSE))
        self.assertTrue(truncated_copy_of(BECAUSE[:400] + "…", BECAUSE))
        self.assertTrue(truncated_copy_of(BECAUSE, BECAUSE))
        self.assertFalse(truncated_copy_of(BECAUSE, BECAUSE[:400]))
        # A short shared opening is a coincidence, not a copy.
        self.assertFalse(truncated_copy_of("首选解读：", BECAUSE))

    def test_a_source_that_repeats_itself_is_refused_before_any_call(self):
        product = {"kind": "surface_event_judgement", "sections": [
            {"title": "事件研判", "body": BECAUSE, "gaps": []},
            {"title": "事件研判", "body": BECAUSE[:400], "gaps": []}]}
        self.assertEqual(truncated_copy_pairs([BECAUSE, BECAUSE[:400]]), [(1, 0)])
        with self.assertRaisesRegex(ResearchLocalizationError, "truncated copy"):
            validate_no_truncated_copies(product)

    def test_a_draft_that_copies_one_section_into_another_is_refused(self):
        other = "另一段完全不同的内容，讲的是订单与收入之间的滞后关系，以及管理层怎样解释它。"
        product = {"kind": "dossier", "sections": [
            {"title": "a", "body": BECAUSE, "gaps": []},
            {"title": "b", "body": other, "gaps": []}]}
        validate_no_truncated_copies(product)
        draft = {"sections": [
            {"index": 0, "title": "甲", "body": BECAUSE, "gaps": []},
            {"index": 1, "title": "乙", "body": BECAUSE[:200], "gaps": []}]}
        with self.assertRaisesRegex(ResearchLocalizationError, "localized section 1"):
            validate_no_truncated_copies(product, draft)
        fine = {"sections": [
            {"index": 0, "title": "甲", "body": BECAUSE, "gaps": []},
            {"index": 1, "title": "乙", "body": other, "gaps": []}]}
        validate_no_truncated_copies(product, fine)

    def test_short_repeated_labels_do_not_make_a_product_untranslatable(self):
        product = {"kind": "debate_map", "sections": [
            {"title": "多方", "body": "尚未形成判断", "gaps": []},
            {"title": "空方", "body": "尚未形成判断", "gaps": []}]}
        validate_no_truncated_copies(product)
        validate_no_truncated_copies(product, {"sections": [
            {"index": 0, "title": "多方", "body": "尚未形成判断", "gaps": []},
            {"index": 1, "title": "空方", "body": "尚未形成判断", "gaps": []}]})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
