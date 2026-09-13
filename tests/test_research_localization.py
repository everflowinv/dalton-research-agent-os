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
    self.assertIn("Preserve every financial number token exactly", producer)
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
