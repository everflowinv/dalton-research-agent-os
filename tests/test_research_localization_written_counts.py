"""2026-09-25: ACN and CTSH NO_CHANGE drafts failed on ``missing_tokens ["4"]``.

The source said "同组 4 家公司", the draft "同组四家公司".  Small counts written
out in Chinese now account for the Arabic source token -- narrowly.
"""

from __future__ import annotations

import unittest

from dalton_core.research_localization import (
    ResearchLocalizationError,
    _number_differences,
    validate_localized_text,
)


def differences(source: str, target: str):
    return _number_differences([source], [target])


class WrittenSmallCountTests(unittest.TestCase):
    def test_the_live_cases(self):
        self.assertEqual(differences("同组 4 家公司平均下跌 3.2%", "同组四家公司平均下跌 3.2%"),
                         ([], []))
        self.assertEqual(differences("同组 4 只可比股", "同组四只可比股"), ([], []))
        self.assertEqual(differences("距今已超过 10 个月", "距今已超过十个月"), ([], []))

    def test_zero_to_ten_and_the_teens(self):
        for number, written in (("0", "零"), ("1", "一"), ("2", "两"), ("2", "二"),
                                ("7", "七"), ("10", "十"), ("11", "十一"), ("19", "十九")):
            with self.subTest(written=written):
                self.assertEqual(differences(f"共 {number} 项", f"共{written}项"), ([], []))
        self.assertEqual(differences("第 3 次", "第三次"), ([], []))
        self.assertEqual(differences("第 3 名", "第三名"), ([], []))

    def test_outside_the_rule_stays_strict(self):
        # Larger numbers, compounds and non-count uses are not equivalences.
        self.assertEqual(differences("24 个", "二十四个")[0], ["24"])
        self.assertEqual(differences("4 个", "二十四个")[0], ["4"])
        self.assertEqual(differences("4 个", "十四个")[0], ["4"])
        self.assertEqual(differences("增长 4%", "增长四成")[0], ["4%"])
        self.assertEqual(differences("1 家", "口径统一")[0], ["1"])
        self.assertEqual(differences("4.5 家", "四家")[0], ["4.5"])
        # No measure word, no 第: not a count.
        self.assertEqual(differences("共 4 项", "四面楚歌")[0], ["4"])

    def test_one_written_count_accounts_for_one_token(self):
        self.assertEqual(differences("4 家与 4 家", "四家与另一组")[0], ["4"])

    def test_a_written_count_never_licenses_an_added_number(self):
        self.assertEqual(differences("四家公司", "4 家公司")[1], [])
        self.assertEqual(differences("三家公司", "5 家公司")[1], ["5"])

    def test_validate_localized_text_accepts_the_live_draft(self):
        source = {"sections": [{"title": "事件研判", "body": "同组 4 家公司平均下跌 3.2%。",
                                "gaps": []}]}
        draft = {"sections": [{"index": 0, "title": "事件研判",
                               "body": "同组四家公司平均下跌 3.2%。", "gaps": []}]}
        validate_localized_text(source, draft)
        draft["sections"][0]["body"] = "同组五家公司平均下跌 3.2%。"
        with self.assertRaisesRegex(ResearchLocalizationError, "missing_tokens"):
            validate_localized_text(source, draft)


if __name__ == "__main__":
    unittest.main()
