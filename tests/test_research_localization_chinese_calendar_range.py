import unittest

from dalton_core.research_localization import (
    ResearchLocalizationError,
    validate_localized_text,
)


def product(period: str = "2025-04-01..2025-06-30") -> dict:
    return {
        "kind": "ui_text",
        "sections": [{"title": period, "body": f"Period: {period}", "gaps": []}],
    }


def localized(period: str = "2025年4月1日至6月30日") -> dict:
    return {
        "sections": [{
            "index": 0,
            "title": "2025年第二季度",
            "body": f"期间为{period}。",
            "gaps": [],
        }],
    }


class ChineseCalendarQuarterRangeTest(unittest.TestCase):
    def test_retained_exact_chinese_range_and_quarter_title_are_not_double_counted(self):
        self.assertEqual(validate_localized_text(product(), localized())[0]["index"], 0)

    def test_explicit_same_end_year_is_equivalent(self):
        self.assertEqual(
            validate_localized_text(product(), localized("2025年4月1日至2025年6月30日"))[0]["index"],
            0,
        )

    def test_changed_year_is_rejected(self):
        with self.assertRaisesRegex(ResearchLocalizationError, "changed financial number tokens"):
            validate_localized_text(product(), localized("2025年4月1日至2026年6月30日"))

    def test_non_calendar_quarter_is_rejected(self):
        with self.assertRaisesRegex(ResearchLocalizationError, "changed financial number tokens"):
            validate_localized_text(product(), localized("2025年4月2日至6月30日"))

    def test_changed_end_boundary_is_rejected(self):
        with self.assertRaisesRegex(ResearchLocalizationError, "changed financial number tokens"):
            validate_localized_text(product(), localized("2025年4月1日至6月29日"))

    def test_latin_q_name_matches_only_its_exact_iso_quarter(self):
        source = product("2025-07-01..2025-09-30")
        translated = localized("2025年7月1日至9月30日")
        translated["sections"][0]["title"] = "2025年Q3"
        self.assertEqual(validate_localized_text(source, translated)[0]["index"], 0)
        for title in ("2025年Q2", "2024 Q3"):
            with self.subTest(title=title):
                translated["sections"][0]["title"] = title
                with self.assertRaisesRegex(ResearchLocalizationError, "changed financial number tokens"):
                    validate_localized_text(source, translated)

    def test_one_decimal_yi_rounding_is_exactly_derived(self):
        source = {
            "kind": "ui_text",
            "sections": [{"title": "Amount", "body": "Revenue was 1394373000 USD.", "gaps": []}],
        }
        translated = {"sections": [{
            "index": 0, "title": "金额", "body": "收入为13.9亿美元。", "gaps": [],
        }]}
        self.assertEqual(validate_localized_text(source, translated)[0]["index"], 0)
        translated["sections"][0]["body"] = "收入为13.94亿美元。"
        self.assertEqual(validate_localized_text(source, translated)[0]["index"], 0)
        translated["sections"][0]["body"] = "收入为14.0亿美元。"
        with self.assertRaisesRegex(ResearchLocalizationError, "changed financial number tokens"):
            validate_localized_text(source, translated)


if __name__ == "__main__":
    unittest.main()
