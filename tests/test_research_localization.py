from __future__ import annotations

import copy
import unittest

from dalton_core.research_localization import (
    ResearchLocalizationError,
    build_localization,
    build_prompt,
    build_verifier_prompt,
    select_localized,
    validate_localized_text,
    validate_localization,
)


def product():
    return {
        "kind": "dossier", "version_ref": "dossier-version:1", "status": "available",
        "approval": {"status": "pending_human_decision"},
        "sections": [{
            "title": "Demand", "body": "Revenue was USD 1,234.50, up 5.2%.",
            "gaps": ["Missing FY2027 margin"],
            "sources": [{"ref": "claim-version:" + "1" * 64}],
            "numbers": [{"text": "USD 1,234.50", "period": "FY2026"}],
        }],
    }


def localized():
    return {"sections": [{"index": 0, "title": "需求",
                           "body": "收入为 USD 1,234.50，同比增长 5.2%。",
                           "gaps": ["缺少 FY2027 利润率"]}]}


def verifier():
    return {"verdict": "pass", "faithful": True, "no_new_facts": True,
            "meaning_preserved": True, "findings": []}


class ResearchLocalizationTests(unittest.TestCase):
 def test_localization_replays_and_only_overlays_display_prose(self):
    source = product()
    candidate = build_localization(source, localized(), verifier())
    self.assertEqual(validate_localization(source, candidate), candidate)
    selected = select_localized(source, candidate)
    self.assertEqual(selected["sections"][0]["title"], "需求")
    self.assertTrue(selected["sections"][0]["body"].startswith("收入为"))
    self.assertEqual(selected["sections"][0]["sources"], source["sections"][0]["sources"])
    self.assertEqual(selected["sections"][0]["numbers"], source["sections"][0]["numbers"])
    self.assertEqual(selected["status"], source["status"])
    self.assertEqual(selected["approval"], source["approval"])
    self.assertNotIn("localization", source)


 def test_localization_rejects_source_drift_added_number_and_unclean_verifier(self):
    source = product()
    candidate = build_localization(source, localized(), verifier())
    changed = copy.deepcopy(source); changed["sections"][0]["body"] += " Changed."
    with self.assertRaisesRegex(ResearchLocalizationError, "source identity"):
        validate_localization(changed, candidate)
    added = localized(); added["sections"][0]["body"] += " 目标 6.0%。"
    with self.assertRaisesRegex(ResearchLocalizationError, "number tokens"):
        build_localization(source, added, verifier())
    failed = verifier(); failed["verdict"] = "reject"; failed["findings"] = ["删掉缺口"]
    with self.assertRaisesRegex(ResearchLocalizationError, "did not pass"):
        build_localization(source, localized(), failed)


 def test_prompts_require_chinese_fidelity_without_granting_new_research(self):
    producer = build_prompt(product())
    review = build_verifier_prompt(product(), localized())
    self.assertIn("fluent Simplified Chinese", producer)
    self.assertIn("not new research", producer)
    self.assertIn("Preserve every authoritative value", producer)
    self.assertIn("no_new_facts", review)
    self.assertIn("meaning_preserved", review)

 def test_preflight_sees_numbers_adjacent_to_chinese_and_ignores_opaque_refs(self):
    source = product()
    source["sections"][0]["body"] = (
        "2026年收入12.5亿元，来源 claim-version:" + "2" * 64)
    translated = {"sections": [{"index": 0, "title": "收入",
                                  "body": "收入在2026年为12.5亿元。",
                                  "gaps": ["Missing FY2027 margin"]}]}
    checked = validate_localized_text(source, translated)
    self.assertEqual(checked[0]["index"], 0)
    translated["sections"][0]["body"] = "收入在2026年为13.5亿元。"
    with self.assertRaisesRegex(ResearchLocalizationError, "number tokens"):
        validate_localized_text(source, translated)

 def test_english_number_words_and_months_may_be_rendered_as_chinese_digits(self):
    source = product()
    source["sections"][0]["body"] = (
        "GIS book-to-bill was below one; management discussed it in December.")
    translated = {"sections": [{"index": 0, "title": "订单趋势",
                                  "body": "GIS 订单出货比低于 1；管理层在 12 月讨论了这一点。",
                                  "gaps": ["缺少 FY2027 利润率"]}]}
    self.assertEqual(validate_localized_text(source, translated)[0]["index"], 0)
    translated["sections"][0]["body"] += " 目标为 13。"
    with self.assertRaises(ResearchLocalizationError) as caught:
        validate_localized_text(source, translated)
    self.assertIn('"section_index": 0', str(caught.exception))
    self.assertIn('"added_tokens": ["13"]', str(caught.exception))

 def test_preflight_reports_missing_and_added_tokens_by_section(self):
    source = product()
    bad = localized()
    bad["sections"][0]["body"] = "收入为 USD 1,235.50，同比增长 6.2%。"
    with self.assertRaises(ResearchLocalizationError) as caught:
        validate_localized_text(source, bad)
    message = str(caught.exception)
    self.assertIn('"section_index": 0', message)
    self.assertIn('"missing_tokens": ["1,234.50", "5.2%"]', message)
    self.assertIn('"added_tokens": ["1,235.50", "6.2%"]', message)

 def test_preflight_allows_only_unit_bound_deterministic_display_rounding(self):
    source = product()
    source["sections"][0]["body"] = "Revenue was 15623445 USD and margin was 5.24%."
    translated = {"sections": [{"index": 0, "title": "财务摘要",
                                  "body": "收入为 1562 万美元，利润率为 5.2%。",
                                  "gaps": ["缺少 FY2027 利润率"]}]}
    self.assertEqual(validate_localized_text(source, translated)[0]["index"], 0)
    translated["sections"][0]["body"] = "收入为 1563 万美元，利润率为 5.2%。"
    with self.assertRaisesRegex(ResearchLocalizationError, "number tokens"):
        validate_localized_text(source, translated)

 def test_preflight_rejects_an_all_english_display_section(self):
    source = product()
    with self.assertRaisesRegex(ResearchLocalizationError, "no Simplified Chinese"):
        validate_localized_text(source, {"sections": [{
            "index": 0, "title": "Demand",
            "body": "Revenue was USD 1,234.50, up 5.2%.",
            "gaps": ["Missing FY2027 margin"],
        }]})

 def test_internal_section_ids_and_thesis_labels_are_not_financial_numbers(self):
    source = product()
    source['sections'][0].update(title='causal_chain:0', body='T1 supports revenue of 123 USD.', gaps=[])
    translated={'sections':[{'index':0,'title':'收入增长的传导','body':'T1 支持 123 USD 收入的判断，需继续跟踪 T1。','gaps':[]}]}
    self.assertEqual(validate_localized_text(source,translated)[0]['index'],0)
    translated['sections'][0]['body']='T1 支持 124 USD 收入的判断。'
    with self.assertRaises(ResearchLocalizationError):
        validate_localized_text(source,translated)
 def test_excel_titles_and_explanations_may_remain_english(self):
    source = product(); source["kind"] = "model_excel"
    checked = validate_localized_text(source, {"sections": [{
        "index": 0, "title": "Financial Model",
        "body": "Revenue was USD 1,234.50, up 5.2%.",
        "gaps": ["Missing FY2027 margin"],
    }]})
    self.assertEqual(checked[0]["title"], "Financial Model")
