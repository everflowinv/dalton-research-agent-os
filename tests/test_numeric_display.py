from __future__ import annotations

import unittest

from dalton_core.numeric_display import (
    format_display_number, format_prose_date_ranges, format_prose_usd_amounts, format_typed_value,
)
from dalton_core.final_text_contract import FINAL_TEXT_RULES_VERSION


class NumericDisplayTests(unittest.TestCase):
    def test_reviewed_date_ranges_are_readable_without_changing_quotes(self):
        text = ('期间2026-04-01..2026-06-30；“原文2026-04-01..2026-06-30”\n'
                '> 引文2026-04-01..2026-06-30\n无效2026-02-30..2026-03-31')
        self.assertEqual(
            format_prose_date_ranges(text),
            ('期间2026年4月1日至2026年6月30日；“原文2026-04-01..2026-06-30”\n'
             '> 引文2026-04-01..2026-06-30\n无效2026-02-30..2026-03-31'))
        url = "查看 https://example.test/?range=2026-04-01..2026-06-30"
        self.assertEqual(format_prose_date_ranges(url), url)
    def test_formats_only_explicit_large_base_usd_in_chinese_prose(self):
        text = ("收入为 USD 1,535,000,000，成本为14968000000 USD，"
                "现金流为-14752000000美元。")
        self.assertEqual(format_prose_usd_amounts(text),
            "收入为 15.35 亿美元，成本为149.7 亿美元，现金流为-147.5 亿美元。")

    def test_preserves_quotes_scaled_amounts_small_values_and_bare_dollars(self):
        text = ('正文为1535000000美元；“原文为 1535000000 USD”；'
                '说明为 USD 1.535 billion、15.35亿美元、EPS 2.35 USD、$1535000000。')
        self.assertEqual(format_prose_usd_amounts(text),
            '正文为15.35 亿美元；“原文为 1535000000 USD”；'
            '说明为 USD 1.535 billion、15.35亿美元、EPS 2.35 USD、$1535000000。')

    def test_preserves_markdown_quotes_and_pure_english_lines(self):
        text = ('> 引文金额为 1535000000 USD\n'
                'Revenue was 1535000000 USD.\n'
                '中文正文为1535000000 USD。\n')
        self.assertEqual(format_prose_usd_amounts(text),
            '> 引文金额为 1535000000 USD\n'
            'Revenue was 1535000000 USD.\n'
            '中文正文为15.35 亿美元。\n')

    def test_rejects_partial_scientific_and_malformed_numeric_tokens(self):
        text = ('保留1e100000 USD、USD 1e100000、1,53,500000 USD、'
                'USD 1,53,500000；转换1535000000 USD。')
        self.assertEqual(format_prose_usd_amounts(text),
            '保留1e100000 USD、USD 1e100000、1,53,500000 USD、'
            'USD 1,53,500000；转换15.35 亿美元。')

    def test_preserves_multiline_and_escaped_quoted_source_text(self):
        text = ('引文“第一行 1535000000 USD\n'
                '第二行 14968000000 USD”；正文1535000000 USD。\n'
                '记录"source says \\"1535000000 USD\\" and 14968000000 USD"；'
                '正文1535000000美元。')
        self.assertEqual(format_prose_usd_amounts(text),
            '引文“第一行 1535000000 USD\n'
            '第二行 14968000000 USD”；正文15.35 亿美元。\n'
            '记录"source says \\"1535000000 USD\\" and 14968000000 USD"；'
            '正文15.35 亿美元。')

    def test_amounts_use_readable_chinese_units(self):
        self.assertEqual(FINAL_TEXT_RULES_VERSION, "simplified-chinese-research-prose:0.4")
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
