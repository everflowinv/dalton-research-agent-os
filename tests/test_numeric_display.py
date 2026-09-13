from __future__ import annotations

import unittest

from dalton_core.numeric_display import format_display_number, format_typed_value
from dalton_core.final_text_contract import FINAL_TEXT_RULES_VERSION


class NumericDisplayTests(unittest.TestCase):
    def test_amounts_use_readable_chinese_units(self):
        self.assertEqual(FINAL_TEXT_RULES_VERSION, "simplified-chinese-research-prose:0.2")
        self.assertEqual(format_display_number("15623445", kind="amount_usd"), "1562 万美元")
        self.assertEqual(format_display_number("125500000", kind="amount_usd"), "1.26 亿美元")

    def test_percent_eps_and_arpu_have_distinct_precision_and_labels(self):
        self.assertEqual(format_display_number("5.24", kind="percent"), "5.2%")
        self.assertEqual(format_display_number("2.345", kind="eps"), "2.35 美元/股")
        self.assertEqual(format_display_number("12.345", kind="arpu"), "12.35 美元/用户")

    def test_boolean_is_not_a_number(self):
        with self.assertRaisesRegex(ValueError, "numeric"):
            format_display_number(True, kind="amount_usd")

    def test_typed_claims_scale_amounts_but_keep_per_share_values(self):
        self.assertEqual(format_typed_value('15.623445',unit='usd',scale='million'), '1562 万美元')
        self.assertEqual(format_typed_value('12550',unit='usd'), '1.26 万美元')
        self.assertEqual(format_typed_value('0.25',unit='usd'), '0.25 美元')
        self.assertEqual(format_typed_value('3.03',unit='currency_per_share',currency='USD',scale='million'), '3.03 美元/股')
        self.assertEqual(format_typed_value('42',unit='widgets',scale='unknown'), '42 widgets unknown')

    def test_non_finite_values_are_rejected(self):
        for value in ['NaN','Infinity','-Infinity']:
            with self.assertRaises(ValueError):format_display_number(value,kind='amount_usd')
