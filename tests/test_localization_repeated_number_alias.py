import copy
import unittest

from dalton_core.research_localization import ResearchLocalizationError, validate_localized_text


class RepeatedNumberAliasTests(unittest.TestCase):
    def test_spelled_ratio_may_repeat_in_heading_and_body(self):
        source = {"kind": "ui_text", "sections": [{"title": "Interface text",
            "body": "GIS book-to-bill was below one; CES book-to-bill was above one.", "gaps": []}]}
        output = {"sections": [{"index": 0, "title": "CES 订单出货比大于 1",
            "body": "GIS 订单出货比小于 1；CES 订单出货比大于 1。", "gaps": []}]}
        self.assertEqual(len(validate_localized_text(source, output)), 1)
        changed = copy.deepcopy(output)
        changed["sections"][0]["body"] += " 目标为 2。"
        with self.assertRaisesRegex(ResearchLocalizationError, "number tokens"):
            validate_localized_text(source, changed)
        source["sections"][0]["body"] += " The period was 4 quarters."
        with self.assertRaisesRegex(ResearchLocalizationError, "number tokens"):
            validate_localized_text(source, output)

    def test_written_chinese_ratio_may_repeat_in_heading(self):
        source = {"kind": "ui_text", "sections": [{"title": "订单趋势",
            "body": "GIS 订单出货比低于一，CES 订单出货比高于一。", "gaps": []}]}
        output = {"sections": [{"index": 0, "title": "CES 订单出货比高于 1",
            "body": "GIS 订单出货比低于 1，CES 订单出货比高于 1。", "gaps": []}]}
        self.assertEqual(len(validate_localized_text(source, output)), 1)
