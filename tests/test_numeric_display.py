from __future__ import annotations

import unittest

from dalton_core.numeric_display import format_display_number
from dalton_core.final_text_contract import FINAL_TEXT_RULES_VERSION


class NumericDisplayTests(unittest.TestCase):
    def test_amounts_use_readable_chinese_units(self):
        self.assertEqual(FINAL_TEXT_RULES_VERSION, "simplified-chinese-research-prose:0.2")
        self.assertEqual(format_display_number("15623445", kind="amount_usd"), "1562 万美元")
        self.assertEqual(format_display_number("125500000", kind="amount_usd"), "1.3 亿美元")

    def test_percent_eps_and_arpu_have_distinct_precision_and_labels(self):
        self.assertEqual(format_display_number("5.24", kind="percent"), "5.2%")
        self.assertEqual(format_display_number("2.345", kind="eps"), "2.35 美元/股")
        self.assertEqual(format_display_number("12.345", kind="arpu"), "12.35 美元/用户")

    def test_boolean_is_not_a_number(self):
        with self.assertRaisesRegex(ValueError, "numeric"):
            format_display_number(True, kind="amount_usd")
