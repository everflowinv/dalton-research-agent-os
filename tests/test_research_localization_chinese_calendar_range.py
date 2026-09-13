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


if __name__ == "__main__":
    unittest.main()
